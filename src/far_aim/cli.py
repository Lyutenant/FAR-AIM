"""Command-line interface for the far-aim pipeline (plan §18).

Implemented: `check`, `validate`, `fetch ecfr|aim|pcg`, `parse ecfr|aim|pcg`,
`enrich`, `diff`, `build-vault`, `update`. The remaining command is a
registered stub that exits with code 2 until its phase is implemented.

The normalized layer of every corpus is published, recovered and verified
by the same code, parametrized by a :class:`LayerSpec` (directory name,
manifest source, document types, filename rule, provenance check).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
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
from far_aim.generate import concept_notes as generate_concept_notes
from far_aim.generate import notes as generate_notes
from far_aim.generate import pcg_notes as generate_pcg_notes
from far_aim.generate.enrich import EnrichmentLayer
from far_aim.links import glossary, semantic
from far_aim.links.concepts import ConceptGraph
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
    sub.add_parser(
        "enrich",
        help="derive the related-links layer (data/enrichment/related.json) from the "
        "canonical FAR and AIM layers",
    )
    sub.add_parser("diff", help="structural diff between accepted source versions")
    sub.add_parser("build-vault", help="generate the Obsidian vault from canonical data")
    update = sub.add_parser(
        "update", help="run the full check→fetch→parse→enrich→diff→generate→validate sequence"
    )
    update.add_argument(
        "--reverify",
        action="store_true",
        help="when versions already match upstream, re-run the fetchers so accepted "
        "content is re-verified against the pinned raw hashes (used by CI, where the "
        "FAA can edit pages without bumping the edition)",
    )
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


def _definitions_gate(
    config: Config, docs: dict[str, dict]
) -> generate_build.definitions.DefinitionsGate | str | None:
    """The committed FAR definitions gate, required whenever the canonical
    layer holds a definitions source (§ 1.1, § 61.1 …); None otherwise."""
    if not generate_build.definitions.discover_sources(docs):
        return None
    try:
        return generate_build.definitions.DefinitionsGate.load(config.part1_gate_path)
    except BuildError as exc:
        return str(exc)


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


# ---------------------------------------------------------------------------
# enrichment layer (Phase 9, plan §36)
# ---------------------------------------------------------------------------

_CURATED_ROOTS = ("Collections", "Topics", "Study")


def _curated_notes(config: Config) -> dict[str, tuple[str, ...]]:
    """stem → vault-relative path parts of every curated note on disk."""
    out: dict[str, tuple[str, ...]] = {}
    for root_name in _CURATED_ROOTS:
        root = config.vault_dir / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            if generate_build.is_generated_note(path):
                continue
            out.setdefault(path.stem, path.relative_to(config.vault_dir).parts)
    return out


def _enrichment_layer(config: Config) -> EnrichmentLayer | str | None:
    """The committed enrichment layer, a defect message, or None when absent.

    ``data/enrichment/`` missing altogether means "no enrichment" — the
    vault is built without concept notes or derived links (plan §36.1). A
    present-but-broken file, or a related-links file without its review
    overlay, is a defect: silently building without curated decisions is
    exactly what §32.13 forbids. Staleness of ``related.json`` against the
    accepted layers is checked where the hashes are known (``plan_vault``).
    """
    if not config.enrichment_dir.is_dir():
        return None
    concepts: ConceptGraph | None = None
    related: semantic.RelatedIndex | None = None
    try:
        if config.concepts_path.exists():
            concepts = ConceptGraph.load(config.concepts_path)
        if config.related_path.exists():
            raw = semantic.RelatedIndex.load(config.related_path)
            review = semantic.Review.load(config.related_review_path)
            related = dataclasses.replace(raw, units=review.apply(raw.units))
    except BuildError as exc:
        return str(exc)
    if concepts is None and related is None:
        return None
    curated = _curated_notes(config) if concepts is not None else {}
    return EnrichmentLayer(concepts=concepts, related=related, curated_notes=curated)


def _vault_has_generated_concepts(config: Config) -> bool:
    root = config.vault_dir / generate_concept_notes.CONCEPTS_DIR
    if not root.is_dir():
        return False
    return any(generate_build.is_generated_note(p) for p in sorted(root.rglob("*.md")))


def cmd_enrich(config: Config) -> int:
    """Compute ``data/enrichment/related.json`` from the verified layers (plan §36.3).

    Deterministic and idempotent: identical inputs produce identical bytes,
    and the file is rewritten only when its content changes. The human
    review overlay is validated (unknown ids fail; stale denials warn) but
    never modified — except that a missing overlay is created empty, since
    a first ``enrich`` has nothing to review yet.
    """
    path = config.manifest_path
    if not path.exists():
        print(f"error: source registry missing: {path}", file=sys.stderr)
        return EXIT_ERROR
    try:
        with exclusive_lock(fetch_lock_path(config)):
            return _enrich_locked(config)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: filesystem failure during enrich: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _enrich_locked(config: Config) -> int:
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
    aim_state = manifest.sources[aim_source.SOURCE_NAME]
    aim_docs: dict[str, dict] | None = None
    if aim_state.accepted_version is not None and aim_state.canonical_hash is not None:
        loaded = _load_verified_docs(config, aim_state, AIM_SPEC)
        if isinstance(loaded, str):
            print(f"error: {loaded}", file=sys.stderr)
            return EXIT_ERROR
        aim_docs = loaded
    elif aim_state.accepted_version is not None:
        print(
            f"error: the accepted AIM edition {aim_state.accepted_version} has not been "
            "parsed yet; run `far-aim parse aim` first",
            file=sys.stderr,
        )
        return EXIT_ERROR
    else:
        print("note: no accepted AIM canonical layer; deriving FAR-only related links.")

    units = generate_build.collect_units(docs, aim_docs)
    related = semantic.compute_related(units)
    inputs = generate_build.related_inputs(state.canonical_hash, aim_state.canonical_hash)
    data = semantic.related_bytes(inputs, related)

    review_path = config.related_review_path
    if not review_path.exists():
        config.enrichment_dir.mkdir(parents=True, exist_ok=True)
        generate_build.write_text_atomic(review_path, _EMPTY_REVIEW)
        print(f"note: created empty review overlay {review_path}")
    try:
        review = semantic.Review.load(review_path)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    unknown = review.unknown_ids(unit.id for unit in units)
    if unknown:
        print(
            f"error: review overlay {review_path} names unknown unit id(s): "
            f"{', '.join(unknown)}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    for unit_id, target in review.stale(related):
        print(f"warning: review denial {unit_id} → {target} is stale (no longer suggested)")

    links = sum(len(v) for v in related.values())
    linked = sum(1 for v in related.values() if v)
    target = config.related_path
    if target.exists() and target.read_bytes() == data:
        print(
            f"ok: related links unchanged ({len(units)} units, {links} links from "
            f"{linked} units; {semantic.PROVIDER_ID} v{semantic.PROVIDER_VERSION})"
        )
        return EXIT_OK
    config.enrichment_dir.mkdir(parents=True, exist_ok=True)
    generate_build.write_text_atomic(target, data)
    print(
        f"ok: related links written to {target} ({len(units)} units, {links} links from "
        f"{linked} units; {semantic.PROVIDER_ID} v{semantic.PROVIDER_VERSION})"
    )
    return EXIT_OK


_EMPTY_REVIEW = (
    json.dumps(
        {
            "_comment": "Human review overlay for the derived related links (plan §36.3). "
            "Each deny entry suppresses one suggestion: unit and target are canonical ids "
            "(cfr-14-91.155, aim-3-1-4); reason is required.",
            "deny": [],
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n"
).encode("utf-8")


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
    vault_concepts = config.vault_dir / generate_concept_notes.CONCEPTS_DIR
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
        for root in (vault_far, vault_aim, vault_pcg, vault_concepts)
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
    planned = _plan_generated_vault(config, manifest, state)
    if isinstance(planned, str):
        print(f"error: {planned}", file=sys.stderr)
        return EXIT_ERROR
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
    for path in _stale_generated_on_disk(config, planned):
        print(
            f"error: stale generated note {path} not produced by the "
            "canonical layer; re-run `far-aim build-vault`",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(f"ok: vault matches canonical layer ({len(planned)} notes)")
    return EXIT_OK


def _plan_generated_vault(
    config: Config, manifest: SourceManifest, state: SourceState
) -> dict[Path, bytes] | str:
    """The complete generated-vault plan as on-disk paths → bytes, or a defect.

    Shared by ``validate`` and ``diff``: loads and verifies every canonical
    layer, renders the whole plan in memory, and touches nothing.
    """
    docs = _load_verified_docs(config, state, ECFR_SPEC)
    if isinstance(docs, str):
        return docs
    for pending in (
        _aim_layer_pending_defect(config, manifest),
        _pcg_layer_pending_defect(config, manifest),
    ):
        if pending is not None:
            return pending
    aim = _aim_layer(config, manifest)
    if isinstance(aim, str):
        return aim
    pcg = _pcg_layer(config, manifest)
    if isinstance(pcg, str):
        return pcg
    enrichment = _enrichment_layer(config)
    if isinstance(enrichment, str):
        return enrichment
    definitions_gate = _definitions_gate(config, docs)
    if isinstance(definitions_gate, str):
        return definitions_gate
    try:
        plan = generate_build.plan_vault(
            docs,
            state.accepted_version,
            state.canonical_hash,
            manifest.sources,
            aim,
            pcg,
            enrichment,
            definitions_gate,
        )
    except BuildError as exc:
        return str(exc)
    return {config.vault_dir.joinpath(*parts): data for parts, data in plan.items()}


def _stale_generated_on_disk(config: Config, planned: dict[Path, bytes]) -> list[Path]:
    """Generator-owned files on disk that the plan no longer produces.

    Curated material is never listed: notes prove generator ownership by
    frontmatter, assets by the ledger (anything else in the assets
    directory is curated and ignored).
    """
    vault_aim = config.vault_dir / generate_aim_notes.AIM_DIR
    on_disk: list[Path] = []
    corpus_dirs = (
        generate_notes.FAR_DIR,
        generate_aim_notes.AIM_DIR,
        generate_pcg_notes.PCG_DIR,
        generate_concept_notes.CONCEPTS_DIR,
    )
    for root_name in corpus_dirs:
        root = config.vault_dir / root_name
        if root.is_dir():
            on_disk.extend(sorted(root.rglob("*.md")))
    for stem in (generate_notes.SOURCE_STATUS_STEM, generate_notes.HOME_STEM):
        note = config.vault_dir / f"{stem}.md"
        if note.exists():
            on_disk.append(note)
    assets_root = vault_aim / generate_aim_notes.ASSETS_DIR
    ledger = generate_build.read_asset_ledger(assets_root / generate_build.ASSET_LEDGER)
    if assets_root.is_dir():
        on_disk.extend(
            p
            for p in sorted(assets_root.iterdir())
            if p.is_file() and p.suffix != ".md" and p.name in ledger
        )
    return [
        path
        for path in on_disk
        if path not in planned
        and (
            (path.parent == assets_root and path.suffix != ".md")
            or generate_build.is_generated_note(path)
        )
    ]


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def cmd_diff(config: Config) -> int:
    """Structural diff: the on-disk generated vault vs a freshly planned one.

    Run between ``parse`` and ``build-vault`` (as ``far-aim update`` does),
    the vault on disk still reflects the previously accepted versions while
    the plan reflects the newly parsed layers — so this reports, one line
    per document (each category capped), exactly what the next build will
    do: notes to **add** (new documents), **rewrite** (content or
    provenance changes — a provenance-only issue bump rewrites every note
    of a corpus, which the cap keeps bounded and the update summary's
    "canonical content unchanged" explains), and **remove** (upstream
    removals — the changes a reviewer must see before publication; plan
    §32.7). Read-only; differences are reported, never an error.
    """
    path = config.manifest_path
    if not path.exists():
        print(f"error: source registry missing: {path}", file=sys.stderr)
        return EXIT_ERROR
    try:
        with exclusive_lock(fetch_lock_path(config)):
            return _diff_locked(config)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: filesystem failure during diff: {exc}", file=sys.stderr)
        return EXIT_ERROR


_DIFF_LIST_LIMIT = 50


def _diff_locked(config: Config) -> int:
    try:
        manifest = SourceManifest.load(config.manifest_path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    state = manifest.sources[ecfr.SOURCE_NAME]
    if state.accepted_version is None or state.canonical_hash is None:
        print(
            "error: no accepted eCFR canonical layer to diff against; run "
            "`far-aim parse ecfr` first",
            file=sys.stderr,
        )
        return EXIT_ERROR
    planned = _plan_generated_vault(config, manifest, state)
    if isinstance(planned, str):
        print(f"error: {planned}", file=sys.stderr)
        return EXIT_ERROR
    to_add = sorted(p for p in planned if not p.exists())
    to_rewrite = sorted(p for p in planned if p.exists() and p.read_bytes() != planned[p])
    to_remove = _stale_generated_on_disk(config, planned)

    def show(label: str, paths: list[Path]) -> None:
        for p in paths[:_DIFF_LIST_LIMIT]:
            print(f"  {label}: {p.relative_to(config.vault_dir)}")
        if len(paths) > _DIFF_LIST_LIMIT:
            print(f"  ... and {len(paths) - _DIFF_LIST_LIMIT} more to {label}")

    show("add", to_add)
    show("rewrite", to_rewrite)
    show("remove", to_remove)
    print(
        f"diff: {len(to_add)} to add, {len(to_rewrite)} to rewrite, "
        f"{len(to_remove)} to remove ({len(planned)} files planned)"
    )
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
    enrichment = _enrichment_layer(config)
    if isinstance(enrichment, str):
        print(f"error: {enrichment}", file=sys.stderr)
        return EXIT_ERROR
    if aim is None:
        print("note: no accepted AIM canonical layer; the vault will not cover the AIM.")
    if pcg is None:
        print("note: no accepted PCG canonical layer; the vault will not cover the PCG.")
    if enrichment is None:
        print("note: no enrichment layer (data/enrichment); no concept notes or derived links.")
    definitions_gate = _definitions_gate(config, docs)
    if isinstance(definitions_gate, str):
        print(f"error: {definitions_gate}", file=sys.stderr)
        return EXIT_ERROR
    try:
        stats = generate_build.build_vault(
            config,
            state.accepted_version,
            state.canonical_hash,
            manifest.sources,
            docs,
            aim,
            pcg,
            enrichment,
            definitions_gate,
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
# update (Phase 8)
# ---------------------------------------------------------------------------


def _update_resume_reason(config: Config, manifest: SourceManifest) -> str | None:
    """Why an up-to-date-looking manifest still needs the pipeline, if it does.

    Fetch accepts each source before parse/build run (clearing the paired
    ``canonical_hash``), so an ``update`` that died mid-pipeline leaves a
    persisted signal: an accepted source whose canonical hash is missing
    (parse never completed for the accepted raw), a ``Source Status.md``
    that no longer byte-matches the manifest's accepted state (the vault
    predates a version acceptance), or a corpus index note pinning a
    different canonical hash than the manifest records (a same-version
    correction was parsed but never published). None of these probes needs
    the local canonical layers, so a fresh checkout of a validated
    commit — CI's daily case — trips nothing.
    """
    pending = sorted(
        name
        for name, state in manifest.sources.items()
        if state.accepted_version is not None and state.canonical_hash is None
    )
    if pending:
        return f"the canonical layer is pending for: {', '.join(pending)}"
    status = generate_notes.build_source_status(manifest.sources)
    try:
        expected = generate_build.assemble_note(status)
    except BuildError as exc:
        return f"the source-status note cannot be rendered ({exc})"
    try:
        on_disk = config.vault_dir.joinpath(*status.path_parts).read_bytes()
    except OSError:
        return "the vault has no generated Source Status note"
    if on_disk != expected:
        return "the vault's Source Status note predates the accepted sources"
    # Source Status proves the vault followed version acceptances, but a
    # same-version correction (`fetch --force` + re-parse) moves only
    # hashes. Each corpus index note pins the canonical hash it was built
    # from — compare those against the manifest so a correction that was
    # parsed but never published still resumes.
    for source_name, index_parts in (
        (ecfr.SOURCE_NAME, (generate_notes.FAR_DIR, f"{generate_notes.TITLE_INDEX_STEM}.md")),
        (
            aim_source.SOURCE_NAME,
            (generate_aim_notes.AIM_DIR, f"{generate_aim_notes.AIM_INDEX_STEM}.md"),
        ),
        (
            pcg_source.SOURCE_NAME,
            (generate_pcg_notes.PCG_DIR, f"{generate_pcg_notes.PCG_INDEX_STEM}.md"),
        ),
    ):
        recorded = manifest.sources[source_name].canonical_hash
        if recorded is None:
            continue  # never parsed → no published corpus to compare
        pinned = _note_canonical_hash(config.vault_dir.joinpath(*index_parts))
        if pinned != recorded:
            return (
                f"the vault's {index_parts[-1]} was built from a different "
                "canonical layer than the manifest records"
            )
    # When the local canonical layers are on disk, guard the rest of the
    # vault with the full read-only validation (a sync killed mid-write, a
    # deleted or hand-damaged generated note). A fresh checkout of a
    # validated commit carries no local layers — they are gitignored and
    # reconstructible — and cannot be re-verified here, so it is trusted
    # as merged and the cheap probes above are the whole check.
    if _normalized_layers_present(config) and cmd_validate(config) != EXIT_OK:
        return "the published output failed validation"
    return None


def _note_canonical_hash(path: Path) -> str | None:
    """The ``canonical_hash`` a generated note's frontmatter pins, if readable."""
    prefix = 'canonical_hash: "'
    try:
        with path.open(encoding="utf-8") as fh:
            if fh.readline().rstrip("\n") != "---":
                return None
            for _ in range(64):
                line = fh.readline()
                if not line or line.rstrip("\n") == "---":
                    return None
                stripped = line.rstrip("\n")
                if stripped.startswith(prefix) and stripped.endswith('"'):
                    return stripped[len(prefix) : -1]
    except OSError:
        return None
    return None


