"""AIM acquisition from the FAA HTML edition (plan §4.2, §5.2, §6, §14.2; Phase 4).

Discovery reads the FAA publications landing page (``sources.faa_publications``)
for the current edition label, change number, effective date, and HTML
index URL. The corpus is then fetched from that index:

- ``index.html`` — edition summary plus the navigation listing every page;
- ``chap_N.html`` — chapter pages (heading + section/paragraph TOC);
- ``chapN_section_M.html`` — section pages (the paragraphs themselves);
- ``appendix_N.html`` — appendices;
- ``images/*`` — every figure referenced by any page (plan §4.2: figures are
  part of the corpus; never hotlinked, never dropped).

Accepted snapshots are archived in the gitignored raw cache under
``data/raw/aim/{effective-date}-change-{n}/`` as ``pages/``, ``figures/``
and ``metadata.json`` (per-file checksums). The manifest records a *tree
hash* over all page and figure checksums as ``raw_hash``. The FAA offers no
point-in-time access, so accepted snapshots must additionally be archived
outside this repository (plan §6.2).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from far_aim.config import Config
from far_aim.htmltree import HTMLStructureError, Node, parse_html
from far_aim.manifest import SourceManifest
from far_aim.sources import faa_publications
from far_aim.sources.common import (
    RETRYABLE_STATUS,
    TIMESTAMP_RE,
    TRANSIENT_HTTP_ERRORS,
    FetchError,
    RetryableError,
    Sleep,
    exclusive_lock,
    fetch_lock_path,
    fsync_dir,
    hash_suffix,
    is_calendar_date,
    make_client,
    retryable_status,
    retrying,
    sha256_of,
    sha256_of_bytes,
    utc_now_iso,
    write_json_durable,
)

log = logging.getLogger(__name__)

SOURCE_NAME = "aim"
PAGES_DIR = "pages"
FIGURES_DIR = "figures"
INDEX_PAGE = "index.html"

# Truncation / layout-change guards (plan §17.1, §25.1), data-informed: the
# Basic-with-Change-3 edition (effective 2026-07-09) has 65 pages (12
# chapters, 48 sections, 5 appendices), 432 numbered paragraphs and 270
# distinct figure files. Anything near these floors means an incomplete
# download or an upstream layout change — fail closed.
MIN_PAGES = 40
MIN_PARAGRAPHS = 300
MIN_FIGURES = 150
# Baseline structure every accepted edition must still contain: a partially
# rendered index that dropped a whole chapter or appendix would otherwise
# pass the aggregate floors above. New chapters/appendices beyond the
# baseline are accepted; gaps below it are not.
REQUIRED_CHAPTERS = frozenset(range(0, 12))
REQUIRED_APPENDICES = frozenset(range(1, 6))
# Polite pacing between consecutive requests to the FAA site (plan §26).
PAUSE_SECONDS = 0.1

PAGE_NAME_RE = re.compile(
    r"^(?:\./)?(?P<name>chap_(?P<chapter>\d{1,2})|chap(?P<sc>\d{1,2})_section_(?P<section>\d{1,2})"
    r"|appendix_(?P<appendix>\d{1,2}))\.html$"
)
IMAGE_SRC_RE = re.compile(r"^(?:\./)?images/(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]*)$")
_PARAGRAPH_TITLE_RE = re.compile(r'class="paragraph-title"')
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class AimDiscovery:
    """The current AIM HTML edition as listed on the FAA publications page."""

    label: str
    effective_date: str
    change: int
    index_url: str

    @property
    def version(self) -> str:
        return version_string(self.effective_date, self.change)


def version_string(effective_date: str, change: int) -> str:
    """``2026-07-09`` + change 3 → ``2026-07-09-change-3`` (snapshot directory name)."""
    return f"{effective_date}-change-{change}"


def discover_aim(client: httpx.Client, *, sleep: Sleep | None = None) -> AimDiscovery:
    listing = faa_publications.discover_publications(client, sleep=sleep, publications=("aim",))
    listing = listing["aim"]
    return AimDiscovery(
        label=listing.label,
        effective_date=listing.effective_date,
        change=listing.change,
        index_url=listing.html_url,
    )


# ---------------------------------------------------------------------------
# Page naming
# ---------------------------------------------------------------------------


def classify_page(name: str) -> tuple:
    """``("chapter", 4)`` / ``("section", 4, 1)`` / ``("appendix", 3)`` for a page filename."""
    match = PAGE_NAME_RE.match(name)
    if match is None:
        raise ValueError(f"not an AIM page name: {name!r}")
    if match.group("chapter") is not None:
        return ("chapter", int(match.group("chapter")))
    if match.group("appendix") is not None:
        return ("appendix", int(match.group("appendix")))
    return ("section", int(match.group("sc")), int(match.group("section")))


def page_sort_key(name: str) -> tuple:
    kind = classify_page(name)
    if kind[0] == "chapter":
        return (0, kind[1], -1)
    if kind[0] == "section":
        return (0, kind[1], kind[2])
    return (1, kind[1], 0)


def parse_page(html: str, what: str) -> Node:
    """Strictly parse a downloaded page; malformed/truncated markup is a fetch failure."""
    try:
        return parse_html(html)
    except HTMLStructureError as exc:
        raise FetchError(f"{what}: malformed or truncated HTML ({exc})") from exc


def page_names_from_index(html: str) -> list[str]:
    """Every AIM page linked from the index navigation, in canonical order.

    Chapter pages come first (each followed by its sections), appendices
    last. Every section's chapter must have a chapter page and every chapter
    page at least one section.
    """
    root = parse_page(html, "AIM index page")
    navs = root.find_all(lambda n: n.tag == "nav")
    if not navs:
        raise FetchError("AIM index page has no navigation; upstream layout may have changed")
    names: dict[str, None] = {}
    for nav in navs:
        for anchor in nav.find_all(lambda n: n.tag == "a"):
            href = (anchor.get("href") or "").strip()
            match = PAGE_NAME_RE.match(href)
            if match is not None:
                names.setdefault(match.group("name") + ".html", None)
            elif not _is_non_page_nav_link(href):
                # A local link of an unknown shape may be a page the grammar
                # does not know (a renamed section, a new kind of appendix):
                # dropping it would archive an incomplete edition.
                raise FetchError(
                    f"AIM index navigation links to an unrecognized local page {href!r}; "
                    "upstream layout may have changed"
                )
    ordered = sorted(names, key=page_sort_key)
    check_page_set(ordered)
    return ordered


def _is_non_page_nav_link(href: str) -> bool:
    """Navigation links that are legitimately not edition pages: the index
    itself, in-page anchors, and external sites."""
    return (
        href in ("", "./", ".", INDEX_PAGE, f"./{INDEX_PAGE}")
        or href.startswith(("#", "http://", "https://", "mailto:"))
    )


def check_page_set(names: list[str]) -> None:
    """Structural gate over a page list (index navigation or archived snapshot).

    - two filenames must never normalize to one identity (``chap_4.html``
      and ``chap_04.html`` would otherwise silently overwrite each other);
    - chapter pages and chapters-with-sections must agree;
    - every chapter's sections must be contiguous from 0 or 1;
    - the baseline chapters and appendices must all be present.
    Raises :class:`FetchError` describing the first violation.
    """
    identities: dict[tuple, str] = {}
    for name in names:
        kind = classify_page(name)
        if kind in identities:
            raise FetchError(
                f"AIM pages {identities[kind]!r} and {name!r} denote the same "
                f"{kind[0]}; refusing an ambiguous page set"
            )
        identities[kind] = name
    chapters = {k[1] for k in identities if k[0] == "chapter"}
    section_chapters = {k[1] for k in identities if k[0] == "section"}
    if chapters != section_chapters:
        raise FetchError(
            "AIM index navigation is inconsistent: chapter pages "
            f"{sorted(chapters)} vs chapters with section pages {sorted(section_chapters)}"
        )
    for chapter in sorted(chapters):
        sections = sorted(k[2] for k in identities if k[0] == "section" and k[1] == chapter)
        if sections[0] not in (0, 1) or sections != list(range(sections[0], sections[-1] + 1)):
            raise FetchError(
                f"AIM chapter {chapter} sections are not contiguous: {sections}; "
                "index may be partially rendered"
            )
    appendices = {k[1] for k in identities if k[0] == "appendix"}
    missing_chapters = sorted(REQUIRED_CHAPTERS - chapters)
    missing_appendices = sorted(REQUIRED_APPENDICES - appendices)
    if missing_chapters or missing_appendices:
        raise FetchError(
            f"AIM page set is missing baseline chapters {missing_chapters} and "
            f"appendices {missing_appendices}; index may be partially rendered "
            "(adjust REQUIRED_CHAPTERS/REQUIRED_APPENDICES only after confirming the "
            "FAA restructured the manual)"
        )


def index_edition(html: str) -> tuple[str, int]:
    """(effective date, change) from the AIM index page's publication summary."""
    root = parse_page(html, "AIM index page")
    summary = root.find(lambda n: n.tag == "div" and n.has_class("publication-summary"))
    if summary is None:
        raise FetchError("AIM index page has no publication summary; upstream layout changed?")
    text = _WS_RE.sub(" ", summary.text())
    effective = re.search(r"Effective:\s*(\d{1,2})/(\d{1,2})/(\d{4})", text)
    change = re.search(r"Change:\s*(Change\s+\d+|Basic)", text, re.IGNORECASE)
    if effective is None or change is None:
        raise FetchError(f"AIM index page summary is unparseable: {text.strip()!r}")
    month, day, year = (int(effective.group(i)) for i in (1, 2, 3))
    from datetime import date

    try:
        effective_date = date(year, month, day).isoformat()
    except ValueError as exc:
        raise FetchError(f"AIM index page has an invalid effective date: {text!r}") from exc
    return effective_date, faa_publications.change_number(change.group(1))


