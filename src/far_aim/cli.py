"""Command-line interface for the far-aim pipeline (plan §18).

Implemented: `check`, `validate`, `fetch ecfr|aim`, `parse ecfr|aim`,
`build-vault`. The remaining commands are registered stubs that exit with
code 2 until their phase is implemented.

The normalized layer of every corpus is published, recovered and verified
by the same code, parametrized by a :class:`LayerSpec` (directory name,
manifest source, document types, filename rule, provenance check).
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from far_aim import __version__
from far_aim.config import Config
from far_aim.generate import BuildError
from far_aim.generate import aim_notes as generate_aim_notes
from far_aim.generate import build as generate_build
from far_aim.generate import notes as generate_notes
from far_aim.generate import pcg_notes as generate_pcg_notes
from far_aim.links import glossary
from far_aim.log import setup_logging
from far_aim.manifest import ManifestError, SourceManifest, SourceState
from far_aim.models import aim as aim_model
from far_aim.models import cfr as cfr_model
from far_aim.models import pcg as pcg_model
from far_aim.parsers import aim as aim_parser
from far_aim.parsers import ecfr as ecfr_parser
from far_aim.parsers import pcg as pcg_parser
from far_aim.sources import aim as aim_source
from far_aim.sources import ecfr
from far_aim.sources import pcg as pcg_source
from far_aim.sources.common import FetchError, exclusive_lock, fetch_lock_path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2

CORPORA = ("ecfr", "aim", "pcg")

NOT_IMPLEMENTED_PHASE = {
    ("normalize", None): "Phase 2",
    ("diff", None): "Phase 2",
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
        help="eCFR only: limit to one part (repeatable); default: all parts",
    )
    sub.add_parser("normalize", help="normalize parsed data into canonical JSON")
    sub.add_parser("validate", help="validate manifest and normalized data")
    sub.add_parser("diff", help="structural diff between accepted source versions")
    sub.add_parser("build-vault", help="generate the Obsidian vault from canonical data")
    sub.add_parser("update", help="run the full check→fetch→parse→validate→diff→generate sequence")
    return parser


# ---------------------------------------------------------------------------
# Layer specifications
# ---------------------------------------------------------------------------

_RETRIEVED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


@dataclass(frozen=True)
class LayerSpec:
    """How one corpus's normalized layer is laid out and verified."""

    name: str  # directory under data/normalized (also names the staging dirs)
    source_name: str  # manifest key
    label: str  # human label in messages
    fetch_command: str
    parse_command: str
    root_types: frozenset[str]
    hashed_types: frozenset[str]
    file_glob: str
    file_noun: str  # "part files"
    key_noun: str  # "part"
    doc_key: Callable[[dict], str | None]  # root document → layer key
    filename: Callable[[str], str]  # layer key → file name
    source_defect: Callable[[dict, dict, SourceState], str | None]  # (document, source, state)

    @property
    def out_dir_name(self) -> str:
        return self.name

    def staging_dir(self, config: Config) -> Path:
        return config.normalized_dir / f".{self.name}-staging"

    def previous_dir(self, config: Config) -> Path:
        return config.normalized_dir / f".{self.name}-previous"

    def out_dir(self, config: Config) -> Path:
        return config.normalized_dir / self.name


def _ecfr_source_defect(doc: dict, source: dict, state: SourceState) -> str | None:
    expected = {
        "provider": "ecfr",
        "source_version": state.accepted_version,
        "url": ecfr.full_title14_url(state.accepted_version or ""),
        "raw_checksum": state.raw_hash,
    }
    return _expected_source_fields(source, expected)


def _aim_source_defect(doc: dict, source: dict, state: SourceState) -> str | None:
    """Every provenance field the AIM notes render must match the manifest.

    ``edition_label`` and ``url`` are pinned in the manifest at fetch time
    precisely so an altered normalized ``source`` block (which canonical
    hashes exclude) cannot flow into generated notes as verified provenance.
    """
    expected = {
        "provider": "faa",
        "publication": "aim",
        "source_version": state.accepted_version,
        "edition_label": state.edition_label,
        "effective_date": state.effective_date,
        "change": state.change,
        "raw_checksum": state.raw_hash,
    }
    defect = _expected_source_fields(source, expected)
    if defect is not None:
        return defect
    return _aim_url_defect(doc, source.get("url"), state.source_url)


def _aim_url_defect(doc: dict, url: object, index_url: str | None) -> str | None:
    """A document's ``source.url`` must be *its own* page of the accepted edition.

    The expected page identity and anchor derive from the document's type
    and citation (chapter → ``chap_N``, section/paragraph → ``chapN_section_M``
    with the paragraph number as anchor, appendix → ``appendix_N``); the
    filename may be padded (``chap_04.html``) but must denote that identity.
    """
    if not isinstance(index_url, str) or not isinstance(url, str):
        return f"source url {url!r} cannot be checked against the manifest ({index_url!r})"
    base = index_url.rsplit("/", 1)[0] + "/"
    if not url.startswith(base):
        return f"source url {url!r} is outside the accepted edition ({base!r})"
    page, _, anchor = url[len(base) :].partition("#")
    kind = doc.get("document_type")
    if kind == aim_model.DOCUMENT_TYPE_PUBLICATION:
        if page != aim_source.INDEX_PAGE or anchor:
            return f"source url {url!r} is not the edition index page"
        return None
    try:
        identity = aim_source.classify_page(page)
    except ValueError:
        return f"source url {url!r} does not name an edition page"
    expected_anchor = ""
    if kind == aim_model.DOCUMENT_TYPE_CHAPTER:
        expected = ("chapter", doc.get("chapter"))
    elif kind == aim_model.DOCUMENT_TYPE_SECTION:
        expected = ("section", doc.get("chapter"), doc.get("section"))
    elif kind == aim_model.DOCUMENT_TYPE_PARAGRAPH:
        expected = ("section", doc.get("chapter"), doc.get("section"))
        expected_anchor = str(doc.get("paragraph"))
    elif kind == aim_model.DOCUMENT_TYPE_APPENDIX:
        expected = ("appendix", doc.get("appendix"))
    else:
        return f"document type {kind!r} has no expected source page"
    if identity != expected or anchor != expected_anchor:
        return (
            f"source url {url!r} does not match the document's identity "
            f"({doc.get('id')!r} expects page {expected} anchor {expected_anchor!r})"
        )
    return None


def _expected_source_fields(source: dict, expected: dict) -> str | None:
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


def fsync_dir(path: Path) -> None:
    """Directory fsync, resolved through ``sources.ecfr`` so tests can observe it."""
    ecfr.fsync_dir(path)


def normalized_part_filename(number: str) -> str:
    """``91`` → ``part-0091.json`` (sortable); ranges keep their text."""
    return f"part-{number.zfill(4) if number.isdigit() else number}.json"


