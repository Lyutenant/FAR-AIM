"""PCG acquisition from the FAA HTML edition (plan §4.3, §5.2, §6, §14.3; Phase 5).

Discovery reads the FAA publications landing page (``sources.faa_publications``)
for the current Pilot/Controller Glossary edition label, change number,
effective date, and HTML index URL. The corpus is then fetched from that
index:

- ``index.html`` — purpose/front matter, the letter navigation, and the
  edition summary (cross-checked against the publications listing);
- ``glossary-a.html`` … ``glossary-w.html`` — one page per section letter,
  each holding that letter's term entries.

Accepted snapshots are archived in the gitignored raw cache under
``data/raw/pcg/{effective-date}-change-{n}/`` as ``pages/`` and
``metadata.json`` (per-file checksums). The manifest records a *tree hash*
over all page checksums as ``raw_hash``. The FAA offers no point-in-time
access, so accepted snapshots must additionally be archived outside this
repository (plan §6.2).

The transactional acceptance machinery (quarantine on re-fetch mismatch,
superseded-snapshot preservation until the manifest commits, offline
reconciliation after an interrupted acceptance) mirrors ``sources.aim``.
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
from far_aim.sources.aim import decode_page, page_url, tree_hash, version_string
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

SOURCE_NAME = "pcg"
PAGES_DIR = "pages"
INDEX_PAGE = "index.html"

# Truncation / layout-change guards (plan §17.1, §25.1), data-informed: the
# Basic-with-Change-3 edition (effective 2026-07-09) has 24 pages (index +
# 23 letters) and 1,562 term-entry paragraphs. Anything near these floors
# means an incomplete download or an upstream layout change — fail closed.
MIN_PAGES = 15
MIN_TERMS = 1200
# Baseline letters every accepted edition must still contain (the current
# glossary has no X/Y/Z sections). New letters beyond the baseline are
# accepted; gaps below it are not.
REQUIRED_LETTERS = frozenset("abcdefghijklmnopqrstuvw")
# Polite pacing between consecutive requests to the FAA site (plan §26).
PAUSE_SECONDS = 0.1

PAGE_NAME_RE = re.compile(r"^(?:\./)?(?P<name>glossary-(?P<letter>[a-z]))\.html$")
_TERM_ENTRY_RE = re.compile(r'glossary-term-entry')
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class PcgDiscovery:
    """The current PCG HTML edition as listed on the FAA publications page."""

    label: str
    effective_date: str
    change: int
    index_url: str

    @property
    def version(self) -> str:
        return version_string(self.effective_date, self.change)


def discover_pcg(client: httpx.Client, *, sleep: Sleep | None = None) -> PcgDiscovery:
    listing = faa_publications.discover_publications(client, sleep=sleep, publications=("pcg",))
    listing = listing["pcg"]
    return PcgDiscovery(
        label=listing.label,
        effective_date=listing.effective_date,
        change=listing.change,
        index_url=listing.html_url,
    )


# ---------------------------------------------------------------------------
# Page naming
# ---------------------------------------------------------------------------


def classify_page(name: str) -> tuple:
    """``("letter", "a")`` for ``glossary-a.html``."""
    match = PAGE_NAME_RE.match(name)
    if match is None:
        raise ValueError(f"not a PCG page name: {name!r}")
    return ("letter", match.group("letter"))


def page_sort_key(name: str) -> str:
    return classify_page(name)[1]


def parse_page(html: str, what: str) -> Node:
    """Strictly parse a downloaded page; malformed/truncated markup is a fetch failure."""
    try:
        return parse_html(html)
    except HTMLStructureError as exc:
        raise FetchError(f"{what}: malformed or truncated HTML ({exc})") from exc


def page_names_from_index(html: str) -> list[str]:
    """Every letter page linked from the index, in letter order.

    The sidebar letter navigation (``ul.pcg-letter-nav``) is authoritative;
    every link in it must be a recognizable glossary page. The index body's
    letter-card grid must agree with it — a disagreement means the index is
    partially rendered or mid-update.
    """
    root = parse_page(html, "PCG index page")
    nav = root.find(lambda n: n.tag == "ul" and n.has_class("pcg-letter-nav"))
    if nav is None:
        raise FetchError(
            "PCG index page has no letter navigation; upstream layout may have changed"
        )
    names: dict[str, None] = {}
    for anchor in nav.find_all(lambda n: n.tag == "a"):
        href = (anchor.get("href") or "").strip()
        match = PAGE_NAME_RE.match(href)
        if match is None:
            raise FetchError(
                f"PCG letter navigation links to an unrecognized page {href!r}; "
                "upstream layout may have changed"
            )
        names.setdefault(match.group("name") + ".html", None)
    grid_letters: set[str] = set()
    for anchor in root.find_all(lambda n: n.tag == "a" and n.has_class("pcg-letter-card")):
        href = (anchor.get("href") or "").strip()
        match = PAGE_NAME_RE.match(href)
        if match is None:
            raise FetchError(
                f"PCG letter grid links to an unrecognized page {href!r}; "
                "upstream layout may have changed"
            )
        grid_letters.add(match.group("letter"))
    ordered = sorted(names, key=page_sort_key)
    nav_letters = {classify_page(name)[1] for name in ordered}
    if grid_letters and grid_letters != nav_letters:
        raise FetchError(
            f"PCG index navigation lists letters {sorted(nav_letters)} but the letter "
            f"grid lists {sorted(grid_letters)}; index may be partially rendered"
        )
    check_page_set(ordered)
    return ordered


def check_page_set(names: list[str]) -> None:
    """Structural gate over a letter-page list (index navigation or archive)."""
    letters: dict[str, str] = {}
    for name in names:
        letter = classify_page(name)[1]
        if letter in letters:
            raise FetchError(
                f"PCG pages {letters[letter]!r} and {name!r} denote the same letter; "
                "refusing an ambiguous page set"
            )
        letters[letter] = name
    missing = sorted(REQUIRED_LETTERS - set(letters))
    if missing:
        raise FetchError(
            f"PCG page set is missing baseline letters {missing}; index may be "
            "partially rendered (adjust REQUIRED_LETTERS only after confirming the "
            "FAA restructured the glossary)"
        )


def index_edition(html: str) -> tuple[str, int]:
    """(effective date, change) from the PCG index page's publication summary."""
    root = parse_page(html, "PCG index page")
    summary = root.find(lambda n: n.tag == "aside" and n.has_class("pcg-publication-summary"))
    if summary is None:
        raise FetchError("PCG index page has no publication summary; upstream layout changed?")
    text = _WS_RE.sub(" ", summary.text())
    effective = re.search(r"Effective:\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    change = re.search(r"Change:\s*(Change\s+\d+|Basic|\d+)", text, re.IGNORECASE)
    if effective is None or change is None:
        raise FetchError(f"PCG index page summary is unparseable: {text.strip()!r}")
    month, day, year = (int(effective.group(i)) for i in (1, 2, 3))
    if year < 100:
        year += 2000
    from datetime import date

    try:
        effective_date = date(year, month, day).isoformat()
    except ValueError as exc:
        raise FetchError(f"PCG index page has an invalid effective date: {text!r}") from exc
    label = change.group(1)
    change_number = int(label) if label.isdigit() else faa_publications.change_number(label)
    return effective_date, change_number


def _page_defect(name: str, html: str) -> str | None:
    """Coarse source-integrity check that a downloaded page is a complete PCG page."""
    try:
        parse_html(html)
    except HTMLStructureError as exc:
        return f"malformed or truncated HTML ({exc})"
    if "pcg-content" not in html:
        return "no glossary content region"
    if not _TERM_ENTRY_RE.search(html):
        return "letter page has no term entries"
    return None


def count_terms(html: str) -> int:
    """Term-entry paragraphs on a letter page (the truncation-floor metric)."""
    return len(_TERM_ENTRY_RE.findall(html))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_bytes(
    client: httpx.Client, url: str, *, expect: str, sleep: Sleep, what: str
) -> tuple[bytes, str]:
    """GET ``url`` with retries; returns (body, content-type)."""

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


def snapshot_files(snapshot_dir: Path) -> dict[str, str]:
    """Checksums of every page file actually on disk, by relative path."""
    files: dict[str, str] = {}
    base = snapshot_dir / PAGES_DIR
    if base.is_dir():
        for path in sorted(base.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                files[f"{PAGES_DIR}/{path.name}"] = sha256_of(path)
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
        "term_count",
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
    counts = (metadata.get("byte_count"), metadata.get("page_count"), metadata.get("term_count"))
    return (
        metadata.get("provider") == "faa"
        and metadata.get("publication") == "pcg"
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
    extra page files may exist, the tree hash must equal ``raw_hash``, and
    the metadata counts — outside the tree hash — must be what the archived
    files actually yield and clear the integrity floors, so an edited
    ``metadata.json`` cannot pass off as a verified snapshot.
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
    term_count = 0
    for rel, entry in files.items():
        path = snapshot_dir / rel
        size = path.stat().st_size
        if entry["bytes"] != size:
            return False
        byte_count += size
        if rel != f"{PAGES_DIR}/{INDEX_PAGE}":
            try:
                term_count += count_terms(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                return False
    page_count = len(files)
    return (
        metadata["byte_count"] == byte_count
        and metadata["page_count"] == page_count
        and metadata["term_count"] == term_count
        and page_count >= MIN_PAGES
        and term_count >= MIN_TERMS
    )


def _snapshot_dir(config: Config, version: str) -> Path:
    return config.raw_dir / "pcg" / version


def _superseded_dir(snapshot_dir: Path, raw_hash: str) -> Path:
    return snapshot_dir.with_name(f"{snapshot_dir.name}.superseded-{hash_suffix(raw_hash)}")


def _restore_known_good(snapshot_dir: Path, version: str, known_good_hash: str) -> bool:
    """Roll the archive back to a preserved last known-good snapshot, if any."""
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
    log.warning("restored last known-good PCG snapshot (%s) to %s", known_good_hash, snapshot_dir)
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


def listing_matches_pins(state, discovery: PcgDiscovery) -> bool:
    """False when the FAA lists the accepted edition under a different label or URL."""
    pins = (state.edition_label, state.source_url)
    if None in pins:
        return True  # nothing pinned yet (legacy manifest)
    return pins == (discovery.label, discovery.index_url)


def _check_listing_matches_pins(state, discovery: PcgDiscovery) -> None:
    if listing_matches_pins(state, discovery):
        return
    raise FetchError(
        f"FAA now lists PCG edition {discovery.version} as {discovery.label!r} at "
        f"{discovery.index_url!r}, but the accepted snapshot was recorded as "
        f"{state.edition_label!r} at {state.source_url!r}; re-run with --force "
        "to re-accept the edition under its current listing"
    )


@dataclass(frozen=True)
class PcgFetchResult:
    version: str
    effective_date: str
    change: int
    label: str
    snapshot_dir: Path
    raw_hash: str
    byte_count: int
    page_count: int
    term_count: int
    downloaded: bool


def fetch_pcg(
    config: Config,
    client: httpx.Client | None = None,
    *,
    force: bool = False,
    sleep: Sleep | None = None,
    now: Callable[[], str] = utc_now_iso,
) -> PcgFetchResult:
    """Discover → download corpus → validate → archive → update manifest.

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
        return _fetch_pcg_locked(
            config, client, manifest_path=manifest_path, force=force, sleep=sleep, now=now
        )


def _download_corpus(
    client: httpx.Client, discovery: PcgDiscovery, dest: Path, *, sleep: Sleep
) -> tuple[dict[str, dict], int]:
    """Download index and letter pages into ``dest``; returns (files, term count)."""
    pages_dir = dest / PAGES_DIR
    pages_dir.mkdir(parents=True)
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
        client, discovery.index_url, expect="html", sleep=sleep, what="PCG index page"
    )
    index_html = decode_page(index_bytes, "PCG index page")
    edition = index_edition(index_html)
    if edition != (discovery.effective_date, discovery.change):
        raise FetchError(
            f"FAA publications page lists PCG effective {discovery.effective_date} "
            f"change {discovery.change}, but the HTML edition at {discovery.index_url} "
            f"reports effective {edition[0]} change {edition[1]}; upstream may be "
            "mid-update — refusing to accept"
        )
    store(f"{PAGES_DIR}/{INDEX_PAGE}", index_bytes)
    page_names = page_names_from_index(index_html)
    if len(page_names) < MIN_PAGES - 1:
        raise FetchError(
            f"PCG index lists only {len(page_names)} letter pages (expected ≥ {MIN_PAGES - 1}); "
            "upstream layout may have changed"
        )
    pause()

    term_count = 0
    for name in page_names:
        url = urljoin(discovery.index_url, name)
        body, _ = _get_bytes(client, url, expect="html", sleep=sleep, what=f"PCG page {name}")
        html = decode_page(body, f"PCG page {name}")
        defect = _page_defect(name, html)
        if defect is not None:
            raise FetchError(f"PCG page {name}: {defect}; upstream layout may have changed")
        term_count += count_terms(html)
        store(f"{PAGES_DIR}/{name}", body)
        pause()
    if term_count < MIN_TERMS:
        raise FetchError(
            f"only {term_count} term entries found across PCG pages "
            f"(expected ≥ {MIN_TERMS}); download looks incomplete or upstream markup changed"
        )
    fsync_dir(pages_dir)
    return files, term_count


def _publish_snapshot(
    tmp_dir: Path, snapshot_dir: Path, *, new_hash: str, version: str
) -> Path | None:
    """Install the validated download, preserving the previous snapshot first."""
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
                "PCG %s: previous snapshot (%s) preserved at %s", version, existing_hash, superseded
            )
    os.replace(tmp_dir, snapshot_dir)
    fsync_dir(snapshot_dir.parent)
    return preserved


def _fetch_pcg_locked(
    config: Config,
    client: httpx.Client | None,
    *,
    manifest_path: Path,
    force: bool,
    sleep: Sleep,
    now: Callable[[], str],
) -> PcgFetchResult:
    manifest = SourceManifest.load(manifest_path)
    state = manifest.sources[SOURCE_NAME]

    if state.accepted_version is not None and state.raw_hash is not None:
        _reconcile_accepted_archive(config, state.accepted_version, state.raw_hash)

    own_client = client is None
    if client is None:
        client = make_client()
    try:
        discovery = discover_pcg(client, sleep=sleep)
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
                f"FAA lists PCG effective {discovery.effective_date} change {discovery.change}, "
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
            log.info("PCG %s already accepted; cached snapshot verified", version)
            archived = (metadata["edition_label"], metadata["index_url"])
            if state.edition_label is None:
                state.edition_label = archived[0]
            if state.source_url is None:
                state.source_url = archived[1]
            if (state.edition_label, state.source_url) != archived:
                raise FetchError(
                    f"archived PCG snapshot {snapshot_dir} records edition {archived[0]!r} at "
                    f"{archived[1]!r}, but the manifest accepted {state.edition_label!r} at "
                    f"{state.source_url!r}; the archive's provenance was altered — restore "
                    "it, or re-run with --force to re-accept the edition from the FAA"
                )
            state.last_checked_at = checked_at
            manifest.save(manifest_path)
            _check_listing_matches_pins(state, discovery)
            return PcgFetchResult(
                version=version,
                effective_date=discovery.effective_date,
                change=discovery.change,
                label=discovery.label,
                snapshot_dir=snapshot_dir,
                raw_hash=state.raw_hash,
                byte_count=int(metadata.get("byte_count", 0)),
                page_count=int(metadata.get("page_count", 0)),
                term_count=int(metadata.get("term_count", 0)),
                downloaded=False,
            )

        if not force and state.accepted_version == version:
            _check_listing_matches_pins(state, discovery)
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=snapshot_dir.parent, prefix=f".{version}.download-"))
        try:
            log.info(
                "downloading PCG %s (%s) from %s", version, discovery.label, discovery.index_url
            )
            files, term_count = _download_corpus(client, discovery, tmp_dir, sleep=sleep)
            raw_hash = tree_hash({k: v["sha256"] for k, v in files.items()})
            byte_count = sum(v["bytes"] for v in files.values())
            page_count = len(files)
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
                    f"re-fetch of accepted PCG edition {version} returned different content: "
                    f"manifest records {state.raw_hash}, download is {raw_hash}. "
                    f"Downloaded files kept at {quarantine}. "
                    "Investigate, then re-run with --force to accept the new content."
                )
            write_json_durable(
                tmp_dir / "metadata.json",
                {
                    "provider": "faa",
                    "publication": "pcg",
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
                    "term_count": term_count,
                    "files": files,
                },
            )
            if not verify_snapshot(tmp_dir, version=version, raw_hash=raw_hash):
                raise FetchError(
                    "downloaded PCG snapshot failed self-verification before acceptance; "
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
                if previous is not None and _manifest_records(
                    manifest_path,
                    raw_hash=previous[0],
                    edition_label=previous[1],
                    source_url=previous[2],
                ):
                    _restore_known_good(snapshot_dir, version, previous[0])
                raise
            if preserved is not None and previous is not None and previous[0] == raw_hash:
                shutil.rmtree(preserved)
                fsync_dir(snapshot_dir.parent)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

        log.info(
            "accepted PCG %s (%s): %d pages, %d terms, %d bytes",
            version,
            discovery.label,
            page_count,
            term_count,
            byte_count,
        )
        return PcgFetchResult(
            version=version,
            effective_date=discovery.effective_date,
            change=discovery.change,
            label=discovery.label,
            snapshot_dir=snapshot_dir,
            raw_hash=raw_hash,
            byte_count=byte_count,
            page_count=page_count,
            term_count=term_count,
            downloaded=True,
        )
    finally:
        if own_client:
            client.close()


__all__ = [
    "INDEX_PAGE",
    "PAGES_DIR",
    "SOURCE_NAME",
    "PcgDiscovery",
    "PcgFetchResult",
    "classify_page",
    "count_terms",
    "discover_pcg",
    "fetch_pcg",
    "index_edition",
    "listing_matches_pins",
    "load_metadata",
    "metadata_intact",
    "page_names_from_index",
    "page_url",
    "verify_snapshot",
]