def image_names(html: str, what: str = "AIM page") -> list[str]:
    """Distinct figure filenames referenced by a page, in document order.

    An image outside the edition's ``images/`` directory cannot be archived
    with the snapshot and is a fetch failure, not a dropped figure (plan §4.2).
    """
    root = parse_page(html, what)
    # Only the publication content counts; site chrome outside <main> (e.g.
    # an analytics pixel in <noscript>) is not part of the corpus.
    main = root.find(lambda n: n.tag == "main")
    if main is None:
        raise FetchError("AIM page has no <main> content region")
    names: dict[str, None] = {}
    for img in main.find_all(lambda n: n.tag == "img"):
        src = (img.get("src") or "").strip()
        match = IMAGE_SRC_RE.match(src)
        if match is None:
            raise FetchError(f"AIM page references an image outside images/: {src!r}")
        names.setdefault(match.group("name"), None)
    return list(names)


def decode_page(body: bytes, what: str) -> str:
    """Decode a downloaded page as UTF-8 (the edition's declared charset).

    The FAA serves the AIM as ``<meta charset="UTF-8">`` HTML; bytes that do
    not decode as UTF-8 are a corrupt or foreign response and a controlled
    fetch failure, never a traceback.
    """
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FetchError(
            f"{what}: response is not valid UTF-8 ({exc.reason} at byte {exc.start})"
        ) from exc