def _aim_doc_key(doc: dict) -> str | None:
    kind = doc.get("document_type")
    if kind == aim_model.DOCUMENT_TYPE_PUBLICATION:
        return "publication"
    if kind == aim_model.DOCUMENT_TYPE_CHAPTER and isinstance(doc.get("chapter"), int):
        return f"chapter-{doc['chapter']:02d}"
    if kind == aim_model.DOCUMENT_TYPE_APPENDIX and isinstance(doc.get("appendix"), int):
        return f"appendix-{doc['appendix']}"
    return None


ECFR_SPEC = LayerSpec(
    name="ecfr",
    source_name=ecfr.SOURCE_NAME,
    label="eCFR",
    fetch_command="fetch ecfr",
    parse_command="parse ecfr",
    root_types=frozenset({cfr_model.DOCUMENT_TYPE_PART}),
    hashed_types=frozenset(
        {
            cfr_model.DOCUMENT_TYPE_PART,
            cfr_model.DOCUMENT_TYPE_SECTION,
            cfr_model.DOCUMENT_TYPE_APPENDIX,
        }
    ),
    file_glob="part-*.json",
    file_noun="part files",
    key_noun="part",
    doc_key=lambda doc: doc.get("part") if isinstance(doc.get("part"), str) else None,
    filename=normalized_part_filename,
    source_defect=_ecfr_source_defect,
)

def _pcg_source_defect(doc: dict, source: dict, state: SourceState) -> str | None:
    """Every provenance field the PCG notes render must match the manifest."""
    expected = {
        "provider": "faa",
        "publication": "pcg",
        "source_version": state.accepted_version,
        "edition_label": state.edition_label,
        "effective_date": state.effective_date,
        "change": state.change,
        "raw_checksum": state.raw_hash,
    }
    defect = _expected_source_fields(source, expected)
    if defect is not None:
        return defect
    return _pcg_url_defect(doc, source.get("url"), state.source_url)


def _pcg_url_defect(doc: dict, url: object, index_url: str | None) -> str | None:
    """A document's ``source.url`` must be *its own* page of the accepted edition.

    Publication → the index page; letter → its ``glossary-x.html`` page;
    term → its letter's page. A term's anchor is upstream layout (anchor ids
    are not unique in the FAA HTML), so any or no fragment is accepted on
    term documents, but never on container documents.
    """
    if not isinstance(index_url, str) or not isinstance(url, str):
        return f"source url {url!r} cannot be checked against the manifest ({index_url!r})"
    base = index_url.rsplit("/", 1)[0] + "/"
    if not url.startswith(base):
        return f"source url {url!r} is outside the accepted edition ({base!r})"
    page, _, anchor = url[len(base) :].partition("#")
    kind = doc.get("document_type")
    if kind == pcg_model.DOCUMENT_TYPE_PUBLICATION:
        if page != pcg_source.INDEX_PAGE or anchor:
            return f"source url {url!r} is not the edition index page"
        return None
    try:
        identity = pcg_source.classify_page(page)
    except ValueError:
        return f"source url {url!r} does not name an edition page"
    if kind == pcg_model.DOCUMENT_TYPE_LETTER:
        expected = ("letter", str(doc.get("letter", "")).lower())
        if identity != expected or anchor:
            return (
                f"source url {url!r} does not match the document's identity "
                f"({doc.get('id')!r} expects page {expected})"
            )
        return None
    if kind == pcg_model.DOCUMENT_TYPE_TERM:
        expected = ("letter", str(doc.get("letter", "")).lower())
        if identity != expected:
            return (
                f"source url {url!r} does not match the document's identity "
                f"({doc.get('id')!r} expects page {expected})"
            )
        return None
    return f"document type {kind!r} has no expected source page"


def _pcg_doc_key(doc: dict) -> str | None:
    kind = doc.get("document_type")
    if kind == pcg_model.DOCUMENT_TYPE_PUBLICATION:
        return "publication"
    if kind == pcg_model.DOCUMENT_TYPE_LETTER and isinstance(doc.get("letter"), str):
        return f"letter-{doc['letter'].lower()}"
    return None


AIM_SPEC = LayerSpec(
    name="aim",
    source_name=aim_source.SOURCE_NAME,
    label="AIM",
    fetch_command="fetch aim",
    parse_command="parse aim",
    root_types=frozenset(
        {
            aim_model.DOCUMENT_TYPE_PUBLICATION,
            aim_model.DOCUMENT_TYPE_CHAPTER,
            aim_model.DOCUMENT_TYPE_APPENDIX,
        }
    ),
    hashed_types=frozenset(
        {
            aim_model.DOCUMENT_TYPE_PUBLICATION,
            aim_model.DOCUMENT_TYPE_CHAPTER,
            aim_model.DOCUMENT_TYPE_SECTION,
            aim_model.DOCUMENT_TYPE_PARAGRAPH,
            aim_model.DOCUMENT_TYPE_APPENDIX,
        }
    ),
    file_glob="*.json",
    file_noun="document files",
    key_noun="document",
    doc_key=_aim_doc_key,
    filename=lambda key: f"{key}.json",
    source_defect=_aim_source_defect,
)

PCG_SPEC = LayerSpec(
    name="pcg",
    source_name=pcg_source.SOURCE_NAME,
    label="PCG",
    fetch_command="fetch pcg",
    parse_command="parse pcg",
    root_types=frozenset(
        {
            pcg_model.DOCUMENT_TYPE_PUBLICATION,
            pcg_model.DOCUMENT_TYPE_LETTER,
        }
    ),
    hashed_types=frozenset(
        {
            pcg_model.DOCUMENT_TYPE_PUBLICATION,
            pcg_model.DOCUMENT_TYPE_LETTER,
            pcg_model.DOCUMENT_TYPE_TERM,
        }
    ),
    file_glob="*.json",
    file_noun="document files",
    key_noun="document",
    doc_key=_pcg_doc_key,
    filename=lambda key: f"{key}.json",
    source_defect=_pcg_source_defect,
)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


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
        print("note: pass --remote to poll upstream for newer versions (eCFR and AIM).")
        return EXIT_OK

    # Each upstream is polled independently: one unreachable source must not
    # hide the others' status (plan §25.3 — network failure ≠ source removed).
    code = EXIT_OK
    with ecfr.make_client() as client:
        try:
            discovery = ecfr.discover_title14(client)
        except FetchError as exc:
            print(f"error: ecfr_title_14: {exc}", file=sys.stderr)
            discovery = None
        try:
            aim_discovery = aim_source.discover_aim(client)
        except FetchError as exc:
            print(f"error: aim: {exc}", file=sys.stderr)
            aim_discovery = None
        try:
            pcg_discovery = pcg_source.discover_pcg(client)
        except FetchError as exc:
            print(f"error: pcg: {exc}", file=sys.stderr)
            pcg_discovery = None
    if discovery is None or aim_discovery is None or pcg_discovery is None:
        code = EXIT_ERROR
    if discovery is not None:
        code = max(code, _report_ecfr_remote(manifest, discovery))
    if aim_discovery is not None:
        code = max(code, _report_aim_remote(manifest, aim_discovery))
    if pcg_discovery is not None:
        code = max(code, _report_pcg_remote(manifest, pcg_discovery))
    return code


