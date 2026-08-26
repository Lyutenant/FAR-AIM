"""Command-line interface for the far-aim pipeline (plan §18).

Phase 1 status: `check`, `validate`, and `fetch ecfr` are implemented;
the remaining commands are registered stubs that exit with code 2 until
their phase is implemented.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from far_aim import __version__
from far_aim.config import Config
from far_aim.log import setup_logging
from far_aim.manifest import ManifestError, SourceManifest, SourceState
from far_aim.models import cfr as cfr_model
from far_aim.parsers import ecfr as ecfr_parser
from far_aim.sources import ecfr

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2

CORPORA = ("ecfr", "aim", "pcg")

NOT_IMPLEMENTED_PHASE = {
    ("normalize", None): "Phase 2",
    ("diff", None): "Phase 2",
    ("build-vault", None): "Phase 3",
    ("fetch", "aim"): "Phase 4",
    ("parse", "aim"): "Phase 4",
    ("fetch", "pcg"): "Phase 5",
    ("parse", "pcg"): "Phase 5",
    ("update", None): "Phase 8",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="far-aim",
        description="Deterministic FAR/AIM/PCG ingestion pipeline and Obsidian vault generator.",
    )
    parser.add_argument("--version", action="version", version=f"far-aim {__version__}")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="project root directory (default: current directory)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)

    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="report source registry state")
    check.add_argument(
        "--remote",
        action="store_true",
        help="also poll upstream for newer source versions (read-only)",
    )
    fetch = sub.add_parser("fetch", help="download a source corpus")
    fetch.add_argument("corpus", choices=CORPORA)
    fetch.add_argument(
        "--force",
        action="store_true",
        help="re-download and accept upstream content even if the accepted version matches",
    )
    parse = sub.add_parser("parse", help="parse an archived raw corpus")
    parse.add_argument("corpus", choices=CORPORA)
    parse.add_argument(
        "--part",
        action="append",
        dest="parts",
        metavar="N",
        help="limit to one part (repeatable); default: all parts",
    )
    sub.add_parser("normalize", help="normalize parsed data into canonical JSON")
    sub.add_parser("validate", help="validate manifest and normalized data")
    sub.add_parser("diff", help="structural diff between accepted source versions")
    sub.add_parser("build-vault", help="generate the Obsidian vault from canonical data")
    sub.add_parser("update", help="run the full check→fetch→parse→validate→diff→generate sequence")
    return parser


def cmd_check(config: Config, *, remote: bool = False) -> int:
    path = config.manifest_path
    if path.exists():
        try:
            manifest = SourceManifest.load(path)
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"Source registry: {path}")
    else:
        manifest = SourceManifest.default()
        print(f"Source registry: {path} (not created yet — showing defaults)")
    print()
    rows = [("source", "accepted version", "last checked")]
    for name, state in sorted(manifest.sources.items()):
        accepted = state.accepted_version or state.effective_date or "-"
        if state.change is not None:
            accepted = f"{accepted} (change {state.change})"
        rows.append((name, accepted, state.last_checked_at or "never"))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        line = "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        print(line.rstrip())
    print()
    if not remote:
        print("note: pass --remote to poll upstream for newer versions (eCFR only for now).")
        return EXIT_OK

    try:
        with ecfr.make_client() as client:
            discovery = ecfr.discover_title14(client)
    except ecfr.FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    accepted = manifest.sources[ecfr.SOURCE_NAME].accepted_version
    latest = discovery.latest_issue_date
    print("aim, pcg: remote polling arrives in Phases 4-5.")
    if accepted == latest:
        print(f"ecfr_title_14: up to date (issue {accepted})")
        return EXIT_OK
    if accepted is not None and latest < accepted:
        # Same rule as fetch_title14: issue dates only move forward, so an
        # older upstream date is a glitch or rollback, not an update.
        print(
            f"error: ecfr_title_14: upstream reports issue {latest}, older than accepted "
            f"{accepted}; possible stale API response or upstream rollback. "
            "`fetch ecfr` will refuse this without --force.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(
        f"ecfr_title_14: update available — latest issue {latest}, accepted {accepted or 'none'}"
    )
    return EXIT_OK


def cmd_fetch_ecfr(config: Config, *, force: bool) -> int:
    try:
        result = ecfr.fetch_title14(config, force=force)
    except (ecfr.FetchError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        # Unwritable cache dir, full disk, failed rename: an operational
        # failure, not a bug — report it and leave the last known-good state.
        print(f"error: filesystem failure during fetch: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if result.downloaded:
        print(f"accepted: eCFR Title 14 issue {result.version}")
        print(f"  archive:  {result.xml_path}")
        print(f"  checksum: {result.raw_hash}")
        print(f"  size:     {result.byte_count} bytes, {result.section_count} sections")
    else:
        print(
            f"unchanged: eCFR Title 14 issue {result.version} already accepted; "
            "cached snapshot verified"
        )
    return EXIT_OK


def cmd_parse_ecfr(config: Config, parts: list[str] | None) -> int:
    """Parse the accepted raw snapshot into canonical part JSON (Phase 2).

    All requested parts are built in memory first; nothing is written unless
    every one of them parses and passes the lossless-capture check, so a
    failure preserves the last known-good normalized output (plan §32.13).

    The whole operation — manifest read, snapshot verification, parse, and
    publication — runs under the shared source lock: a concurrent
    ``fetch ecfr`` accepting a newer snapshot mid-parse would otherwise let
    this process publish documents from the old snapshot and save a stale
    in-memory manifest over the newly accepted version.
    """
    try:
        with ecfr.exclusive_lock(ecfr.fetch_lock_path(config)):
            return _parse_ecfr_locked(config, parts)
    except ecfr.FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        # Unwritable directories, full disk, failed renames the publish
        # handlers could not themselves recover from: an operational
        # failure, not a bug — report it without a traceback.
        print(f"error: filesystem failure during parse: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _parse_ecfr_locked(config: Config, parts: list[str] | None) -> int:
    try:
        manifest = SourceManifest.load(config.manifest_path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    state = manifest.sources[ecfr.SOURCE_NAME]
    if state.accepted_version is None or state.raw_hash is None:
        print("error: no accepted eCFR snapshot; run `far-aim fetch ecfr` first", file=sys.stderr)
        return EXIT_ERROR
    snapshot_dir = config.raw_dir / "ecfr" / state.accepted_version
    xml_path = snapshot_dir / "title-14.xml"
    if not xml_path.exists():
        print(
            f"error: archived snapshot missing: {xml_path}; run `far-aim fetch ecfr`",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if ecfr.sha256_of(xml_path) != state.raw_hash:
        print(
            f"error: archived snapshot {xml_path} does not match the accepted checksum; "
            "run `far-aim fetch ecfr` to restore it",
            file=sys.stderr,
        )
        return EXIT_ERROR
    # The metadata must describe the *accepted* snapshot, not merely parse:
    # an interrupted forced fetch writes metadata before publication, so a
    # stale file could otherwise attach false provenance (wrong version,
    # wrong checksum) to the canonical layer. metadata_intact cross-checks
    # provider, version, URL, checksum, byte count and timestamp format.
    metadata_path = snapshot_dir / "metadata.json"
    url = ecfr.full_title14_url(state.accepted_version)
    if not ecfr.metadata_intact(
        metadata_path,
        version=state.accepted_version,
        raw_hash=state.raw_hash,
        url=url,
        byte_count=xml_path.stat().st_size,
    ):
        print(
            f"error: snapshot metadata missing or unreadable in {snapshot_dir}, or it "
            "does not describe the accepted snapshot; run `far-aim fetch ecfr` to "
            "regenerate it",
            file=sys.stderr,
        )
        return EXIT_ERROR
    retrieved_at = json.loads(metadata_path.read_text(encoding="utf-8"))["retrieved_at"]

    source = {
        "provider": "ecfr",
        "source_version": state.accepted_version,
        "url": url,
        "retrieved_at": retrieved_at,
        "raw_checksum": state.raw_hash,
    }
    # An interrupted earlier publish may have left the committed layer
    # displaced under `.ecfr-previous`. Reconcile that *before* parsing:
    # recovery deferred to the publish step would never run if parsing
    # fails, stranding the last known-good layer off its canonical path
    # while the error below claims it was preserved.
    _recover_normalized_layer(config, state)
    try:
        root = ElementTree.parse(xml_path).getroot()
        docs = ecfr_parser.build_part_docs(root, source, set(parts) if parts else None)
    except ElementTree.ParseError as exc:
        print(f"error: archived snapshot is not well-formed XML: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ecfr_parser.ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("note: nothing was written; last known-good output preserved.", file=sys.stderr)
        return EXIT_ERROR

    if parts:
        return _publish_normalized_parts(config, state, docs)
    return _publish_normalized_title(config, manifest, docs)


def _publish_normalized_parts(
    config: Config, state: SourceState, docs: dict[str, dict]
) -> int:
    """Partial parse: update the requested part files as one on-disk unit.

    The existing layer is hard-link-copied into staging (cheap: new
    directory entries sharing the same file contents), the requested part
    files are rewritten there, and the staged copy is swapped into place
    with the same two renames as a full publish. A write failure, crash,
    kill, or ``KeyboardInterrupt`` at any point therefore leaves either the
    old layer or the fully-updated one at the canonical path — never a mix
    — and every interruption window is repaired by the standard recovery
    on the next run (in-memory backups would not survive a kill).
    """
    version = state.accepted_version
    out_dir = config.normalized_dir / "ecfr"
    staging = config.normalized_dir / ".ecfr-staging"
    previous = config.normalized_dir / ".ecfr-previous"
    # An interrupted full publish may have left the committed layer
    # displaced; writing partial files into a fresh/uncommitted `ecfr`
    # would strand it, so run the same recovery the full path performs.
    _recover_normalized_layer(config, state)
    if out_dir.exists():
        # The untouched parts are carried over from the existing layer, so
        # that layer must have been built from the *accepted* snapshot.
        # After a fetch accepts a newer snapshot (which clears the recorded
        # canonical_hash), the old layer is stale: merging one new-version
        # part into it would publish a mixed-provenance corpus as success.
        stale = _stale_layer_defect(out_dir, state)
        if stale is not None:
            print(
                f"error: the existing normalized layer was not built from the "
                f"accepted snapshot ({stale}); run `far-aim parse ecfr` (full) "
                "first. Normalized layer left untouched.",
                file=sys.stderr,
            )
            return EXIT_ERROR
    total_sections = 0
    written: list[tuple[Path, int]] = []
    try:
        if out_dir.exists():
            shutil.copytree(out_dir, staging, copy_function=os.link)
        else:
            staging.mkdir(parents=True)
        for number, doc in docs.items():
            # write_json_atomic replaces the directory entry, so the
            # hard-linked original in `out_dir` is untouched.
            write_json_atomic(staging / normalized_part_filename(number), doc)
            sections = ecfr_parser.count_sections(doc)
            total_sections += sections
            written.append((out_dir / normalized_part_filename(number), sections))
        ecfr.fsync_dir(staging)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        print(
            f"error: write failed ({exc}); normalized layer left untouched",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if state.canonical_hash is not None:
        # The manifest hash certifies a full-title parse. If this partial
        # update would change the layer's title hash (parser changes altered
        # the part's canonical content), publishing it would leave a layer
        # that `validate` rejects while reporting success — and silently
        # re-recording the hash would bless a mixed-provenance layer. Fail
        # closed instead.
        staged_hash = _layer_title_hash(staging, state)
        if staged_hash != state.canonical_hash:
            shutil.rmtree(staging)
            print(
                f"error: this partial parse would change the published layer's "
                f"title hash (manifest records {state.canonical_hash}, layer would "
                f"become {staged_hash}); the manifest hash is only recorded on "
                "full-title parses — run `far-aim parse ecfr` (full) to publish "
                "this change. Normalized layer left untouched.",
                file=sys.stderr,
            )
            return EXIT_ERROR
    try:
        had_previous = _swap_staged_layer(config, out_dir, staging, previous)
    except OSError as exc:
        print(
            f"error: filesystem failure while publishing normalized layer ({exc}); "
            "rolled back to the pre-parse state",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if had_previous:
        _discard_fallback(config, previous)
    for path, sections in written:
        print(f"wrote {path} ({sections} sections)")
    print(f"parsed eCFR issue {version}: {len(docs)} part(s), {total_sections} sections")
    print(
        "note: manifest canonical_hash is only recorded on a full-title parse "
        "(Phase 2 exit)."
    )
    return EXIT_OK


def _swap_staged_layer(
    config: Config, out_dir: Path, staging: Path, previous: Path
) -> bool:
    """Two-rename swap of the staged layer into place.

    Returns whether an earlier layer was displaced to ``previous``. On
    failure the pre-swap state is restored and the ``OSError`` re-raised.
    """
    had_previous = out_dir.exists()
    try:
        if had_previous:
            os.replace(out_dir, previous)
        os.replace(staging, out_dir)
        ecfr.fsync_dir(config.normalized_dir)
    except OSError:
        _restore_previous_layer(
            config, out_dir, staging, previous, had_previous=had_previous
        )
        raise
    return had_previous


def _discard_fallback(config: Config, fallback: Path) -> None:
    """Remove a superseded ``.ecfr-previous`` and make the removal durable.

    Resurrected by a power loss, a stale fallback can later win recovery
    arbitration and displace a newer committed layer: with no full-title
    parse yet ``canonical_hash`` was never recorded, and a subsequent fetch
    accepting a newer snapshot *clears* it — in both states recovery
    reinstates ``.ecfr-previous`` unconditionally.
    """
    shutil.rmtree(fallback)
    ecfr.fsync_dir(config.normalized_dir)


def _recover_normalized_layer(config: Config, state: SourceState) -> None:
    """Repair the aftermath of an interrupted earlier publish (all parsers).

    An interrupted publish may have left the manifest's layer displaced
    under ``.ecfr-previous``; both the full-title and the ``--part`` paths
    must reconcile that before touching the canonical directory.
    """
    out_dir = config.normalized_dir / "ecfr"
    staging = config.normalized_dir / ".ecfr-staging"
    previous = config.normalized_dir / ".ecfr-previous"
    # Staging never became authoritative and is always safe to discard.
    if staging.exists():
        shutil.rmtree(staging)
    if not previous.exists():
        return
    if not out_dir.exists():
        # Crash between the two swap renames: `previous` is the sole copy.
        os.replace(previous, out_dir)
        ecfr.fsync_dir(config.normalized_dir)
        print(f"note: restored interrupted normalized layer from {previous}")
    elif state.canonical_hash is None or (
        _layer_title_hash(previous, state) == state.canonical_hash
        and _layer_title_hash(out_dir, state) != state.canonical_hash
    ):
        # Crash between the swap and the manifest commit: `out_dir` holds
        # an uncommitted layer while `previous` is the pre-command state —
        # the only fallback that must survive if the next publish fails
        # too. With a recorded hash this is verified (the manifest's layer
        # wins); with none recorded, `out_dir` cannot be a committed layer
        # at all (a completed full publish always records a hash), so the
        # pre-command `previous` is reinstated unconditionally. The
        # uncommitted layer is rebuilt from the archived snapshot on the
        # next full parse.
        os.replace(out_dir, staging)
        os.replace(previous, out_dir)
        ecfr.fsync_dir(config.normalized_dir)
        shutil.rmtree(staging)
        print(
            "note: reinstated the previous normalized layer displaced by an "
            "interrupted publish"
        )
    elif _layer_title_hash(out_dir, state) == state.canonical_hash:
        # `out_dir` verifiably matches the manifest: `previous` is
        # superseded and safe to discard.
        _discard_fallback(config, previous)
    else:
        # Neither layer verifies against the manifest (corruption, or an
        # unreadable part file). Destructive arbitration is impossible, so
        # fail closed keeping both copies for inspection (plan §32.13).
        raise ecfr.FetchError(
            f"neither the normalized layer at {out_dir} nor the displaced copy at "
            f"{previous} verifies against the manifest; refusing to discard either. "
            "Inspect them, remove the corrupt copy, then re-run `far-aim parse ecfr`."
        )


def _publish_normalized_title(
    config: Config, manifest: SourceManifest, docs: dict[str, dict]
) -> int:
    """Full parse: replace the whole normalized eCFR layer as one unit.

    The new layer is staged in a sibling directory and swapped in with two
    renames, so a failure mid-write (full disk, interruption) can never
    leave a mix of old and new part files, and parts removed or renumbered
    upstream cannot survive as stale files. Every failure path puts the
    last known-good layer back at its canonical path (plan §32.13):

    - a crash between the two swap renames leaves the old layer at
      ``.ecfr-previous`` with ``ecfr`` absent — the next publish restores
      it before doing anything else, never discards it;
    - a crash between the swap and the manifest commit leaves an
      *uncommitted* layer at ``ecfr`` while ``.ecfr-previous`` still holds
      the layer the manifest records — the next publish reconciles both
      against the recorded hash and reinstates the committed one as the
      fallback rather than discarding it;
    - a failed swap restores ``.ecfr-previous`` in-process;
    - a failed manifest commit rolls the swap back — unless the on-disk
      manifest shows the commit actually became visible before the failure
      (e.g. the rename landed but its directory fsync did not), in which
      case rolling back would *create* an inconsistency and the new layer
      is kept.
    """
    state = manifest.sources[ecfr.SOURCE_NAME]
    out_dir = config.normalized_dir / "ecfr"
    staging = config.normalized_dir / ".ecfr-staging"
    previous = config.normalized_dir / ".ecfr-previous"
    _recover_normalized_layer(config, state)

    staging.mkdir(parents=True)
    total_sections = 0
    try:
        for number, doc in docs.items():
            write_json_atomic(staging / normalized_part_filename(number), doc)
            total_sections += ecfr_parser.count_sections(doc)
        # The file contents are fsynced individually, but their directory
        # entries live in `staging` itself: sync it before the rename makes
        # it authoritative, or a power loss after the manifest commit could
        # leave a committed layer with unpersisted part files.
        ecfr.fsync_dir(staging)
    except OSError:
        # The authoritative layer is untouched at this point; just drop the
        # partial staging directory before the failure propagates.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    try:
        had_previous = _swap_staged_layer(config, out_dir, staging, previous)
    except OSError as exc:
        print(
            f"error: filesystem failure while publishing normalized layer ({exc}); "
            "rolled back to the pre-parse state",
            file=sys.stderr,
        )
        return EXIT_ERROR

    title_hash = cfr_model.canonical_hash(
        {number: doc["canonical_hash"] for number, doc in docs.items()}
    )
    version = state.accepted_version
    print(f"parsed eCFR issue {version}: {len(docs)} part(s), {total_sections} sections")
    print(f"published normalized layer: {out_dir}")
    if state.canonical_hash != title_hash:
        # Record the canonical-layer hash — the content hash over all part
        # hashes — so a markup-only upstream change is detectable as
        # content-identical (plan §14.4). Committed after the swap so the
        # manifest never references a layer that is not on disk.
        previous_hash = state.canonical_hash
        try:
            state.canonical_hash = title_hash
            manifest.save(config.manifest_path)
        except (OSError, ManifestError) as exc:
            if _manifest_records_canonical(config.manifest_path, title_hash):
                # manifest.save() failed *after* its atomic rename became
                # visible (e.g. the directory fsync): manifest and layer
                # already agree, so keep both; `previous` stays on disk as
                # recovery material until the next successful publish.
                print(
                    f"error: manifest commit reported a failure after committing "
                    f"({exc}); the new normalized layer and manifest agree and were "
                    "kept, but the manifest write may not be durable",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            state.canonical_hash = previous_hash
            _restore_previous_layer(
                config, out_dir, staging, previous, had_previous=had_previous
            )
            print(
                f"error: manifest commit failed ({exc}); normalized layer rolled "
                "back to the pre-parse state",
                file=sys.stderr,
            )
            return EXIT_ERROR
        print(f"manifest updated: canonical_hash {title_hash}")
    else:
        print(f"canonical_hash unchanged ({title_hash})")
    if had_previous:
        _discard_fallback(config, previous)
    return EXIT_OK


def _restore_previous_layer(
    config: Config, out_dir: Path, staging: Path, previous: Path, *, had_previous: bool
) -> None:
    """Restore the pre-parse state at ``out_dir``, whatever partial state a
    failed swap or a rolled-back commit left behind.

    With no earlier layer (``had_previous`` false — a first publish), an
    ``out_dir`` present here is the unpublished new layer: fail closed by
    removing it rather than leaving output the manifest never committed to.
    With an earlier layer but no ``previous`` directory, the first swap
    rename never happened, so ``out_dir`` still holds the old layer and is
    left alone.
    """
    if previous.exists():
        if out_dir.exists():
            # Both renames completed (the failure came later), so `staging`
            # is free again: displace the unpublished new layer into it,
            # then reinstate the old one.
            os.replace(out_dir, staging)
        os.replace(previous, out_dir)
    elif not had_previous and out_dir.exists():
        shutil.rmtree(out_dir)
    if staging.exists():
        shutil.rmtree(staging)
    with contextlib.suppress(OSError):
        ecfr.fsync_dir(config.normalized_dir)


# Document types that must each carry a self-verifying canonical_hash and a
# provenance block. Keyed on document_type, not on the presence of a
# canonical_hash key: a *deleted* hash must be a defect, not a skipped check
# (parent hashes deliberately exclude nested hashes, so nothing else would
# notice).
_HASHED_DOCUMENT_TYPES = frozenset(
    {
        cfr_model.DOCUMENT_TYPE_PART,
        cfr_model.DOCUMENT_TYPE_SECTION,
        cfr_model.DOCUMENT_TYPE_APPENDIX,
    }
)

_RETRIEVED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _first_document_defect(node: object, state: SourceState | None) -> str | None:
    """First integrity defect in a canonical document tree, or None.

    Walks the part document and every nested hashed document (sections,
    appendices). Each must carry a string ``canonical_hash`` that recomputes
    from its own content, and — when ``state`` is given — a ``source``
    block matching the manifest's accepted snapshot, so missing or false
    provenance cannot pass the gate (plan §32.8).
    """
    if isinstance(node, dict):
        if node.get("document_type") in _HASHED_DOCUMENT_TYPES:
            doc_id = str(node.get("id", "<unidentified document>"))
            stored = node.get("canonical_hash")
            if not isinstance(stored, str):
                return f"{doc_id!r} has no stored canonical_hash"
            if cfr_model.canonical_hash(node) != stored:
                return f"stored canonical_hash of {doc_id!r} does not match its content"
            if state is not None:
                source_defect = _source_block_defect(node.get("source"), state)
                if source_defect is not None:
                    return f"{doc_id!r}: {source_defect}"
        for value in node.values():
            if isinstance(value, list):
                for item in value:
                    bad = _first_document_defect(item, state)
                    if bad is not None:
                        return bad
    return None


def _source_block_defect(source: object, state: SourceState) -> str | None:
    """Why a document's provenance does not describe the accepted snapshot."""
    if not isinstance(source, dict):
        return "missing source block"
    expected = {
        "provider": "ecfr",
        "source_version": state.accepted_version,
        "url": ecfr.full_title14_url(state.accepted_version or ""),
        "raw_checksum": state.raw_hash,
    }
    for key, value in expected.items():
        if source.get(key) != value:
            return (
                f"source {key} {source.get(key)!r} does not match the accepted "
                f"snapshot ({value!r})"
            )
    retrieved_at = source.get("retrieved_at")
    if not isinstance(retrieved_at, str) or not _RETRIEVED_AT_RE.fullmatch(retrieved_at):
        return f"source retrieved_at {retrieved_at!r} is not a valid timestamp"
    return None