def _page_defect(name: str, html: str) -> str | None:
    """Coarse source-integrity check that a downloaded page is a complete AIM page.

    Strict parsing rejects truncated or unbalanced markup outright; the
    marker checks then confirm the page is the kind its name promises.
    """
    try:
        parse_html(html)
    except HTMLStructureError as exc:
        return f"malformed or truncated HTML ({exc})"
    if 'id="main"' not in html:
        return "no main content region"
    kind = classify_page(name)[0]
    if kind == "chapter" and "book-chapter" not in html:
        return "chapter page has no section listing"
    if kind in ("section", "appendix") and 'class="body conbody"' not in html:
        return "content page has no body"
    return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_bytes(
    client: httpx.Client, url: str, *, expect: str, sleep: Sleep, what: str
) -> tuple[bytes, str]:
    """GET ``url`` with retries; returns (body, content-type). ``expect`` is a
    substring the content type must contain (``html`` / ``image``)."""

    def attempt() -> tuple[bytes, str]:
        try:
            response = client.get(url)
        except TRANSIENT_HTTP_ERRORS as exc:
            raise RetryableError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP client error fetching {url}: {exc}") from exc
        if response.status_code in RETRYABLE_STATUS:
            raise retryable_status(response)
        if response.status_code != 200:
            raise FetchError(f"{what}: {url} returned HTTP {response.status_code}")
        faa_publications.check_response_origin(response, what)
        content_type = response.headers.get("content-type", "")
        if expect not in content_type.lower():
            raise FetchError(
                f"{what}: expected {expect} from {url}, got content-type {content_type!r}"
            )
        body = response.content
        declared = response.headers.get("content-length")
        if declared is not None and "content-encoding" not in response.headers:
            try:
                expected = int(declared)
            except ValueError as exc:
                raise FetchError(
                    f"malformed Content-Length header from {url}: {declared!r}"
                ) from exc
            if expected != len(body):
                raise RetryableError(f"truncated transfer: got {len(body)} of {expected} bytes")
        if not body:
            raise FetchError(f"{what}: empty response from {url}")
        return body, content_type

    return retrying(attempt, what=what, sleep=sleep)


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------


