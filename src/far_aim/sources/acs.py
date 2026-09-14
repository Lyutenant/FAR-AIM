"""Private Pilot Airplane ACS acquisition from the FAA (plan §39.2; Phase 10a).

The Airman Certification Standards page
(``https://www.faa.gov/training_testing/testing/acs``) lists every current
standard in one table — title with document number, publication date,
change date, status — and links each to a PDF. The FAA publishes the ACS
**only** as a PDF, so this fetcher is the plan's one bounded deviation from
HTML-first ingestion (§5.3, §39.2): the PDF must carry a text layer (no
OCR, ever), the archived artifact is the PDF byte-for-byte, and the
extractor of record is a pinned ``pypdf`` used only to validate the
download here (page count, cover page, element-code floor) and, in
``parsers.acs``, to read it.

Discovery: the row whose title is exactly the publication's
(``Private Pilot for Airplane Category (FAA-S-ACS-6C)``) gives the document
number — the **version** — the PDF URL and the status's effective date. The
PDF link is edition-agnostic (``private_airplane_acs_6.pdf``), so the cover
page is cross-checked against the listed document number before anything
is accepted (the AIM's index-summary cross-check, §14.2).

Accepted snapshots live in the gitignored raw cache under
``data/raw/acs/{document-number}/`` as the PDF plus ``metadata.json``; the
manifest records the PDF's SHA-256 as ``raw_hash``. The FAA offers no
point-in-time access, so accepted snapshots must additionally be archived
outside this repository (plan §6.2). Quarantine on re-fetch mismatch,
superseded-snapshot preservation and offline reconciliation mirror
``sources.pcg``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import httpx
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from far_aim.config import Config
from far_aim.htmltree import HTMLStructureError, parse_html
from far_aim.manifest import SourceManifest
from far_aim.sources import faa_publications
from far_aim.sources.aim import tree_hash
from far_aim.sources.common import (
    RETRYABLE_STATUS,
    TIMESTAMP_RE,
    TRANSIENT_HTTP_ERRORS,
    FetchError,
    RetryableError,
    Sleep,
    cache_busted,
    download_nonce,
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

SOURCE_NAME = "acs_private_airplane"
ACS_PAGE_URL = "https://www.faa.gov/training_testing/testing/acs"
PUBLICATION_TITLE = "Private Pilot for Airplane Category"
PUBLICATION_KEY = "private-airplane"

# Truncation / layout-change guards (plan §17.1, §25.1), data-informed: the
# FAA-S-ACS-6C PDF has 87 pages and codes 1,300+ elements (every element
# line plus the Major Enhancements grid). Anything near these floors means
# a truncated download or a redesigned document — fail closed.
MIN_PAGES = 60
MIN_ELEMENTS = 800

DOCUMENT_NUMBER_RE = re.compile(r"^FAA-S-ACS-(?P<number>\d+)(?P<revision>[A-Z]?)$")
_TITLE_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<number>FAA-S-ACS-\d+[A-Z]?)\)$")
_EFFECTIVE_RE = re.compile(
    r"^(?:Effective\s+)?(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})$"
)
_PUBLICATION_DATE_RE = re.compile(r"^[A-Z][a-z]+ \d{4}$")
_ELEMENT_CODE_RE = re.compile(r"\b[A-Z]{2,3}\.[IVX]+\.[A-Z]\.[KRS]\d+[a-z]?\b")
_WS_RE = re.compile(r"\s+")
_MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}


@dataclass(frozen=True)
class AcsDiscovery:
    """The current Private Pilot Airplane ACS as listed on the FAA ACS page."""

    title: str
    document_number: str
    publication_date: str  # as listed ("November 2023")
    effective_date: str  # ISO, from the status column
    pdf_url: str

    @property
    def version(self) -> str:
        return self.document_number

    @property
    def label(self) -> str:
        return f"{self.title} ({self.document_number})"


def document_number_key(number: str) -> tuple[int, str]:
    """``FAA-S-ACS-6C`` → ``(6, "C")``: revisions order by letter, base first."""
    match = DOCUMENT_NUMBER_RE.match(number)
    if match is None:
        raise ValueError(f"not an ACS document number: {number!r}")
    return int(match.group("number")), match.group("revision")


def _flat(text: str) -> str:
    return _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()


def _effective_date(status: str) -> str | None:
    match = _EFFECTIVE_RE.match(_flat(status))
    if match is None:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    try:
        return date(int(match.group("year")), month, int(match.group("day"))).isoformat()
    except ValueError:
        return None


def parse_acs_page(
    html: str, base_url: str = ACS_PAGE_URL, title: str = PUBLICATION_TITLE
) -> AcsDiscovery:
    """The listing for ``title`` on the FAA ACS page, strictly.

    Exactly one table row must carry the title with a parseable document
    number, a PDF link on the FAA origin, a publication date and an
    effective date, or discovery fails closed (a redesigned page must never
    accept the wrong document).
    """
    try:
        root = parse_html(html)
    except HTMLStructureError as exc:
        raise FetchError(f"FAA ACS page: malformed or truncated HTML ({exc})") from exc
    found: list[AcsDiscovery] = []
    for row in root.find_all(lambda n: n.tag == "tr"):
        cells = [c for c in row.elements() if c.tag == "td"]
        if len(cells) < 4:
            continue
        anchor = next((c for c in cells[0].elements() if c.tag == "a"), None)
        if anchor is None or not anchor.get("href"):
            continue
        match = _TITLE_RE.match(_flat(anchor.text()))
        if match is None or match.group("title") != title:
            continue
        number = match.group("number")
        if DOCUMENT_NUMBER_RE.match(number) is None:
            raise FetchError(f"FAA ACS page lists {title!r} with document number {number!r}")
        pdf_url = urljoin(base_url, anchor.get("href") or "")
        if faa_publications.origin_defect(pdf_url) is not None:
            raise FetchError(
                f"{title} listing resolves outside the approved FAA origin: {pdf_url!r}"
            )
        if not pdf_url.lower().endswith(".pdf"):
            raise FetchError(f"{title} listing does not link a PDF: {pdf_url!r}")
        publication_date = _flat(cells[1].text())
        if _PUBLICATION_DATE_RE.match(publication_date) is None:
            raise FetchError(
                f"{title} listing has an unparseable publication date {publication_date!r}"
            )
        effective = _effective_date(cells[3].text())
        if effective is None:
            raise FetchError(
                f"{title} listing has no parseable effective date in its status "
                f"{_flat(cells[3].text())!r}"
            )
        found.append(
            AcsDiscovery(
                title=title,
                document_number=number,
                publication_date=publication_date,
                effective_date=effective,
                pdf_url=pdf_url,
            )
        )
    if len(found) != 1:
        raise FetchError(
            f"expected exactly one listing for {title!r} on the FAA ACS page, found "
            f"{len(found)}; upstream page layout may have changed"
        )
    return found[0]


def fetch_acs_page(client: httpx.Client, *, sleep: Sleep | None = None) -> str:
    if sleep is None:
        sleep = time.sleep

    def attempt() -> str:
        try:
            response = client.get(ACS_PAGE_URL)
        except TRANSIENT_HTTP_ERRORS as exc:
            raise RetryableError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP client error fetching {ACS_PAGE_URL}: {exc}") from exc
        if response.status_code in RETRYABLE_STATUS:
            raise retryable_status(response)
        if response.status_code != 200:
            raise FetchError(f"FAA ACS page returned HTTP {response.status_code}")
        faa_publications.check_response_origin(response, "FAA ACS page")
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            raise FetchError(
                f"expected HTML from {ACS_PAGE_URL}, got content-type {content_type!r}"
            )
        return response.text

    return retrying(attempt, what="FAA ACS page", sleep=sleep)


def discover_acs(client: httpx.Client, *, sleep: Sleep | None = None) -> AcsDiscovery:
    return parse_acs_page(fetch_acs_page(client, sleep=sleep))


# ---------------------------------------------------------------------------
# PDF validation
# ---------------------------------------------------------------------------


def open_pdf(data: bytes, what: str) -> PdfReader:
    """A strict reader over ``data``; unreadable or truncated PDFs are fetch failures."""
    if not data.startswith(b"%PDF-"):
        raise FetchError(f"{what}: not a PDF (missing %PDF- header)")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise FetchError(f"{what}: PDF is encrypted")
        page_count = len(reader.pages)
    except (PyPdfError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise FetchError(f"{what}: unreadable PDF ({exc})") from exc
    if page_count == 0:
        raise FetchError(f"{what}: PDF has no pages")
    return reader


def page_text(reader: PdfReader, index: int) -> str:
    """Text of one page as the parser reads it (layout mode; see ``parsers.acs``)."""
    try:
        return reader.pages[index].extract_text(extraction_mode="layout")
    except (PyPdfError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise FetchError(f"PDF page {index + 1}: text extraction failed ({exc})") from exc


def count_elements(reader: PdfReader) -> int:
    """Element codes (``PA.I.A.K1``) across every page — the truncation-floor metric."""
    total = 0
    for index in range(len(reader.pages)):
        total += len(_ELEMENT_CODE_RE.findall(page_text(reader, index)))
    return total


def _pdf_defect(data: bytes, document_number: str, what: str) -> tuple[int, int]:
    """Validate a downloaded ACS PDF; returns (page count, element count)."""
    reader = open_pdf(data, what)
    page_count = len(reader.pages)
    if page_count < MIN_PAGES:
        raise FetchError(
            f"{what}: only {page_count} pages (expected ≥ {MIN_PAGES}); download looks "
            "truncated or the document was restructured"
        )
    cover = page_text(reader, 0)
    if not cover.strip():
        raise FetchError(
            f"{what}: the cover page has no text layer (a scanned PDF is never accepted)"
        )
    if document_number not in cover:
        raise FetchError(
            f"{what}: the cover page does not name document {document_number}, which the "
            "FAA ACS page lists; upstream may be mid-update — refusing to accept"
        )
    elements = count_elements(reader)
    if elements < MIN_ELEMENTS:
        raise FetchError(
            f"{what}: only {elements} element codes found (expected ≥ {MIN_ELEMENTS}); "
            "download looks incomplete or the document was restructured"
        )
    return page_count, elements


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_bytes(client: httpx.Client, url: str, *, expect: str, sleep: Sleep, what: str) -> bytes:
    def attempt() -> bytes:
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
        return body

    return retrying(attempt, what=what, sleep=sleep)


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------

_METADATA_KEYS = frozenset(
    {
        "provider",
        "publication",
        "acs",
        "source_version",
        "document_number",
        "title",
        "edition_label",
        "publication_date",
        "effective_date",
        "pdf_url",
        "acs_page_url",
        "retrieved_at",
        "raw_hash",
        "byte_count",
        "page_count",
        "element_count",
        "file",
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
    counts = (metadata.get("byte_count"), metadata.get("page_count"), metadata.get("element_count"))
    return (
        metadata.get("provider") == "faa"
        and metadata.get("publication") == "acs"
        and metadata.get("acs") == PUBLICATION_KEY
        and metadata.get("source_version") == version
        and metadata.get("document_number") == version
        and DOCUMENT_NUMBER_RE.match(str(version)) is not None
        and isinstance(metadata.get("title"), str)
        and metadata.get("edition_label") == f"{metadata.get('title')} ({version})"
        and isinstance(metadata.get("publication_date"), str)
        and is_calendar_date(metadata.get("effective_date"))
        and isinstance(metadata.get("pdf_url"), str)
        and isinstance(metadata.get("acs_page_url"), str)
        and isinstance(metadata.get("retrieved_at"), str)
        and TIMESTAMP_RE.fullmatch(metadata["retrieved_at"]) is not None
        and metadata.get("raw_hash") == raw_hash
        and all(isinstance(c, int) and not isinstance(c, bool) for c in counts)
        and isinstance(metadata.get("file"), str)
        and "/" not in metadata["file"]
        and metadata["file"].lower().endswith(".pdf")
    )


def verify_snapshot(snapshot_dir: Path, *, version: str, raw_hash: str) -> bool:
    """True when the archived PDF is present, hashes to ``raw_hash`` and still
    clears the integrity floors its metadata records (recomputed, so an edited
    ``metadata.json`` cannot pass off as a verified snapshot)."""
    metadata = load_metadata(snapshot_dir)
    if not metadata_intact(metadata, version=version, raw_hash=raw_hash):
        return False
    assert isinstance(metadata, dict)
    pdf_path = snapshot_dir / metadata["file"]
    try:
        size = pdf_path.stat().st_size
    except OSError:
        return False
    if size != metadata["byte_count"] or sha256_of(pdf_path) != raw_hash:
        return False
    keep = (metadata["file"], "metadata.json")
    if any(p.is_file() and p.name not in keep for p in snapshot_dir.iterdir()):
        return False
    try:
        page_count, element_count = _pdf_defect(pdf_path.read_bytes(), version, "archived ACS PDF")
    except (FetchError, OSError):
        return False
    return page_count == metadata["page_count"] and element_count == metadata["element_count"]


def _snapshot_dir(config: Config, version: str) -> Path:
    return config.raw_dir / "acs" / version


def _current_hash(snapshot_dir: Path) -> str:
    """Whatever an (unverified) snapshot directory hashes to, for naming a
    preserved copy: the sole PDF's checksum, else a tree hash of its files."""
    pdfs = sorted(p for p in snapshot_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    if len(pdfs) == 1:
        return sha256_of(pdfs[0])
    files = {p.name: sha256_of(p) for p in sorted(snapshot_dir.iterdir()) if p.is_file()}
    return tree_hash(files)


def _superseded_dir(snapshot_dir: Path, raw_hash: str) -> Path:
    return snapshot_dir.with_name(f"{snapshot_dir.name}.superseded-{hash_suffix(raw_hash)}")


def _restore_known_good(snapshot_dir: Path, version: str, known_good_hash: str) -> bool:
    superseded = _superseded_dir(snapshot_dir, known_good_hash)
    if not superseded.is_dir() or not verify_snapshot(
        superseded, version=version, raw_hash=known_good_hash
    ):
        return False
    if snapshot_dir.exists():
        current = _current_hash(snapshot_dir)
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
    log.warning("restored last known-good ACS snapshot (%s) to %s", known_good_hash, snapshot_dir)
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


def listing_matches_pins(state, discovery: AcsDiscovery) -> bool:
    """False when the FAA lists the accepted document under a different label,
    URL or effective date (all three are rendered provenance)."""
    pins = (state.edition_label, state.source_url, state.effective_date)
    if None in pins:
        return True
    return pins == (discovery.label, discovery.pdf_url, discovery.effective_date)


def _check_listing_matches_pins(state, discovery: AcsDiscovery) -> None:
    if listing_matches_pins(state, discovery):
        return
    raise FetchError(
        f"FAA now lists ACS {discovery.version} as {discovery.label!r} at "
        f"{discovery.pdf_url!r} effective {discovery.effective_date}, but the accepted "
        f"snapshot was recorded as {state.edition_label!r} at {state.source_url!r} "
        f"effective {state.effective_date}; re-run with --force to re-accept the "
        "document under its current listing"
    )


@dataclass(frozen=True)
class AcsFetchResult:
    version: str
    label: str
    effective_date: str
    snapshot_dir: Path
    pdf_path: Path
    raw_hash: str
    byte_count: int
    page_count: int
    element_count: int
    downloaded: bool


def fetch_acs(
    config: Config,
    client: httpx.Client | None = None,
    *,
    force: bool = False,
    sleep: Sleep | None = None,
    now: Callable[[], str] = utc_now_iso,
) -> AcsFetchResult:
    """Discover → download PDF → validate → archive → update manifest.

    Idempotent: when the manifest's accepted document matches the FAA
    listing and the archived PDF verifies, nothing is downloaded and only
    ``last_checked_at`` moves.
    """
    if sleep is None:
        sleep = time.sleep
    manifest_path = config.manifest_path
    if not manifest_path.exists():
        raise FetchError(f"source registry missing: {manifest_path}")
    with exclusive_lock(fetch_lock_path(config)):
        return _fetch_acs_locked(
            config, client, manifest_path=manifest_path, force=force, sleep=sleep, now=now
        )


def _publish_snapshot(
    tmp_dir: Path, snapshot_dir: Path, *, new_hash: str, version: str
) -> Path | None:
    preserved: Path | None = None
    if snapshot_dir.exists():
        existing_hash = _current_hash(snapshot_dir)
        superseded = _superseded_dir(snapshot_dir, existing_hash)
        if superseded.exists():
            shutil.rmtree(superseded)
        os.replace(snapshot_dir, superseded)
        fsync_dir(snapshot_dir.parent)
        preserved = superseded
        if existing_hash != new_hash:
            log.warning(
                "ACS %s: previous snapshot (%s) preserved at %s", version, existing_hash, superseded
            )
    os.replace(tmp_dir, snapshot_dir)
    fsync_dir(snapshot_dir.parent)
    return preserved


def pdf_filename(pdf_url: str) -> str:
    """The archived file name: the URL's own basename (``private_airplane_acs_6.pdf``)."""
    name = pdf_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not name.lower().endswith(".pdf") or "/" in name or name.startswith("."):
        raise FetchError(f"ACS PDF URL has no usable file name: {pdf_url!r}")
    return name


def _fetch_acs_locked(
    config: Config,
    client: httpx.Client | None,
    *,
    manifest_path: Path,
    force: bool,
    sleep: Sleep,
    now: Callable[[], str],
) -> AcsFetchResult:
    manifest = SourceManifest.load(manifest_path)
    state = manifest.sources[SOURCE_NAME]

    if state.accepted_version is not None and state.raw_hash is not None:
        _reconcile_accepted_archive(config, state.accepted_version, state.raw_hash)

    own_client = client is None
    if client is None:
        client = make_client()
    try:
        discovery = discover_acs(client, sleep=sleep)
        version = discovery.version
        checked_at = now()
        snapshot_dir = _snapshot_dir(config, version)
        file_name = pdf_filename(discovery.pdf_url)

        if (
            not force
            and state.accepted_version is not None
            and DOCUMENT_NUMBER_RE.match(state.accepted_version) is not None
            and document_number_key(version) < document_number_key(state.accepted_version)
        ):
            raise FetchError(
                f"FAA lists ACS {version}, older than accepted {state.accepted_version}; "
                "refusing to roll back automatically. Investigate upstream, then re-run "
                "with --force."
            )

        if (
            not force
            and state.accepted_version == version
            and state.raw_hash is not None
            and verify_snapshot(snapshot_dir, version=version, raw_hash=state.raw_hash)
        ):
            metadata = load_metadata(snapshot_dir) or {}
            log.info("ACS %s already accepted; cached snapshot verified", version)
            archived = (metadata["edition_label"], metadata["pdf_url"])
            if state.edition_label is None:
                state.edition_label = archived[0]
            if state.source_url is None:
                state.source_url = archived[1]
            if (state.edition_label, state.source_url) != archived:
                raise FetchError(
                    f"archived ACS snapshot {snapshot_dir} records {archived[0]!r} at "
                    f"{archived[1]!r}, but the manifest accepted {state.edition_label!r} at "
                    f"{state.source_url!r}; the archive's provenance was altered — restore "
                    "it, or re-run with --force to re-accept the document from the FAA"
                )
            state.last_checked_at = checked_at
            manifest.save(manifest_path)
            _check_listing_matches_pins(state, discovery)
            return AcsFetchResult(
                version=version,
                label=discovery.label,
                effective_date=discovery.effective_date,
                snapshot_dir=snapshot_dir,
                pdf_path=snapshot_dir / metadata["file"],
                raw_hash=state.raw_hash,
                byte_count=int(metadata.get("byte_count", 0)),
                page_count=int(metadata.get("page_count", 0)),
                element_count=int(metadata.get("element_count", 0)),
                downloaded=False,
            )

        if not force and state.accepted_version == version:
            _check_listing_matches_pins(state, discovery)
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=snapshot_dir.parent, prefix=f".{version}.download-"))
        try:
            log.info("downloading ACS %s (%s) from %s", version, discovery.label, discovery.pdf_url)
            # Cache-busted like every FAA corpus request (``sources.common``).
            data = _get_bytes(
                client,
                cache_busted(discovery.pdf_url, download_nonce()),
                expect="pdf",
                sleep=sleep,
                what=f"ACS PDF {file_name}",
            )
            page_count, element_count = _pdf_defect(data, version, f"ACS PDF {file_name}")
            raw_hash = sha256_of_bytes(data)
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
                (tmp_dir / file_name).write_bytes(data)
                os.replace(tmp_dir, quarantine)
                raise FetchError(
                    f"re-fetch of accepted ACS {version} returned different content: "
                    f"manifest records {state.raw_hash}, download is {raw_hash}. "
                    f"Downloaded file kept at {quarantine}. "
                    "Investigate, then re-run with --force to accept the new content."
                )
            pdf_path = tmp_dir / file_name
            with pdf_path.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            write_json_durable(
                tmp_dir / "metadata.json",
                {
                    "provider": "faa",
                    "publication": "acs",
                    "acs": PUBLICATION_KEY,
                    "source_version": version,
                    "document_number": version,
                    "title": discovery.title,
                    "edition_label": discovery.label,
                    "publication_date": discovery.publication_date,
                    "effective_date": discovery.effective_date,
                    "pdf_url": discovery.pdf_url,
                    "acs_page_url": ACS_PAGE_URL,
                    "retrieved_at": checked_at,
                    "raw_hash": raw_hash,
                    "byte_count": len(data),
                    "page_count": page_count,
                    "element_count": element_count,
                    "file": file_name,
                },
            )
            fsync_dir(tmp_dir)
            if not verify_snapshot(tmp_dir, version=version, raw_hash=raw_hash):
                raise FetchError(
                    "downloaded ACS snapshot failed self-verification before acceptance; "
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
                    or state.source_url != discovery.pdf_url
                    or state.effective_date != discovery.effective_date
                ):
                    # A changed document — or the same bytes re-accepted under
                    # a changed listing — invalidates the parsed layer, whose
                    # provenance `validate` pins against the manifest.
                    state.canonical_hash = None
                state.accepted_version = version
                state.effective_date = discovery.effective_date
                state.change = None
                state.raw_hash = raw_hash
                state.edition_label = discovery.label
                state.source_url = discovery.pdf_url
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
            "accepted ACS %s (%s): %d pages, %d element codes, %d bytes",
            version,
            discovery.label,
            page_count,
            element_count,
            len(data),
        )
        return AcsFetchResult(
            version=version,
            label=discovery.label,
            effective_date=discovery.effective_date,
            snapshot_dir=snapshot_dir,
            pdf_path=snapshot_dir / file_name,
            raw_hash=raw_hash,
            byte_count=len(data),
            page_count=page_count,
            element_count=element_count,
            downloaded=True,
        )
    finally:
        if own_client:
            client.close()


def page_url(pdf_url: str, page: int | None = None) -> str:
    """The PDF URL, opened at ``page`` (1-based) when given (``…acs_6.pdf#page=10``)."""
    return pdf_url if page is None else f"{pdf_url}#page={page}"


__all__ = [
    "ACS_PAGE_URL",
    "MIN_ELEMENTS",
    "MIN_PAGES",
    "PUBLICATION_KEY",
    "PUBLICATION_TITLE",
    "SOURCE_NAME",
    "AcsDiscovery",
    "AcsFetchResult",
    "count_elements",
    "discover_acs",
    "document_number_key",
    "fetch_acs",
    "listing_matches_pins",
    "load_metadata",
    "metadata_intact",
    "open_pdf",
    "page_text",
    "page_url",
    "parse_acs_page",
    "pdf_filename",
    "verify_snapshot",
]