def _report_ecfr_remote(manifest: SourceManifest, discovery: ecfr.TitleDiscovery) -> int:
    accepted = manifest.sources[ecfr.SOURCE_NAME].accepted_version
    latest = discovery.latest_issue_date
    if accepted == latest:
        print(f"ecfr_title_14: up to date (issue {accepted})")
    elif accepted is not None and latest < accepted:
        # Same rule as fetch_title14: issue dates only move forward, so an
        # older upstream date is a glitch or rollback, not an update.
        print(
            f"error: ecfr_title_14: upstream reports issue {latest}, older than accepted "
            f"{accepted}; possible stale API response or upstream rollback. "
            "`fetch ecfr` will refuse this without --force.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    else:
        print(
            f"ecfr_title_14: update available — latest issue {latest}, "
            f"accepted {accepted or 'none'}"
        )
    return EXIT_OK


def _report_aim_remote(manifest: SourceManifest, aim_discovery: aim_source.AimDiscovery) -> int:
    aim_state = manifest.sources[aim_source.SOURCE_NAME]
    listed = f"{aim_discovery.label} (effective {aim_discovery.effective_date})"
    if aim_state.accepted_version == aim_discovery.version:
        if not aim_source.listing_matches_pins(aim_state, aim_discovery):
            print(
                f"error: aim: FAA now lists the accepted edition {aim_discovery.version} as "
                f"{aim_discovery.label!r} at {aim_discovery.index_url!r} (accepted as "
                f"{aim_state.edition_label!r} at {aim_state.source_url!r}); `fetch aim` will "
                "refuse this without --force.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        print(f"aim: up to date ({listed})")
    elif (
        aim_state.effective_date is not None
        and aim_state.change is not None
        and (aim_discovery.effective_date, aim_discovery.change)
        < (aim_state.effective_date, aim_state.change)
    ):
        print(
            f"error: aim: FAA lists {listed}, older than accepted effective "
            f"{aim_state.effective_date} change {aim_state.change}; `fetch aim` will refuse "
            "this without --force.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    else:
        print(
            f"aim: update available — FAA lists {listed}, "
            f"accepted {aim_state.accepted_version or 'none'}"
        )
    return EXIT_OK


def _report_pcg_remote(manifest: SourceManifest, discovery: pcg_source.PcgDiscovery) -> int:
    state = manifest.sources[pcg_source.SOURCE_NAME]
    listed = f"{discovery.label} (effective {discovery.effective_date})"
    if state.accepted_version == discovery.version:
        if not pcg_source.listing_matches_pins(state, discovery):
            print(
                f"error: pcg: FAA now lists the accepted edition {discovery.version} as "
                f"{discovery.label!r} at {discovery.index_url!r} (accepted as "
                f"{state.edition_label!r} at {state.source_url!r}); `fetch pcg` will "
                "refuse this without --force.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        print(f"pcg: up to date ({listed})")
    elif (
        state.effective_date is not None
        and state.change is not None
        and (discovery.effective_date, discovery.change) < (state.effective_date, state.change)
    ):
        print(
            f"error: pcg: FAA lists {listed}, older than accepted effective "
            f"{state.effective_date} change {state.change}; `fetch pcg` will refuse "
            "this without --force.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    else:
        print(
            f"pcg: update available — FAA lists {listed}, "
            f"accepted {state.accepted_version or 'none'}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def cmd_fetch_ecfr(config: Config, *, force: bool) -> int:
    try:
        result = ecfr.fetch_title14(config, force=force)
    except (FetchError, ManifestError) as exc:
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


def cmd_fetch_aim(config: Config, *, force: bool) -> int:
    try:
        result = aim_source.fetch_aim(config, force=force)
    except (FetchError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: filesystem failure during fetch: {exc}", file=sys.stderr)
        return EXIT_ERROR
    edition = f"{result.label} (effective {result.effective_date})"
    if result.downloaded:
        print(f"accepted: AIM {edition}")
        print(f"  archive:  {result.snapshot_dir}")
        print(f"  checksum: {result.raw_hash}")
        print(
            f"  size:     {result.byte_count} bytes, {result.page_count} pages, "
            f"{result.paragraph_count} paragraphs, {result.figure_count} figures"
        )
        print(
            "note: the FAA offers no point-in-time access — archive this snapshot "
            "outside the repository (plan §6.2)."
        )
    else:
        print(f"unchanged: AIM {edition} already accepted; cached snapshot verified")
    return EXIT_OK


def cmd_fetch_pcg(config: Config, *, force: bool) -> int:
    try:
        result = pcg_source.fetch_pcg(config, force=force)
    except (FetchError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: filesystem failure during fetch: {exc}", file=sys.stderr)
        return EXIT_ERROR
    edition = f"{result.label} (effective {result.effective_date})"
    if result.downloaded:
        print(f"accepted: PCG {edition}")
        print(f"  archive:  {result.snapshot_dir}")
        print(f"  checksum: {result.raw_hash}")
        print(
            f"  size:     {result.byte_count} bytes, {result.page_count} pages, "
            f"{result.term_count} term entries"
        )
        print(
            "note: the FAA offers no point-in-time access — archive this snapshot "
            "outside the repository (plan §6.2)."
        )
    else:
        print(f"unchanged: PCG {edition} already accepted; cached snapshot verified")
    return EXIT_OK


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


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
        with exclusive_lock(fetch_lock_path(config)):
            return _parse_ecfr_locked(config, parts)
    except FetchError as exc:
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
    _recover_normalized_layer(config, state, ECFR_SPEC)
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
    total_sections = sum(ecfr_parser.count_sections(doc) for doc in docs.values())
    summary = (
        f"parsed eCFR issue {state.accepted_version}: {len(docs)} part(s), "
        f"{total_sections} sections"
    )
    return _publish_normalized_layer(config, manifest, docs, ECFR_SPEC, summary)


def cmd_parse_aim(config: Config) -> int:
    """Parse the accepted AIM snapshot into canonical chapter/appendix JSON (Phase 4)."""
    try:
        with exclusive_lock(fetch_lock_path(config)):
            return _parse_aim_locked(config)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: filesystem failure during parse: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _aim_snapshot(config: Config, state: SourceState) -> tuple[Path, dict] | str:
    """The verified accepted AIM snapshot directory and its metadata, or a defect."""
    if state.accepted_version is None or state.raw_hash is None:
        return "no accepted AIM snapshot; run `far-aim fetch aim` first"
    snapshot_dir = config.raw_dir / "aim" / state.accepted_version
    if not snapshot_dir.is_dir():
        return f"archived snapshot missing: {snapshot_dir}; run `far-aim fetch aim`"
    if not aim_source.verify_snapshot(
        snapshot_dir, version=state.accepted_version, raw_hash=state.raw_hash
    ):
        return (
            f"archived snapshot {snapshot_dir} is incomplete or does not match the accepted "
            "checksum; run `far-aim fetch aim` to restore it"
        )
    metadata = aim_source.load_metadata(snapshot_dir)
    assert metadata is not None
    return snapshot_dir, metadata


def _parse_aim_locked(config: Config) -> int:
    try:
        manifest = SourceManifest.load(config.manifest_path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    state = manifest.sources[aim_source.SOURCE_NAME]
    snapshot = _aim_snapshot(config, state)
    if isinstance(snapshot, str):
        print(f"error: {snapshot}", file=sys.stderr)
        return EXIT_ERROR
    snapshot_dir, metadata = snapshot
    pinned = {
        "edition_label": state.edition_label,
        "effective_date": state.effective_date,
        "change": state.change,
        "index_url": state.source_url,
    }
    for key, value in pinned.items():
        if value is None or metadata.get(key) != value:
            print(
                f"error: snapshot metadata {key} {metadata.get(key)!r} does not match the "
                f"manifest ({value!r}); run `far-aim fetch aim` to re-accept the edition",
                file=sys.stderr,
            )
            return EXIT_ERROR
    source = {
        "provider": "faa",
        "publication": "aim",
        "source_version": state.accepted_version,
        "edition_label": state.edition_label,
        "effective_date": state.effective_date,
        "change": state.change,
        "url": state.source_url,
        "retrieved_at": metadata["retrieved_at"],
        "raw_checksum": state.raw_hash,
    }
    _recover_normalized_layer(config, state, AIM_SPEC)
    try:
        docs = aim_parser.build_aim_docs(snapshot_dir, metadata, source)
    except (aim_parser.ParseError, UnicodeDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("note: nothing was written; last known-good output preserved.", file=sys.stderr)
        return EXIT_ERROR
    chapters = sum(
        1 for d in docs.values() if d["document_type"] == aim_model.DOCUMENT_TYPE_CHAPTER
    )
    appendices = sum(
        1 for d in docs.values() if d["document_type"] == aim_model.DOCUMENT_TYPE_APPENDIX
    )
    sections = sum(aim_parser.count_sections(d) for d in docs.values())
    paragraphs = sum(aim_parser.count_paragraphs(d) for d in docs.values())
    summary = (
        f"parsed AIM {state.accepted_version}: {chapters} chapters, {sections} sections, "
        f"{paragraphs} paragraphs, {appendices} appendices"
    )
    return _publish_normalized_layer(config, manifest, docs, AIM_SPEC, summary)


def cmd_parse_pcg(config: Config) -> int:
    """Parse the accepted PCG snapshot into canonical letter/publication JSON (Phase 5)."""
    try:
        with exclusive_lock(fetch_lock_path(config)):
            return _parse_pcg_locked(config)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: filesystem failure during parse: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _pcg_snapshot(config: Config, state: SourceState) -> tuple[Path, dict] | str:
    """The verified accepted PCG snapshot directory and its metadata, or a defect."""
    if state.accepted_version is None or state.raw_hash is None:
        return "no accepted PCG snapshot; run `far-aim fetch pcg` first"
    snapshot_dir = config.raw_dir / "pcg" / state.accepted_version
    if not snapshot_dir.is_dir():
        return f"archived snapshot missing: {snapshot_dir}; run `far-aim fetch pcg`"
    if not pcg_source.verify_snapshot(
        snapshot_dir, version=state.accepted_version, raw_hash=state.raw_hash
    ):
        return (
            f"archived snapshot {snapshot_dir} is incomplete or does not match the accepted "
            "checksum; run `far-aim fetch pcg` to restore it"
        )
    metadata = pcg_source.load_metadata(snapshot_dir)
    assert metadata is not None
    return snapshot_dir, metadata


def _parse_pcg_locked(config: Config) -> int:
    try:
        manifest = SourceManifest.load(config.manifest_path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    state = manifest.sources[pcg_source.SOURCE_NAME]
    snapshot = _pcg_snapshot(config, state)
    if isinstance(snapshot, str):
        print(f"error: {snapshot}", file=sys.stderr)
        return EXIT_ERROR
    snapshot_dir, metadata = snapshot
    pinned = {
        "edition_label": state.edition_label,
        "effective_date": state.effective_date,
        "change": state.change,
        "index_url": state.source_url,
    }
    for key, value in pinned.items():
        if value is None or metadata.get(key) != value:
            print(
                f"error: snapshot metadata {key} {metadata.get(key)!r} does not match the "
                f"manifest ({value!r}); run `far-aim fetch pcg` to re-accept the edition",
                file=sys.stderr,
            )
            return EXIT_ERROR
    source = {
        "provider": "faa",
        "publication": "pcg",
        "source_version": state.accepted_version,
        "edition_label": state.edition_label,
        "effective_date": state.effective_date,
        "change": state.change,
        "url": state.source_url,
        "retrieved_at": metadata["retrieved_at"],
        "raw_checksum": state.raw_hash,
    }
    _recover_normalized_layer(config, state, PCG_SPEC)
    try:
        docs = pcg_parser.build_pcg_docs(snapshot_dir, metadata, source)
    except (pcg_parser.ParseError, UnicodeDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("note: nothing was written; last known-good output preserved.", file=sys.stderr)
        return EXIT_ERROR
    letters = sum(
        1 for d in docs.values() if d["document_type"] == pcg_model.DOCUMENT_TYPE_LETTER
    )
    terms = sum(pcg_parser.count_terms(d) for d in docs.values())
    summary = f"parsed PCG {state.accepted_version}: {letters} letters, {terms} terms"
    return _publish_normalized_layer(config, manifest, docs, PCG_SPEC, summary)


# ---------------------------------------------------------------------------
# Normalized-layer publication (all corpora)
# ---------------------------------------------------------------------------


def _publish_normalized_parts(
    config: Config, state: SourceState, docs: dict[str, dict]
) -> int:
    """Partial eCFR parse: update the requested part files as one on-disk unit.

    The existing layer is hard-link-copied into staging (cheap: new
    directory entries sharing the same file contents), the requested part
    files are rewritten there, and the staged copy is swapped into place
    with the same two renames as a full publish. A write failure, crash,
    kill, or ``KeyboardInterrupt`` at any point therefore leaves either the
    old layer or the fully-updated one at the canonical path — never a mix
    — and every interruption window is repaired by the standard recovery
    on the next run (in-memory backups would not survive a kill).
    """
    spec = ECFR_SPEC
    version = state.accepted_version
    out_dir = spec.out_dir(config)
    staging = spec.staging_dir(config)
    previous = spec.previous_dir(config)
    # An interrupted full publish may have left the committed layer
    # displaced; writing partial files into a fresh/uncommitted `ecfr`
    # would strand it, so run the same recovery the full path performs.
    _recover_normalized_layer(config, state, spec)
    if out_dir.exists():
        # The untouched parts are carried over from the existing layer, so
        # that layer must have been built from the *accepted* snapshot.
        # After a fetch accepts a newer snapshot (which clears the recorded
        # canonical_hash), the old layer is stale: merging one new-version
        # part into it would publish a mixed-provenance corpus as success.
        stale = _stale_layer_defect(out_dir, state, spec)
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
        fsync_dir(staging)
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
        staged_hash = _layer_title_hash(staging, state, spec)
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
        fsync_dir(config.normalized_dir)
    except OSError:
        _restore_previous_layer(
            config, out_dir, staging, previous, had_previous=had_previous
        )
        raise
    return had_previous


def _discard_fallback(config: Config, fallback: Path) -> None:
    """Remove a superseded ``.<layer>-previous`` and make the removal durable.

    Resurrected by a power loss, a stale fallback can later win recovery
    arbitration and displace a newer committed layer: with no full parse yet
    ``canonical_hash`` was never recorded, and a subsequent fetch accepting a
    newer snapshot *clears* it — in both states recovery reinstates
    ``previous`` unconditionally.
    """
    shutil.rmtree(fallback)
    fsync_dir(config.normalized_dir)


def _recover_normalized_layer(config: Config, state: SourceState, spec: LayerSpec) -> None:
    """Repair the aftermath of an interrupted earlier publish.

    An interrupted publish may have left the manifest's layer displaced
    under ``.<layer>-previous``; every publish path must reconcile that
    before touching the canonical directory.
    """
    out_dir = spec.out_dir(config)
    staging = spec.staging_dir(config)
    previous = spec.previous_dir(config)
    # Staging never became authoritative and is always safe to discard.
    if staging.exists():
        shutil.rmtree(staging)
    if not previous.exists():
        return
    if not out_dir.exists():
        # Crash between the two swap renames: `previous` is the sole copy.
        os.replace(previous, out_dir)
        fsync_dir(config.normalized_dir)
        print(f"note: restored interrupted normalized layer from {previous}")
    elif state.canonical_hash is None or (
        _layer_title_hash(previous, state, spec) == state.canonical_hash
        and _layer_title_hash(out_dir, state, spec) != state.canonical_hash
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
        fsync_dir(config.normalized_dir)
        shutil.rmtree(staging)
        print(
            "note: reinstated the previous normalized layer displaced by an "
            "interrupted publish"
        )
    elif _layer_title_hash(out_dir, state, spec) == state.canonical_hash:
        # `out_dir` verifiably matches the manifest: `previous` is
        # superseded and safe to discard.
        _discard_fallback(config, previous)
    else:
        # Neither layer verifies against the manifest (corruption, or an
        # unreadable part file). Destructive arbitration is impossible, so
        # fail closed keeping both copies for inspection (plan §32.13).
        raise FetchError(
            f"neither the normalized layer at {out_dir} nor the displaced copy at "
            f"{previous} verifies against the manifest; refusing to discard either. "
            f"Inspect them, remove the corrupt copy, then re-run `far-aim {spec.parse_command}`."
        )


def _publish_normalized_layer(
    config: Config,
    manifest: SourceManifest,
    docs: dict[str, dict],
    spec: LayerSpec,
    summary: str,
) -> int:
    """Full parse: replace the whole normalized layer of one corpus as one unit.

    The new layer is staged in a sibling directory and swapped in with two
    renames, so a failure mid-write (full disk, interruption) can never
    leave a mix of old and new files, and documents removed or renumbered
    upstream cannot survive as stale files. Every failure path puts the
    last known-good layer back at its canonical path (plan §32.13):

    - a crash between the two swap renames leaves the old layer at
      ``.<layer>-previous`` with the canonical directory absent — the next
      publish restores it before doing anything else, never discards it;
    - a crash between the swap and the manifest commit leaves an
      *uncommitted* layer at the canonical path while ``previous`` still
      holds the layer the manifest records — the next publish reconciles
      both against the recorded hash and reinstates the committed one as
      the fallback rather than discarding it;
    - a failed swap restores ``previous`` in-process;
    - a failed manifest commit rolls the swap back — unless the on-disk
      manifest shows the commit actually became visible before the failure
      (e.g. the rename landed but its directory fsync did not), in which
      case rolling back would *create* an inconsistency and the new layer
      is kept.
    """
    state = manifest.sources[spec.source_name]
    out_dir = spec.out_dir(config)
    staging = spec.staging_dir(config)
    previous = spec.previous_dir(config)
    _recover_normalized_layer(config, state, spec)

    staging.mkdir(parents=True)
    try:
        for key, doc in docs.items():
            write_json_atomic(staging / spec.filename(key), doc)
        # The file contents are fsynced individually, but their directory
        # entries live in `staging` itself: sync it before the rename makes
        # it authoritative, or a power loss after the manifest commit could
        # leave a committed layer with unpersisted files.
        fsync_dir(staging)
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
        {key: doc["canonical_hash"] for key, doc in docs.items()}
    )
    print(summary)
    print(f"published normalized layer: {out_dir}")
    if state.canonical_hash != title_hash:
        # Record the canonical-layer hash — the content hash over all
        # document hashes — so a markup-only upstream change is detectable
        # as content-identical (plan §14.4). Committed after the swap so the
        # manifest never references a layer that is not on disk.
        previous_hash = state.canonical_hash
        try:
            state.canonical_hash = title_hash
            manifest.save(config.manifest_path)
        except (OSError, ManifestError) as exc:
            if _manifest_records_canonical(config.manifest_path, title_hash, spec):
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
        fsync_dir(config.normalized_dir)


def _first_document_defect(node: object, state: SourceState | None, spec: LayerSpec) -> str | None:
    """First integrity defect in a canonical document tree, or None.

    Walks the root document and every nested hashed document. Each must
    carry a string ``canonical_hash`` that recomputes from its own content
    (keyed on ``document_type``, not on the presence of a hash key, so a
    *deleted* hash is a defect rather than a skipped check — parent hashes
    deliberately exclude nested hashes, so nothing else would notice), and
    — when ``state`` is given — a ``source`` block matching the manifest's
    accepted snapshot, so missing or false provenance cannot pass the gate
    (plan §32.8).
    """
    if isinstance(node, dict):
        if node.get("document_type") in spec.hashed_types:
            doc_id = str(node.get("id", "<unidentified document>"))
            stored = node.get("canonical_hash")
            if not isinstance(stored, str):
                return f"{doc_id!r} has no stored canonical_hash"
            if cfr_model.canonical_hash(node) != stored:
                return f"stored canonical_hash of {doc_id!r} does not match its content"
            if state is not None:
                source = node.get("source")
                if not isinstance(source, dict):
                    return f"{doc_id!r}: missing source block"
                source_defect = spec.source_defect(node, source, state)
                if source_defect is not None:
                    return f"{doc_id!r}: {source_defect}"
        for value in node.values():
            if isinstance(value, list):
                for item in value:
                    bad = _first_document_defect(item, state, spec)
                    if bad is not None:
                        return bad
    return None


def _stale_layer_defect(layer_dir: Path, state: SourceState, spec: LayerSpec) -> str | None:
    """Why an on-disk layer was not built from the accepted snapshot.

    Deep-walks every file the same way ``validate`` does — root *and*
    nested hashed documents must verify their canonical hashes and carry
    provenance matching the manifest's accepted version/checksum. Returns
    None when the whole layer verifies.
    """
    for doc_file in sorted(layer_dir.glob(spec.file_glob)):
        try:
            doc = json.loads(doc_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"{doc_file.name}: unreadable ({exc})"
        if not isinstance(doc, dict):
            return f"{doc_file.name}: not a JSON object"
        defect = _first_document_defect(doc, state, spec)
        if defect is not None:
            return f"{doc_file.name}: {defect}"
    return None


def _layer_title_hash(layer_dir: Path, state: SourceState, spec: LayerSpec) -> str | None:
    """Title hash of a normalized layer, deeply verified.

    Used to identify which of two on-disk layers the manifest refers to
    during crash recovery — a decision that may *discard* the other layer,
    so stored hashes are not trusted: every document's hash is recomputed
    from its content, and every document's provenance must describe the
    manifest's accepted snapshot. Returns None when the layer is
    unreadable, malformed, or fails deep verification (never matches).
    """
    hashes: dict[str, str] = {}
    for doc_file in sorted(layer_dir.glob(spec.file_glob)):
        try:
            doc = json.loads(doc_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        stored = doc.get("canonical_hash")
        # The type-keyed walker would skip a root whose document_type was
        # stripped, leaving its stored hash untrusted-but-used; require it.
        if doc.get("document_type") not in spec.root_types:
            return None
        key = spec.doc_key(doc)
        if key is None or not isinstance(stored, str):
            return None
        # Mirror `validate`: a valid document under the wrong filename, or
        # the same document in two files, must disqualify the layer here
        # too — otherwise dict assignment would silently collapse the
        # duplicate and a layer that `validate` rejects could still win
        # destructive recovery arbitration.
        if doc_file.name != spec.filename(key) or key in hashes:
            return None
        if _first_document_defect(doc, state, spec) is not None:
            return None
        hashes[key] = stored
    if not hashes:
        return None
    return cfr_model.canonical_hash(hashes)


def _manifest_records_canonical(manifest_path: Path, title_hash: str, spec: LayerSpec) -> bool:
    """True if the on-disk manifest already records ``title_hash`` for the corpus."""
    try:
        state = SourceManifest.load(manifest_path).sources[spec.source_name]
    except Exception:  # noqa: BLE001 - unreadable manifest: cannot prove the commit
        return False
    return state.canonical_hash == title_hash


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


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def cmd_validate(config: Config) -> int:
    """Validate manifest and normalized layers under the shared source lock.

    Without the lock, a concurrent full parse could commit the manifest
    between the manifest read and the layer walk (or swap the layer between
    the two), making a perfectly valid publication look like a mismatch.
    """
    path = config.manifest_path
    if not path.exists():
        print(f"error: source registry missing: {path}", file=sys.stderr)
        return EXIT_ERROR
    try:
        with exclusive_lock(fetch_lock_path(config)):
            return _validate_locked(config)
    except FetchError as exc:
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
    for spec in (ECFR_SPEC, AIM_SPEC, PCG_SPEC):
        code = _validate_normalized_layer(config, manifest, spec)
        if code != EXIT_OK:
            return code
    return _validate_vault(config, manifest)


def _load_verified_docs(
    config: Config, state: SourceState, spec: LayerSpec
) -> dict[str, dict] | str:
    """Deep-verified canonical documents by layer key, or a defect string.

    Shared by ``validate`` and ``build-vault`` (plan §17.2/.3): every file's
    stored ``canonical_hash`` must recompute from its own content — the root
    AND each nested hashed document, since nested hashes are excluded from
    their parent's hash — with provenance matching the accepted snapshot,
    the filename matching the document it contains, each document appearing
    exactly once, and the layer hash over all root hashes matching the
    manifest. Tampered, truncated, or stale normalized data fails loudly
    instead of flowing into vault generation.
    """
    recorded = state.canonical_hash
    out_dir = spec.out_dir(config)
    doc_files = sorted(out_dir.glob(spec.file_glob)) if out_dir.is_dir() else []
    if not doc_files:
        return (
            f"manifest records canonical_hash but {out_dir} has no {spec.file_noun}; "
            f"re-run `far-aim {spec.parse_command}`"
        )
    docs: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    for doc_file in doc_files:
        try:
            doc = json.loads(doc_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"cannot read {doc_file}: {exc}"
        if not isinstance(doc, dict):
            return f"{doc_file}: not a JSON object (found {type(doc).__name__})"
        stored = doc.get("canonical_hash")
        # The defect walker is keyed on document_type, so a root object
        # whose type was stripped or mangled would skip its own hash check
        # while its stored hash still feeds the title hash; the file's root
        # must be a root document.
        if doc.get("document_type") not in spec.root_types:
            allowed = sorted(spec.root_types)
            expected = repr(allowed[0]) if len(allowed) == 1 else f"one of {allowed}"
            return (
                f"{doc_file}: root document_type {doc.get('document_type')!r} "
                f"is not {expected}"
            )
        bad = _first_document_defect(doc, state, spec)
        if bad is not None:
            return f"{doc_file}: {bad}"
        key = spec.doc_key(doc)
        expected_name = spec.filename(key) if key is not None else None
        if doc_file.name != expected_name:
            return (
                f"{doc_file}: contains {spec.key_noun} {key!r} "
                f"(expected filename {expected_name!r})"
            )
        if key in hashes:
            return f"{spec.key_noun} {key!r} appears in more than one file"
        hashes[key] = stored
        docs[key] = doc
    title_hash = cfr_model.canonical_hash(hashes)
    if title_hash != recorded:
        return (
            f"normalized {spec.label} layer hashes to {title_hash} but the manifest "
            f"records {recorded}; re-run `far-aim {spec.parse_command}`"
        )
    return docs


def _validate_normalized_layer(config: Config, manifest: SourceManifest, spec: LayerSpec) -> int:
    state = manifest.sources[spec.source_name]
    recorded = state.canonical_hash
    out_dir = spec.out_dir(config)
    doc_files = sorted(out_dir.glob(spec.file_glob)) if out_dir.is_dir() else []
    if recorded is None and not doc_files:
        print(f"note: no normalized {spec.label} data yet; run `far-aim {spec.parse_command}`.")
        return EXIT_OK
    if recorded is None:
        print(
            f"error: normalized {spec.label} files exist but the manifest records no "
            f"canonical_hash; re-run `far-aim {spec.parse_command}`"
            + (" (full title)" if spec is ECFR_SPEC else ""),
            file=sys.stderr,
        )
        return EXIT_ERROR
    result = _load_verified_docs(config, state, spec)
    if isinstance(result, str):
        print(f"error: {result}", file=sys.stderr)
        return EXIT_ERROR
    if spec is ECFR_SPEC:
        print(f"ok: normalized eCFR layer verified ({len(result)} parts, {recorded})")
    else:
        print(f"ok: normalized {spec.label} layer verified ({len(result)} documents, {recorded})")
    return EXIT_OK


def _aim_layer(config: Config, manifest: SourceManifest) -> generate_build.AimLayer | str | None:
    """The verified AIM layer with its figure bytes; None when AIM is not parsed yet.

    Figure bytes come from the archived raw snapshot when it is present and
    verifies; otherwise from the assets already in the vault. Either way each
    file must hash to the checksum the canonical layer recorded for it.
    """
    state = manifest.sources[aim_source.SOURCE_NAME]
    if state.accepted_version is None or state.canonical_hash is None:
        return None
    docs = _load_verified_docs(config, state, AIM_SPEC)
    if isinstance(docs, str):
        return docs
    required = generate_build.aim_asset_hashes(docs)
    snapshot_figures = config.raw_dir / "aim" / state.accepted_version / aim_source.FIGURES_DIR
    vault_assets = config.vault_dir / generate_aim_notes.AIM_DIR / generate_aim_notes.ASSETS_DIR
    assets: dict[str, bytes] = {}
    for name, sha in sorted(required.items()):
        data: bytes | None = None
        for candidate in (snapshot_figures / name, vault_assets / name):
            try:
                content = candidate.read_bytes()
            except OSError:
                continue
            if aim_source.sha256_of_bytes(content) == sha:
                data = content
                break
        if data is None:
            return (
                f"AIM figure {name!r} ({sha}) is not available: neither the archived "
                f"snapshot at {snapshot_figures} nor the vault holds it; run `far-aim fetch aim`"
            )
        assets[name] = data
    return generate_build.AimLayer(docs=docs, title_hash=state.canonical_hash, assets=assets)


def _vault_has_generated_aim(config: Config) -> bool:
    """True when generator-owned AIM output (notes or figure assets) is on disk."""
    vault_aim = config.vault_dir / generate_aim_notes.AIM_DIR
    if not vault_aim.is_dir():
        return False
    assets = vault_aim / generate_aim_notes.ASSETS_DIR
    if generate_build.read_asset_ledger(assets / generate_build.ASSET_LEDGER):
        return True
    return any(generate_build.is_generated_note(p) for p in sorted(vault_aim.rglob("*.md")))


def _aim_layer_pending_defect(config: Config, manifest: SourceManifest) -> str | None:
    """Why the vault's AIM output cannot be reconciled with the manifest right now.

    After ``fetch aim`` accepts a newer edition the manifest's AIM
    ``canonical_hash`` is cleared until ``parse aim`` publishes the new
    layer. In that window a FAR-only build would treat every generated AIM
    note and figure as stale and delete the last known-good AIM output
    (plan §32.13) — so building and validating refuse instead.
    """
    if not _vault_has_generated_aim(config):
        return None
    state = manifest.sources[aim_source.SOURCE_NAME]
    if state.canonical_hash is not None:
        return None
    if state.accepted_version is not None:
        return (
            f"the vault holds generated AIM notes but the accepted AIM edition "
            f"{state.accepted_version} has not been parsed yet; run `far-aim parse aim` "
            "first (building now would delete the existing AIM notes)"
        )
    return (
        "the vault holds generated AIM notes but the manifest records no accepted AIM "
        "snapshot; run `far-aim fetch aim` and `far-aim parse aim`, or remove "
        f"{config.vault_dir / generate_aim_notes.AIM_DIR} deliberately"
    )


def _pcg_layer(config: Config, manifest: SourceManifest) -> generate_build.PcgLayer | str | None:
    """The verified PCG layer; None when the PCG is not parsed yet."""
    state = manifest.sources[pcg_source.SOURCE_NAME]
    if state.accepted_version is None or state.canonical_hash is None:
        return None
    docs = _load_verified_docs(config, state, PCG_SPEC)
    if isinstance(docs, str):
        return docs
    try:
        gate = glossary.Gate.load(config.pcg_gate_path)
    except BuildError as exc:
        return str(exc)
    return generate_build.PcgLayer(docs=docs, title_hash=state.canonical_hash, gate=gate)


def _vault_has_generated_pcg(config: Config) -> bool:
    """True when generator-owned PCG notes are on disk."""
    vault_pcg = config.vault_dir / generate_pcg_notes.PCG_DIR
    if not vault_pcg.is_dir():
        return False
    return any(generate_build.is_generated_note(p) for p in sorted(vault_pcg.rglob("*.md")))


def _pcg_layer_pending_defect(config: Config, manifest: SourceManifest) -> str | None:
    """Why the vault's PCG output cannot be reconciled with the manifest right now.

    Same rule as the AIM (plan §32.13): between ``fetch pcg`` accepting a
    newer edition and ``parse pcg`` publishing it, a build would delete the
    last known-good PCG notes as stale — refuse instead.
    """
    if not _vault_has_generated_pcg(config):
        return None
    state = manifest.sources[pcg_source.SOURCE_NAME]
    if state.canonical_hash is not None:
        return None
    if state.accepted_version is not None:
        return (
            f"the vault holds generated PCG notes but the accepted PCG edition "
            f"{state.accepted_version} has not been parsed yet; run `far-aim parse pcg` "
            "first (building now would delete the existing PCG notes)"
        )
    return (
        "the vault holds generated PCG notes but the manifest records no accepted PCG "
        "snapshot; run `far-aim fetch pcg` and `far-aim parse pcg`, or remove "
        f"{config.vault_dir / generate_pcg_notes.PCG_DIR} deliberately"
    )


def _validate_vault(config: Config, manifest: SourceManifest) -> int:
    """Verify the generated vault byte-matches the canonical layers.

    Re-renders the whole plan in memory and compares against disk, making
    "rebuild with unchanged sources produces zero diff" (plan §22) a
    scripted check without mutating anything. Curated notes inside the
    generated tree are ignored — only generator-owned files are compared.
    """
    state = manifest.sources[ecfr.SOURCE_NAME]
    vault_far = config.vault_dir / generate_notes.FAR_DIR
    vault_aim = config.vault_dir / generate_aim_notes.AIM_DIR
    vault_pcg = config.vault_dir / generate_pcg_notes.PCG_DIR
    status_path = config.vault_dir / f"{generate_notes.SOURCE_STATUS_STEM}.md"
    home_path = config.vault_dir / f"{generate_notes.HOME_STEM}.md"
    # A vault "exists" only if any generator-owned note does. Directory
    # presence alone proves nothing: `vault/FAR/` holding only curated notes
    # is a never-built vault (curated notes are ignored, not validated),
    # while a generated Source Status.md or Home.md with the FAR tree
    # deleted is a damaged build that must fail the missing-note checks
    # below, not pass as "nothing to check".
    built = any(
        generate_build.is_generated_note(p) for p in (status_path, home_path)
    ) or any(
        generate_build.is_generated_note(p)
        for root in (vault_far, vault_aim, vault_pcg)
        if root.is_dir()
        for p in sorted(root.rglob("*.md"))
    )
    if not built:
        print("note: no generated vault yet; run `far-aim build-vault`.")
        return EXIT_OK
    if state.accepted_version is None or state.canonical_hash is None:
        print(
            "error: vault exists but the manifest records no accepted eCFR "
            "canonical layer; re-run `far-aim parse ecfr` and `far-aim build-vault`",
            file=sys.stderr,
        )
        return EXIT_ERROR
    docs = _load_verified_docs(config, state, ECFR_SPEC)
    if isinstance(docs, str):
        print(f"error: {docs}", file=sys.stderr)
        return EXIT_ERROR
    for pending in (
        _aim_layer_pending_defect(config, manifest),
        _pcg_layer_pending_defect(config, manifest),
    ):
        if pending is not None:
            print(f"error: {pending}", file=sys.stderr)
            return EXIT_ERROR
    aim = _aim_layer(config, manifest)
    if isinstance(aim, str):
        print(f"error: {aim}", file=sys.stderr)
        return EXIT_ERROR
    pcg = _pcg_layer(config, manifest)
    if isinstance(pcg, str):
        print(f"error: {pcg}", file=sys.stderr)
        return EXIT_ERROR
    try:
        plan = generate_build.plan_vault(
            docs, state.accepted_version, state.canonical_hash, manifest.sources, aim, pcg
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    planned = {config.vault_dir.joinpath(*parts): data for parts, data in plan.items()}
    for path, data in sorted(planned.items()):
        if not path.exists():
            print(f"error: vault is missing generated note {path}", file=sys.stderr)
            return EXIT_ERROR
        if path.read_bytes() != data:
            print(
                f"error: vault note {path} differs from the canonical layer; "
                "re-run `far-aim build-vault`",
                file=sys.stderr,
            )
            return EXIT_ERROR
    on_disk: list[Path] = []
    for root in (vault_far, vault_aim, vault_pcg):
        if root.is_dir():
            on_disk.extend(sorted(root.rglob("*.md")))
    if status_path.exists():
        on_disk.append(status_path)
    if home_path.exists():
        on_disk.append(home_path)
    assets_root = vault_aim / generate_aim_notes.ASSETS_DIR
    ledger = generate_build.read_asset_ledger(assets_root / generate_build.ASSET_LEDGER)
    if assets_root.is_dir():
        # Only assets the ledger attributes to the generator can be stale;
        # anything else in the directory is curated and ignored here.
        on_disk.extend(
            p
            for p in sorted(assets_root.iterdir())
            if p.is_file() and p.suffix != ".md" and p.name in ledger
        )
    for path in on_disk:
        if path in planned:
            continue
        if (path.parent == assets_root and path.suffix != ".md") or (
            generate_build.is_generated_note(path)
        ):
            print(
                f"error: stale generated note {path} not produced by the "
                "canonical layer; re-run `far-aim build-vault`",
                file=sys.stderr,
            )
            return EXIT_ERROR
    print(f"ok: vault matches canonical layer ({len(planned)} notes)")
    return EXIT_OK


# ---------------------------------------------------------------------------
# build-vault
# ---------------------------------------------------------------------------


def cmd_build_vault(config: Config) -> int:
    """Generate the Obsidian vault from the verified canonical layers (Phase 3/4).

    Runs under the shared source lock so a concurrent parse cannot swap a
    normalized layer between verification and rendering. The whole vault is
    planned and verified in memory before any file is written; curated notes
    are never overwritten (plan §32.5, §32.13).
    """
    path = config.manifest_path
    if not path.exists():
        print(f"error: source registry missing: {path}", file=sys.stderr)
        return EXIT_ERROR
    try:
        with exclusive_lock(fetch_lock_path(config)):
            return _build_vault_locked(config)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: filesystem failure during build-vault: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _build_vault_locked(config: Config) -> int:
    try:
        manifest = SourceManifest.load(config.manifest_path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    state = manifest.sources[ecfr.SOURCE_NAME]
    if state.accepted_version is None or state.canonical_hash is None:
        print(
            "error: no accepted eCFR canonical layer; run `far-aim fetch ecfr` "
            "and `far-aim parse ecfr` first",
            file=sys.stderr,
        )
        return EXIT_ERROR
    docs = _load_verified_docs(config, state, ECFR_SPEC)
    if isinstance(docs, str):
        print(f"error: {docs}", file=sys.stderr)
        return EXIT_ERROR
    for pending in (
        _aim_layer_pending_defect(config, manifest),
        _pcg_layer_pending_defect(config, manifest),
    ):
        if pending is not None:
            print(f"error: {pending}", file=sys.stderr)
            return EXIT_ERROR
    aim = _aim_layer(config, manifest)
    if isinstance(aim, str):
        print(f"error: {aim}", file=sys.stderr)
        return EXIT_ERROR
    pcg = _pcg_layer(config, manifest)
    if isinstance(pcg, str):
        print(f"error: {pcg}", file=sys.stderr)
        return EXIT_ERROR
    if aim is None:
        print("note: no accepted AIM canonical layer; the vault will not cover the AIM.")
    if pcg is None:
        print("note: no accepted PCG canonical layer; the vault will not cover the PCG.")
    try:
        stats = generate_build.build_vault(
            config, state.accepted_version, state.canonical_hash, manifest.sources, docs, aim, pcg
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    for warning in stats.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    total = stats.written + stats.unchanged
    print(
        f"ok: vault generated ({total} notes — {stats.written} written, "
        f"{stats.unchanged} unchanged, {stats.deleted} stale deleted)"
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


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
    if args.command == "fetch" and args.corpus == "aim":
        return cmd_fetch_aim(config, force=args.force)
    if args.command == "fetch" and args.corpus == "pcg":
        return cmd_fetch_pcg(config, force=args.force)
    if args.command == "parse" and args.corpus == "ecfr":
        return cmd_parse_ecfr(config, args.parts)
    if args.command == "parse" and args.corpus in ("aim", "pcg"):
        if args.parts:
            print("error: --part applies to `parse ecfr` only", file=sys.stderr)
            return EXIT_ERROR
        return cmd_parse_aim(config) if args.corpus == "aim" else cmd_parse_pcg(config)
    if args.command == "build-vault":
        return cmd_build_vault(config)

    corpus = getattr(args, "corpus", None)
    phase = NOT_IMPLEMENTED_PHASE[(args.command, corpus)]
    target = f"{args.command} {corpus}" if corpus else args.command
    print(f"far-aim {target}: not implemented yet — planned for {phase}.", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