def _normalized_layers_present(config: Config) -> bool:
    """True when any normalized canonical layer files exist locally."""
    for spec in (ECFR_SPEC, AIM_SPEC, PCG_SPEC):
        out_dir = spec.out_dir(config)
        if out_dir.is_dir() and any(out_dir.glob(spec.file_glob)):
            return True
    return False


def _manifest_without_volatile(manifest: SourceManifest) -> dict:
    """The manifest's content with the volatile ``last_checked_at`` removed."""
    data = manifest.to_dict()
    for entry in data["sources"].values():  # type: ignore[union-attr]
        if isinstance(entry, dict):
            entry.pop("last_checked_at", None)
    return data


def _reverify_accepted_content(config: Config, path: Path) -> int:
    """Re-run the fetchers so accepted content is re-verified (plan §25.5).

    With versions matching upstream, discovery alone cannot notice the
    documented FAA failure mode of editing pages without bumping the
    edition. The fetchers can: with the local archive present they verify
    it cheaply; without one (CI's fresh runner) they re-download and
    compare the tree hash against the pinned ``raw_hash``, quarantining
    and failing on any mismatch. Whether the run succeeds or fails, pure
    ``last_checked_at`` drift is restored afterwards (under the source
    lock) so a run that accepted no content leaves the manifest
    byte-identical — any other manifest delta (e.g. backfilled provenance
    pins, or a concurrent process's real acceptance) is kept, not
    restored.
    """
    before_bytes = path.read_bytes()
    code = EXIT_OK
    for label, step in (
        ("fetch ecfr", lambda: cmd_fetch_ecfr(config, force=False)),
        ("fetch aim", lambda: cmd_fetch_aim(config, force=False)),
        ("fetch pcg", lambda: cmd_fetch_pcg(config, force=False)),
    ):
        print(f"==> far-aim {label}")
        code = step()
        if code != EXIT_OK:
            break
    # Success or failure, drop pure last_checked_at drift: a run that
    # accepted no content must leave the manifest byte-identical (a fetch
    # that failed re-verification never saved, but an earlier source's
    # clean check did bump its timestamp). The reload/compare/restore is a
    # transaction under the shared source lock — a concurrent fetch may
    # have accepted real state meanwhile, and the restore must never
    # clobber it (any non-volatile delta is kept, not restored).
    try:
        with exclusive_lock(fetch_lock_path(config)):
            before = SourceManifest.from_dict(json.loads(before_bytes))
            after = SourceManifest.load(path)
            if _manifest_without_volatile(before) == _manifest_without_volatile(after):
                before.save(path)
    except (FetchError, ManifestError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: filesystem failure during re-verification: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if code != EXIT_OK:
        print(
            "error: re-verification failed: upstream content no longer matches an "
            "accepted edition; investigate (any quarantined download is kept), then "
            "re-accept deliberately with `far-aim fetch <source> --force`",
            file=sys.stderr,
        )
        return code
    print("update: accepted editions re-verified against upstream content")
    return EXIT_OK


def cmd_update(config: Config, *, reverify: bool = False) -> int:
    """Check upstream and, when a source changed, run the full pipeline.

    The automated-maintenance entry point (plan §22 Phase 8). Discovery and
    the rollback/provenance-pin guards run first; when every source is
    current *and* the published output is consistent with it, the command
    exits without touching anything — no manifest timestamp churn — so a
    scheduled no-change run leaves a clean tree. With ``reverify`` (CI's
    mode) the fetchers re-verify accepted content against the pinned raw
    hashes first — catching FAA edits that keep the edition label — while
    a clean match still leaves the committed state byte-identical
    (:func:`_reverify_accepted_content`). An earlier update that died
    between per-source acceptance and publication is detected
    (:func:`_update_resume_reason`) and resumed instead of stranded.
    Otherwise every source is fetched (unchanged ones re-verify, or rebuild
    the local raw cache in a fresh environment), every layer parsed, the
    vault rebuilt and validated, stopping at the first failing step so a
    fetch/parse/validation defect blocks publication and preserves the last
    known-good output (plan §32.13). The summary reports each source's
    version transition and whether canonical content actually changed — a
    new eCFR issue date with no Title 14 amendments is provenance-only
    (plan §32.6), though the vault still rewrites per-note provenance.
    """
    path = config.manifest_path
    if not path.exists():
        print(f"error: source registry missing: {path}", file=sys.stderr)
        return EXIT_ERROR
    try:
        manifest = SourceManifest.load(path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    with ecfr.make_client() as client:
        try:
            discovery = ecfr.discover_title14(client)
            aim_discovery = aim_source.discover_aim(client)
            pcg_discovery = pcg_source.discover_pcg(client)
        except FetchError as exc:
            # Network failure is not "source removed" (plan §25.3): report
            # it and leave the last known-good corpus untouched.
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    code = max(
        _report_ecfr_remote(manifest, discovery),
        _report_aim_remote(manifest, aim_discovery),
        _report_pcg_remote(manifest, pcg_discovery),
    )
    if code != EXIT_OK:
        # Upstream rollback or altered provenance pins: never auto-resolved
        # (the report says what to investigate; --force lives on fetch).
        return code

    latest = {
        ecfr.SOURCE_NAME: discovery.latest_issue_date,
        aim_source.SOURCE_NAME: aim_discovery.version,
        pcg_source.SOURCE_NAME: pcg_discovery.version,
    }
    changed = [name for name, v in latest.items() if manifest.sources[name].accepted_version != v]
    if not changed:
        if reverify:
            # Versions match, but the FAA can edit content without bumping
            # the edition — re-verify against the pinned raw hashes.
            code = _reverify_accepted_content(config, path)
            if code != EXIT_OK:
                return code
            # The fetchers run their own discovery, so an edition that
            # moved upstream between our discovery and theirs was just
            # accepted — a kept, non-volatile manifest delta (its canonical
            # hash is now pending). Decide what follows from the manifest
            # as it stands, not the pre-fetch snapshot, so freshly accepted
            # state resumes into publication below instead of exiting as
            # "nothing to do" with the vault behind the manifest.
            try:
                manifest = SourceManifest.load(path)
            except ManifestError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_ERROR
        # An interrupted earlier run can leave every accepted version equal
        # to upstream while the published output lags: fetch accepts each
        # source *before* parse/build run, so dying mid-pipeline must not
        # let the next run report "nothing to do" and strand the stale
        # state forever. Persisted, layer-independent signals catch every
        # such interruption; a fresh environment built from a merged
        # (fully validated) manifest + vault triggers none of them.
        reason = _update_resume_reason(config, manifest)
        if reason is None:
            print("update: all sources up to date; nothing to do")
            return EXIT_OK
        print(f"update: sources up to date but {reason}; resuming the interrupted update")

    before = {
        name: (state.accepted_version, state.canonical_hash)
        for name, state in manifest.sources.items()
    }
    steps = [
        ("fetch ecfr", lambda: cmd_fetch_ecfr(config, force=False)),
        ("fetch aim", lambda: cmd_fetch_aim(config, force=False)),
        ("fetch pcg", lambda: cmd_fetch_pcg(config, force=False)),
        ("parse ecfr", lambda: cmd_parse_ecfr(config, None)),
        ("parse aim", lambda: cmd_parse_aim(config)),
        ("parse pcg", lambda: cmd_parse_pcg(config)),
        # Derived links are recomputed from the freshly parsed layers so a
        # stale related.json never reaches the vault (plan §36.4); the
        # vault diff in the PR is where the changed suggestions are reviewed.
        ("enrich", lambda: cmd_enrich(config)),
        # Structural diff before generation (plan §22 Phase 8): the vault
        # on disk still shows the old versions, the layers the new — the
        # report of adds/rewrites/removals lands in the update log the PR
        # carries, so removals are explicit before publication.
        ("diff", lambda: cmd_diff(config)),
        ("build-vault", lambda: cmd_build_vault(config)),
        ("validate", lambda: cmd_validate(config)),
    ]
    for label, step in steps:
        print(f"==> far-aim {label}")
        step_code = step()
        if step_code != EXIT_OK:
            print(
                f"error: update stopped at `far-aim {label}`; the last known-good "
                "output is preserved",
                file=sys.stderr,
            )
            return step_code

    try:
        after = SourceManifest.load(path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print("update summary:")
    for name in sorted(after.sources):
        old_version, old_hash = before[name]
        state = after.sources[name]
        if name in changed:
            content = (
                "canonical content unchanged"
                if old_hash is not None and old_hash == state.canonical_hash
                else "canonical content changed"
            )
            print(f"  {name}: {old_version or 'none'} → {state.accepted_version} ({content})")
        elif (old_version, old_hash) != (state.accepted_version, state.canonical_hash):
            # A resumed run completed publication of a version the
            # interrupted run had already accepted: the manifest showed the
            # new version from the start (so it is not in ``changed``), and
            # the pre-interruption canonical hash is gone with it — report
            # the publication without a content claim it cannot back.
            print(f"  {name}: resumed publication of {state.accepted_version}")
        else:
            print(f"  {name}: unchanged ({state.accepted_version})")
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
    if args.command == "enrich":
        return cmd_enrich(config)
    if args.command == "diff":
        return cmd_diff(config)
    if args.command == "update":
        return cmd_update(config, reverify=args.reverify)

    corpus = getattr(args, "corpus", None)
    phase = NOT_IMPLEMENTED_PHASE[(args.command, corpus)]
    target = f"{args.command} {corpus}" if corpus else args.command
    print(f"far-aim {target}: not implemented yet — planned for {phase}.", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