def tree_hash(files: dict[str, str]) -> str:
    """Content hash of a snapshot: sha256 over the sorted (path, checksum) listing."""
    payload = json.dumps(dict(sorted(files.items())), separators=(",", ":"), sort_keys=True)
    return sha256_of_bytes(payload.encode("utf-8"))


def snapshot_files(snapshot_dir: Path) -> dict[str, str]:
    """Checksums of every page and figure file actually on disk, by relative path."""
    files: dict[str, str] = {}
    for sub in (PAGES_DIR, FIGURES_DIR):
        base = snapshot_dir / sub
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                files[f"{sub}/{path.name}"] = sha256_of(path)
    return files


_METADATA_KEYS = frozenset(
    {
        "provider",
        "publication",
        "source_version",
        "edition_label",
        "effective_date",
        "change",
        "index_url",
        "publications_url",
        "retrieved_at",
        "raw_hash",
        "byte_count",
        "page_count",
        "paragraph_count",
        "figure_count",
        "files",
    }
)


def load_metadata(snapshot_dir: Path) -> dict | None:
    try:
        data = json.loads((snapshot_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def metadata_intact(metadata: object, *, version: str, raw_hash: str) -> bool:
    """True when ``metadata`` is a complete provenance record for ``raw_hash``."""
    if not isinstance(metadata, dict) or set(metadata) != _METADATA_KEYS:
        return False
    files = metadata.get("files")
    change = metadata.get("change")
    counts = (metadata.get("byte_count"), metadata.get("page_count"),
              metadata.get("paragraph_count"), metadata.get("figure_count"))
    return (
        metadata.get("provider") == "faa"
        and metadata.get("publication") == "aim"
        and metadata.get("source_version") == version
        and isinstance(metadata.get("edition_label"), str)
        and is_calendar_date(metadata.get("effective_date"))
        and isinstance(change, int)
        and not isinstance(change, bool)
        and version == version_string(metadata["effective_date"], change)
        and isinstance(metadata.get("index_url"), str)
        and isinstance(metadata.get("publications_url"), str)
        and isinstance(metadata.get("retrieved_at"), str)
        and TIMESTAMP_RE.fullmatch(metadata["retrieved_at"]) is not None
        and metadata.get("raw_hash") == raw_hash
        and all(isinstance(c, int) and not isinstance(c, bool) for c in counts)
        and isinstance(files, dict)
        and all(
            isinstance(k, str)
            and isinstance(v, dict)
            and isinstance(v.get("sha256"), str)
            and isinstance(v.get("bytes"), int)
            for k, v in files.items()
        )
        and tree_hash({k: v["sha256"] for k, v in files.items()}) == raw_hash
    )


def verify_snapshot(snapshot_dir: Path, *, version: str, raw_hash: str) -> bool:
    """True when the archived snapshot is complete and matches ``raw_hash`` exactly.

    Every listed file must be present with its recorded checksum and size, no
    extra page/figure files may exist, the tree hash must equal ``raw_hash``,
    and the metadata counts — outside the tree hash — must be what the
    archived files actually yield (byte total, page/figure counts, numbered
    paragraphs) and clear the integrity floors, so an edited ``metadata.json``
    cannot pass off as a verified snapshot.
    """
    metadata = load_metadata(snapshot_dir)
    if not metadata_intact(metadata, version=version, raw_hash=raw_hash):
        return False
    assert isinstance(metadata, dict)
    files = metadata["files"]
    listed = {k: v["sha256"] for k, v in files.items()}
    if snapshot_files(snapshot_dir) != listed:
        return False
    if f"{PAGES_DIR}/{INDEX_PAGE}" not in files:
        return False
    byte_count = 0
    paragraph_count = 0
    for rel, entry in files.items():
        path = snapshot_dir / rel
        size = path.stat().st_size
        if entry["bytes"] != size:
            return False
        byte_count += size
        if rel.startswith(f"{PAGES_DIR}/") and rel != f"{PAGES_DIR}/{INDEX_PAGE}":
            try:
                paragraph_count += len(
                    _PARAGRAPH_TITLE_RE.findall(path.read_text(encoding="utf-8"))
                )
            except UnicodeDecodeError:
                return False
    page_count = sum(1 for rel in files if rel.startswith(f"{PAGES_DIR}/"))
    figure_count = sum(1 for rel in files if rel.startswith(f"{FIGURES_DIR}/"))
    return (
        metadata["byte_count"] == byte_count
        and metadata["page_count"] == page_count
        and metadata["figure_count"] == figure_count
        and metadata["paragraph_count"] == paragraph_count
        and page_count >= MIN_PAGES
        and figure_count >= MIN_FIGURES
        and paragraph_count >= MIN_PARAGRAPHS
    )


def _snapshot_dir(config: Config, version: str) -> Path:
    return config.raw_dir / "aim" / version


def _superseded_dir(snapshot_dir: Path, raw_hash: str) -> Path:
    return snapshot_dir.with_name(f"{snapshot_dir.name}.superseded-{hash_suffix(raw_hash)}")


def _restore_known_good(snapshot_dir: Path, version: str, known_good_hash: str) -> bool:
    """Roll the archive back to a preserved last known-good snapshot, if any.

    Whatever currently sits at ``snapshot_dir`` (validated but never
    accepted) is parked as ``{version}.unaccepted-<hash>``. Returns True if
    ``snapshot_dir`` now verifies against ``known_good_hash``.
    """
    superseded = _superseded_dir(snapshot_dir, known_good_hash)
    if not superseded.is_dir() or not verify_snapshot(
        superseded, version=version, raw_hash=known_good_hash
    ):
        return False
    if snapshot_dir.exists():
        current = tree_hash(snapshot_files(snapshot_dir))
        unaccepted = snapshot_dir.with_name(
            f"{snapshot_dir.name}.unaccepted-{hash_suffix(current)}"
        )
        if unaccepted.exists():
            shutil.rmtree(unaccepted)
        os.replace(snapshot_dir, unaccepted)
        log.warning(
            "archive %s held unaccepted files (%s); moved to %s", snapshot_dir, current, unaccepted
        )
    os.replace(superseded, snapshot_dir)
    fsync_dir(snapshot_dir.parent)
    log.warning("restored last known-good AIM snapshot (%s) to %s", known_good_hash, snapshot_dir)
    return True


def _reconcile_accepted_archive(config: Config, version: str, accepted_hash: str) -> None:
    snapshot_dir = _snapshot_dir(config, version)
    if snapshot_dir.is_dir() and verify_snapshot(
        snapshot_dir, version=version, raw_hash=accepted_hash
    ):
        return
    _restore_known_good(snapshot_dir, version, accepted_hash)


def _manifest_records(
    manifest_path: Path, *, raw_hash: str, edition_label: str | None, source_url: str | None
) -> bool:
    """True if the on-disk manifest still records exactly this accepted state."""
    try:
        state = SourceManifest.load(manifest_path).sources[SOURCE_NAME]
    except Exception:  # noqa: BLE001 - unreadable manifest: cannot prove the old state
        return False
    return (
        state.raw_hash == raw_hash
        and state.edition_label == edition_label
        and state.source_url == source_url
    )


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def listing_matches_pins(state, discovery: AimDiscovery) -> bool:
    """False when the FAA lists the accepted edition under a different label or URL."""
    pins = (state.edition_label, state.source_url)
    if None in pins:
        return True  # nothing pinned yet (legacy manifest)
    return pins == (discovery.label, discovery.index_url)


def _check_listing_matches_pins(state, discovery: AimDiscovery) -> None:
    """Same effective date and change, but the FAA re-labelled or relocated
    the edition: the accepted snapshot may still verify, yet the listing no
    longer describes it. Never a silent no-op — the operator re-accepts with
    ``--force``."""
    if listing_matches_pins(state, discovery):
        return
    raise FetchError(
        f"FAA now lists AIM edition {discovery.version} as {discovery.label!r} at "
        f"{discovery.index_url!r}, but the accepted snapshot was recorded as "
        f"{state.edition_label!r} at {state.source_url!r}; re-run with --force "
        "to re-accept the edition under its current listing"
    )


@dataclass(frozen=True)
class AimFetchResult:
    version: str
    effective_date: str
    change: int
    label: str
    snapshot_dir: Path
    raw_hash: str
    byte_count: int
    page_count: int
    paragraph_count: int
    figure_count: int
    downloaded: bool


def fetch_aim(
    config: Config,
    client: httpx.Client | None = None,
    *,
    force: bool = False,
    sleep: Sleep | None = None,
    now: Callable[[], str] = utc_now_iso,
) -> AimFetchResult:
    """Discover → download corpus + figures → validate → archive → update manifest.

    Idempotent: when the manifest's accepted edition matches the FAA listing
    and the archived snapshot verifies file-by-file, nothing is downloaded
    and only ``last_checked_at`` moves.
    """
    if sleep is None:
        sleep = time.sleep
    manifest_path = config.manifest_path
    if not manifest_path.exists():
        raise FetchError(f"source registry missing: {manifest_path}")
    with exclusive_lock(fetch_lock_path(config)):
        return _fetch_aim_locked(
            config, client, manifest_path=manifest_path, force=force, sleep=sleep, now=now
        )


def _download_corpus(
    client: httpx.Client, discovery: AimDiscovery, dest: Path, *, sleep: Sleep
) -> tuple[dict[str, dict], int, int]:
    """Download index, pages and figures into ``dest``; returns (files, paragraphs, figures)."""
    pages_dir = dest / PAGES_DIR
    figures_dir = dest / FIGURES_DIR
    pages_dir.mkdir(parents=True)
    figures_dir.mkdir(parents=True)
    files: dict[str, dict] = {}

    def store(rel: str, body: bytes) -> None:
        path = dest / rel
        with path.open("wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        files[rel] = {"sha256": sha256_of_bytes(body), "bytes": len(body)}

    def pause() -> None:
        if PAUSE_SECONDS:
            sleep(PAUSE_SECONDS)

    index_bytes, _ = _get_bytes(
        client, discovery.index_url, expect="html", sleep=sleep, what="AIM index page"
    )
    index_html = decode_page(index_bytes, "AIM index page")
    edition = index_edition(index_html)
    if edition != (discovery.effective_date, discovery.change):
        raise FetchError(
            f"FAA publications page lists AIM effective {discovery.effective_date} "
            f"change {discovery.change}, but the HTML edition at {discovery.index_url} "
            f"reports effective {edition[0]} change {edition[1]}; upstream may be "
            "mid-update — refusing to accept"
        )
    store(f"{PAGES_DIR}/{INDEX_PAGE}", index_bytes)
    page_names = page_names_from_index(index_html)
    # The index page's own front matter is parsed too; any figure it embeds
    # must be archived like every other page's.
    image_order: dict[str, None] = {}
    for image in image_names(index_html, "AIM index page"):
        image_order.setdefault(image, None)
    if len(page_names) < MIN_PAGES:
        raise FetchError(
            f"AIM index lists only {len(page_names)} pages (expected ≥ {MIN_PAGES}); "
            "upstream layout may have changed"
        )
    pause()

    paragraph_count = 0
    for name in page_names:
        url = urljoin(discovery.index_url, name)
        body, _ = _get_bytes(client, url, expect="html", sleep=sleep, what=f"AIM page {name}")
        html = decode_page(body, f"AIM page {name}")
        defect = _page_defect(name, html)
        if defect is not None:
            raise FetchError(f"AIM page {name}: {defect}; upstream layout may have changed")
        paragraph_count += len(_PARAGRAPH_TITLE_RE.findall(html))
        for image in image_names(html, f"AIM page {name}"):
            image_order.setdefault(image, None)
        store(f"{PAGES_DIR}/{name}", body)
        pause()
    if paragraph_count < MIN_PARAGRAPHS:
        raise FetchError(
            f"only {paragraph_count} numbered paragraphs found across AIM pages "
            f"(expected ≥ {MIN_PARAGRAPHS}); download looks incomplete or upstream markup changed"
        )
    if len(image_order) < MIN_FIGURES:
        raise FetchError(
            f"only {len(image_order)} distinct figures referenced (expected ≥ {MIN_FIGURES}); "
            "upstream markup may have changed"
        )
    # Names that differ only by case would overwrite each other on a
    # case-insensitive filesystem and could never verify; refuse them.
    folded: dict[str, str] = {}
    for name in [*page_names, *image_order]:
        other = folded.setdefault(name.casefold(), name)
        if other != name:
            raise FetchError(
                f"AIM edition contains filenames differing only by case ({other!r}, "
                f"{name!r}); refusing a snapshot that cannot be archived faithfully"
            )
    for image in image_order:
        url = urljoin(discovery.index_url, f"images/{image}")
        body, _ = _get_bytes(client, url, expect="image", sleep=sleep, what=f"AIM figure {image}")
        store(f"{FIGURES_DIR}/{image}", body)
        pause()
    fsync_dir(pages_dir)
    fsync_dir(figures_dir)
    return files, paragraph_count, len(image_order)


def _publish_snapshot(
    tmp_dir: Path, snapshot_dir: Path, *, new_hash: str, version: str
) -> Path | None:
    """Install the validated download, preserving the previous snapshot first.

    The displaced directory is kept as ``{version}.superseded-<hash>`` even
    when its content hash equals the new one: its ``metadata.json`` (edition
    label, index URL, retrieval time) is the last known-good provenance until
    the manifest commit succeeds. Returns the preserved directory, if any.
    """
    preserved: Path | None = None
    if snapshot_dir.exists():
        existing_hash = tree_hash(snapshot_files(snapshot_dir))
        superseded = _superseded_dir(snapshot_dir, existing_hash)
        if superseded.exists():
            shutil.rmtree(superseded)
        os.replace(snapshot_dir, superseded)
        fsync_dir(snapshot_dir.parent)
        preserved = superseded
        if existing_hash != new_hash:
            log.warning(
                "AIM %s: previous snapshot (%s) preserved at %s", version, existing_hash, superseded
            )
    os.replace(tmp_dir, snapshot_dir)
    fsync_dir(snapshot_dir.parent)
    return preserved


def _fetch_aim_locked(
    config: Config,
    client: httpx.Client | None,
    *,
    manifest_path: Path,
    force: bool,
    sleep: Sleep,
    now: Callable[[], str],
) -> AimFetchResult:
    manifest = SourceManifest.load(manifest_path)
    state = manifest.sources[SOURCE_NAME]

    if state.accepted_version is not None and state.raw_hash is not None:
        _reconcile_accepted_archive(config, state.accepted_version, state.raw_hash)

    own_client = client is None
    if client is None:
        client = make_client()
    try:
        discovery = discover_aim(client, sleep=sleep)
        version = discovery.version
        checked_at = now()
        snapshot_dir = _snapshot_dir(config, version)

        if (
            not force
            and state.effective_date is not None
            and state.change is not None
            and (discovery.effective_date, discovery.change) < (state.effective_date, state.change)
        ):
            raise FetchError(
                f"FAA lists AIM effective {discovery.effective_date} change {discovery.change}, "
                f"older than accepted {state.effective_date} change {state.change}; refusing "
                "to roll back automatically. Investigate upstream, then re-run with --force."
            )

        if (
            not force
            and state.accepted_version == version
            and state.raw_hash is not None
            and verify_snapshot(snapshot_dir, version=version, raw_hash=state.raw_hash)
        ):
            metadata = load_metadata(snapshot_dir) or {}
            log.info("AIM %s already accepted; cached snapshot verified", version)
            archived = (metadata["edition_label"], metadata["index_url"])
            # Backfill the pinned provenance for manifests written before it
            # was recorded — from the *verified snapshot's* metadata, which is
            # what the archive and any normalized layer actually carry.
            if state.edition_label is None:
                state.edition_label = archived[0]
            if state.source_url is None:
                state.source_url = archived[1]
            if (state.edition_label, state.source_url) != archived:
                # The archive verifies by content, but its provenance record
                # (outside the tree hash) no longer says what the manifest
                # accepted: `parse aim` would refuse it, so do not report it
                # as an unchanged, verified snapshot.
                raise FetchError(
                    f"archived AIM snapshot {snapshot_dir} records edition {archived[0]!r} at "
                    f"{archived[1]!r}, but the manifest accepted {state.edition_label!r} at "
                    f"{state.source_url!r}; the archive's provenance was altered — restore "
                    "it, or re-run with --force to re-accept the edition from the FAA"
                )
            state.last_checked_at = checked_at
            manifest.save(manifest_path)
            _check_listing_matches_pins(state, discovery)
            return AimFetchResult(
                version=version,
                effective_date=discovery.effective_date,
                change=discovery.change,
                label=discovery.label,
                snapshot_dir=snapshot_dir,
                raw_hash=state.raw_hash,
                byte_count=int(metadata.get("byte_count", 0)),
                page_count=int(metadata.get("page_count", 0)),
                paragraph_count=int(metadata.get("paragraph_count", 0)),
                figure_count=int(metadata.get("figure_count", 0)),
                downloaded=False,
            )

        if not force and state.accepted_version == version:
            _check_listing_matches_pins(state, discovery)
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=snapshot_dir.parent, prefix=f".{version}.download-"))
        try:
            log.info(
                "downloading AIM %s (%s) from %s", version, discovery.label, discovery.index_url
            )
            files, paragraph_count, figure_count = _download_corpus(
                client, discovery, tmp_dir, sleep=sleep
            )
            raw_hash = tree_hash({k: v["sha256"] for k, v in files.items()})
            byte_count = sum(v["bytes"] for v in files.values())
            page_count = sum(1 for k in files if k.startswith(f"{PAGES_DIR}/"))
            if (
                not force
                and state.accepted_version == version
                and state.raw_hash is not None
                and raw_hash != state.raw_hash
            ):
                quarantine = snapshot_dir.with_name(
                    f"{snapshot_dir.name}.mismatch-{hash_suffix(raw_hash)}"
                )
                if quarantine.exists():
                    shutil.rmtree(quarantine)
                os.replace(tmp_dir, quarantine)
                raise FetchError(
                    f"re-fetch of accepted AIM edition {version} returned different content: "
                    f"manifest records {state.raw_hash}, download is {raw_hash}. "
                    f"Downloaded files kept at {quarantine}. "
                    "Investigate, then re-run with --force to accept the new content."
                )
            write_json_durable(
                tmp_dir / "metadata.json",
                {
                    "provider": "faa",
                    "publication": "aim",
                    "source_version": version,
                    "edition_label": discovery.label,
                    "effective_date": discovery.effective_date,
                    "change": discovery.change,
                    "index_url": discovery.index_url,
                    "publications_url": faa_publications.PUBLICATIONS_URL,
                    "retrieved_at": checked_at,
                    "raw_hash": raw_hash,
                    "byte_count": byte_count,
                    "page_count": page_count,
                    "paragraph_count": paragraph_count,
                    "figure_count": figure_count,
                    "files": files,
                },
            )
            # The archive is accepted only if it verifies exactly as later
            # no-op fetches and parses will verify it (plan §25.4).
            if not verify_snapshot(tmp_dir, version=version, raw_hash=raw_hash):
                raise FetchError(
                    "downloaded AIM snapshot failed self-verification before acceptance; "
                    "nothing was accepted"
                )
            previous = (
                (state.raw_hash, state.edition_label, state.source_url)
                if state.accepted_version == version and state.raw_hash is not None
                else None
            )
            preserved: Path | None = None
            try:
                preserved = _publish_snapshot(
                    tmp_dir, snapshot_dir, new_hash=raw_hash, version=version
                )
                state.last_checked_at = checked_at
                if (
                    state.accepted_version != version
                    or state.raw_hash != raw_hash
                    or state.edition_label != discovery.label
                    or state.source_url != discovery.index_url
                ):
                    # A changed edition — or the same bytes re-accepted under
                    # a changed listing label/URL — invalidates the parsed
                    # layer: its documents carry the old provenance, which
                    # `validate` pins against the manifest.
                    state.canonical_hash = None
                state.accepted_version = version
                state.effective_date = discovery.effective_date
                state.change = discovery.change
                state.raw_hash = raw_hash
                state.edition_label = discovery.label
                state.source_url = discovery.index_url
                manifest.save(manifest_path)
            except BaseException:
                # Roll the archive back only if acceptance verifiably did not
                # commit (the on-disk manifest still records the previous
                # state, provenance included — equal content hashes alone do
                # not prove which snapshot the manifest describes).
                if previous is not None and _manifest_records(
                    manifest_path,
                    raw_hash=previous[0],
                    edition_label=previous[1],
                    source_url=previous[2],
                ):
                    _restore_known_good(snapshot_dir, version, previous[0])
                raise
            if preserved is not None and previous is not None and previous[0] == raw_hash:
                # Same content, committed: the preserved copy is redundant.
                shutil.rmtree(preserved)
                fsync_dir(snapshot_dir.parent)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

        log.info(
            "accepted AIM %s (%s): %d pages, %d paragraphs, %d figures, %d bytes",
            version,
            discovery.label,
            page_count,
            paragraph_count,
            figure_count,
            byte_count,
        )
        return AimFetchResult(
            version=version,
            effective_date=discovery.effective_date,
            change=discovery.change,
            label=discovery.label,
            snapshot_dir=snapshot_dir,
            raw_hash=raw_hash,
            byte_count=byte_count,
            page_count=page_count,
            paragraph_count=paragraph_count,
            figure_count=figure_count,
            downloaded=True,
        )
    finally:
        if own_client:
            client.close()


def page_url(index_url: str, page: str, anchor: str | None = None) -> str:
    url = urljoin(index_url, page)
    return f"{url}#{anchor}" if anchor else url