def _stale_layer_defect(layer_dir: Path, state: SourceState) -> str | None:
    """Why an on-disk layer was not built from the accepted snapshot.

    Deep-walks every part file the same way ``validate`` does — root *and*
    nested hashed documents (sections, appendices) must verify their
    canonical hashes and carry provenance matching the manifest's accepted
    version/checksum. A root-only check would let a carried-over part with
    stale or tampered nested provenance ride through a partial publish
    that reports success while ``validate`` immediately rejects the layer
    (nested ``source`` blocks are excluded from canonical hashes, so the
    staged title-hash check cannot catch them either). Returns None when
    the whole layer verifies.
    """
    for part_file in sorted(layer_dir.glob("part-*.json")):
        try:
            doc = json.loads(part_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"{part_file.name}: unreadable ({exc})"
        if not isinstance(doc, dict):
            return f"{part_file.name}: not a JSON object"
        defect = _first_document_defect(doc, state)
        if defect is not None:
            return f"{part_file.name}: {defect}"
    return None


def _layer_title_hash(layer_dir: Path, state: SourceState) -> str | None:
    """Title hash of a normalized layer, deeply verified.

    Used to identify which of two on-disk layers the manifest refers to
    during crash recovery — a decision that may *discard* the other layer,
    so stored hashes are not trusted: every document's hash is recomputed
    from its content, and every document's provenance must describe the
    manifest's accepted snapshot. Canonical hashes deliberately exclude
    ``source`` blocks, so a layer with stale or tampered provenance —
    which ``validate`` rejects — could otherwise still win destructive
    arbitration and delete the intact copy. A truncated or tampered file
    whose stored hash was left intact must disqualify the layer, not
    match. Returns None when the layer is unreadable, malformed, or fails
    deep verification (never matches).
    """
    part_hashes: dict[str, str] = {}
    for part_file in sorted(layer_dir.glob("part-*.json")):
        try:
            doc = json.loads(part_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        number, stored = doc.get("part"), doc.get("canonical_hash")
        if not isinstance(number, str) or not isinstance(stored, str):
            return None
        # The type-keyed walker would skip a root whose document_type was
        # stripped, leaving its stored hash untrusted-but-used; require it.
        if doc.get("document_type") != cfr_model.DOCUMENT_TYPE_PART:
            return None
        # Mirror `validate`: a valid document under the wrong filename, or
        # the same part in two files, must disqualify the layer here too —
        # otherwise dict assignment would silently collapse the duplicate
        # and a layer that `validate` rejects could still win destructive
        # recovery arbitration.
        if part_file.name != normalized_part_filename(number) or number in part_hashes:
            return None
        if _first_document_defect(doc, state) is not None:
            return None
        part_hashes[number] = stored
    if not part_hashes:
        return None
    return cfr_model.canonical_hash(part_hashes)


def _manifest_records_canonical(manifest_path: Path, title_hash: str) -> bool:
    """True if the on-disk manifest already records ``title_hash`` for eCFR."""
    try:
        state = SourceManifest.load(manifest_path).sources[ecfr.SOURCE_NAME]
    except Exception:  # noqa: BLE001 - unreadable manifest: cannot prove the commit
        return False
    return state.canonical_hash == title_hash


def normalized_part_filename(number: str) -> str:
    """``91`` → ``part-0091.json`` (sortable); ranges keep their text."""
    return f"part-{number.zfill(4) if number.isdigit() else number}.json"


def write_json_atomic(path: Path, obj: dict) -> None:
    """Deterministic serialization (sorted keys, LF, trailing newline), atomic replace."""
    payload = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def cmd_validate(config: Config) -> int:
    """Validate manifest and normalized layer under the shared source lock.

    Without the lock, a concurrent full parse could commit the manifest
    between the manifest read and the layer walk (or swap the layer between
    the two), making a perfectly valid publication look like a mismatch.
    """
    path = config.manifest_path
    if not path.exists():
        print(f"error: source registry missing: {path}", file=sys.stderr)
        return EXIT_ERROR
    try:
        with ecfr.exclusive_lock(ecfr.fetch_lock_path(config)):
            return _validate_locked(config)
    except ecfr.FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        # An unopenable lock file (read-only manifests directory) is an
        # operational failure, not a bug — no traceback.
        print(f"error: filesystem failure during validate: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _validate_locked(config: Config) -> int:
    path = config.manifest_path
    try:
        manifest = SourceManifest.load(path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"ok: manifest schema valid ({path})")
    return _validate_normalized_ecfr(config, manifest)


def _validate_normalized_ecfr(config: Config, manifest: SourceManifest) -> int:
    """Verify the normalized eCFR layer against the manifest (plan §17.2/.3).

    Every part file's stored ``canonical_hash`` must recompute from its own
    content, and the title hash over all part hashes must match the
    manifest — so tampered, truncated, or stale normalized data fails
    loudly instead of flowing into vault generation.
    """
    state = manifest.sources[ecfr.SOURCE_NAME]
    recorded = state.canonical_hash
    out_dir = config.normalized_dir / "ecfr"
    part_files = sorted(out_dir.glob("part-*.json")) if out_dir.is_dir() else []
    if recorded is None and not part_files:
        print("note: no normalized eCFR data yet; run `far-aim parse ecfr`.")
        return EXIT_OK
    if recorded is None:
        print(
            "error: normalized eCFR files exist but the manifest records no "
            "canonical_hash; re-run `far-aim parse ecfr` (full title)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if not part_files:
        print(
            f"error: manifest records canonical_hash but {out_dir} has no part files; "
            "re-run `far-aim parse ecfr`",
            file=sys.stderr,
        )
        return EXIT_ERROR
    part_hashes: dict[str, str] = {}
    for part_file in part_files:
        try:
            doc = json.loads(part_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: cannot read {part_file}: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if not isinstance(doc, dict):
            print(
                f"error: {part_file}: not a JSON object (found {type(doc).__name__})",
                file=sys.stderr,
            )
            return EXIT_ERROR
        stored = doc.get("canonical_hash")
        # The defect walker is keyed on document_type, so a root object
        # whose type was stripped or mangled would skip its own hash check
        # while its stored hash still feeds the title hash; the file's root
        # must be a part document.
        if doc.get("document_type") != cfr_model.DOCUMENT_TYPE_PART:
            print(
                f"error: {part_file}: root document_type "
                f"{doc.get('document_type')!r} is not "
                f"{cfr_model.DOCUMENT_TYPE_PART!r}",
                file=sys.stderr,
            )
            return EXIT_ERROR
        # Every hashed document in the file — the part AND each nested
        # section/appendix — must carry a hash that recomputes from its own
        # content (nested hashes are excluded from their parent's hash, so
        # checking only the part would let stale or deleted section-level
        # hashes pass) and provenance matching the accepted snapshot
        # (canonical hashing strips the source block, so nothing else
        # would notice false provenance).
        bad = _first_document_defect(doc, state)
        if bad is not None:
            print(f"error: {part_file}: {bad}", file=sys.stderr)
            return EXIT_ERROR
        # A valid document under the wrong filename (a copied or duplicated
        # part file) passes its own hash check but corrupts the layer; the
        # filename must match the part it contains, and each part must
        # appear exactly once.
        number = doc.get("part")
        expected_name = normalized_part_filename(number) if isinstance(number, str) else None
        if part_file.name != expected_name:
            print(
                f"error: {part_file}: contains part {number!r} "
                f"(expected filename {expected_name!r})",
                file=sys.stderr,
            )
            return EXIT_ERROR
        if number in part_hashes:
            print(f"error: part {number!r} appears in more than one file", file=sys.stderr)
            return EXIT_ERROR
        part_hashes[number] = stored
    title_hash = cfr_model.canonical_hash(part_hashes)
    if title_hash != recorded:
        print(
            f"error: normalized eCFR layer hashes to {title_hash} but the manifest "
            f"records {recorded}; re-run `far-aim parse ecfr`",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(f"ok: normalized eCFR layer verified ({len(part_files)} parts, {title_hash})")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    config = Config.load(args.root)

    if args.command == "check":
        return cmd_check(config, remote=args.remote)
    if args.command == "validate":
        return cmd_validate(config)
    if args.command == "fetch" and args.corpus == "ecfr":
        return cmd_fetch_ecfr(config, force=args.force)
    if args.command == "parse" and args.corpus == "ecfr":
        return cmd_parse_ecfr(config, args.parts)

    corpus = getattr(args, "corpus", None)
    phase = NOT_IMPLEMENTED_PHASE[(args.command, corpus)]
    target = f"{args.command} {corpus}" if corpus else args.command
    print(f"far-aim {target}: not implemented yet — planned for {phase}.", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
