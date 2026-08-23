"""eCFR Title 14 acquisition (plan §5.1, §6, §26; Phase 1).

Uses the official eCFR versioner API:

- ``GET /api/versioner/v1/titles.json`` — per-title version metadata; the
  Title 14 entry's ``latest_issue_date`` is the source version.
- ``GET /api/versioner/v1/full/{date}/title-14.xml`` — the complete
  point-in-time Title 14 XML for that issue date.

Accepted snapshots are archived in the gitignored raw cache
(``data/raw/ecfr/{date}/title-14.xml`` plus ``metadata.json``) and recorded
in the committed source manifest. The API is point-in-time, so the
(version, checksum) pair in the manifest is sufficient to reconstruct any
accepted snapshot exactly (plan §6.2) — losing the local cache is safe.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from far_aim import __version__
from far_aim.config import Config
from far_aim.manifest import SourceManifest

log = logging.getLogger(__name__)

ECFR_BASE_URL = "https://www.ecfr.gov"
TITLES_URL = f"{ECFR_BASE_URL}/api/versioner/v1/titles.json"
TITLE_NUMBER = 14
SOURCE_NAME = "ecfr_title_14"

USER_AGENT = f"far-aim-vault/{__version__} (FAR/AIM Obsidian knowledge-vault pipeline)"

# Truncation guards (plan §17.1), data-informed: the 2026-08-19 issue is
# 15,999,434 bytes with 6,363 DIV8 SECTION elements. Anything near these
# floors means a truncated download or an upstream format break — fail
# closed (plan §25.4).
MIN_XML_BYTES = 4_000_000
MIN_SECTION_COUNT = 4_000
EXPECTED_ROOT_TAG = "ECFR"

RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_HTTP_ERRORS = (httpx.TransportError, httpx.DecodingError)
# Upper bound on a server-requested Retry-After wait; anything longer is
# treated as "come back later" rather than blocking a scheduled job.
RETRY_AFTER_MAX_SECONDS = 300.0

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_OPTIONAL_DATES = ("latest_amended_on", "up_to_date_as_of")

Sleep = Callable[[float], None]


class FetchError(RuntimeError):
    """Source acquisition failed; nothing was accepted into the manifest."""


class _RetryableError(Exception):
    """Transient transport/server failure worth another attempt.

    ``retry_after`` carries a server-requested minimum delay (seconds) from
    a ``Retry-After`` header, honored over the default backoff (plan §26).
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _is_calendar_date(value: object) -> bool:
    """True for a real ``YYYY-MM-DD`` calendar date (``2026-02-31`` is rejected)."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait per ``Retry-After`` (delta-seconds or HTTP-date), if present."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    seconds: float | None = None
    if raw.isdigit():
        seconds = float(raw)
    else:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            log.warning("ignoring unparseable Retry-After header: %r", raw)
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), RETRY_AFTER_MAX_SECONDS)


def _retryable_status(response: httpx.Response) -> _RetryableError:
    return _RetryableError(
        f"HTTP {response.status_code}", retry_after=_parse_retry_after(response)
    )


def full_title14_url(version: str) -> str:
    return f"{ECFR_BASE_URL}/api/versioner/v1/full/{version}/title-{TITLE_NUMBER}.xml"


def make_client() -> httpx.Client:
    """Polite HTTP client (plan §26): identifying User-Agent, sane timeouts."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        follow_redirects=True,
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Errors meaning "this platform/filesystem cannot fsync a directory" — not
# durability failures. Anything else (EIO, ENOSPC, ...) must abort acceptance.
_FSYNC_DIR_UNSUPPORTED = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF, errno.EACCES, errno.EPERM}
)


def _fsync_dir(path: Path) -> None:
    """Make renames/creates inside ``path`` durable (POSIX rename semantics).

    A renamed file is only guaranteed to survive power loss once its parent
    directory entry is synced. Platforms that cannot open or fsync a
    directory are tolerated; real I/O errors propagate so acceptance fails
    closed instead of committing a manifest over an unconfirmed rename.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _FSYNC_DIR_UNSUPPORTED:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _FSYNC_DIR_UNSUPPORTED:
            raise
    finally:
        os.close(fd)


@contextlib.contextmanager
def _fetch_lock(lock_path: Path) -> Iterator[None]:
    """Exclusive, non-blocking inter-process lock around a whole fetch.

    Two overlapping fetches would race on the manifest's read-modify-write
    and on the snapshot directory; the loser fails closed immediately
    instead of publishing the winner's bytes under its own checksum.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover - Windows
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise FetchError(
                f"another fetch is already in progress (lock {lock_path} held); "
                "wait for it to finish and re-run"
            ) from exc
        yield
    finally:
        os.close(fd)  # releases the lock


def _retrying(operation, *, what: str, sleep: Sleep):
    last_error: _RetryableError | None = None
    for attempt in range(RETRY_ATTEMPTS):
        if attempt:
            delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            if last_error is not None and last_error.retry_after is not None:
                delay = max(delay, last_error.retry_after)
            log.info(
                "%s: retrying in %.0fs (attempt %d/%d)", what, delay, attempt + 1, RETRY_ATTEMPTS
            )
            sleep(delay)
        try:
            return operation()
        except _RetryableError as exc:
            last_error = exc
            log.warning("%s: attempt %d failed: %s", what, attempt + 1, exc)
    raise FetchError(f"{what} failed after {RETRY_ATTEMPTS} attempts: {last_error}") from last_error


@dataclass(frozen=True)
class TitleDiscovery:
    """Title 14 version metadata from the eCFR titles endpoint."""

    latest_issue_date: str
    latest_amended_on: str | None
    up_to_date_as_of: str | None


def discover_title14(client: httpx.Client, *, sleep: Sleep | None = None) -> TitleDiscovery:
    """Discover the current Title 14 issue date from ``titles.json``."""
    if sleep is None:
        sleep = time.sleep

    def attempt() -> object:
        try:
            response = client.get(TITLES_URL)
        except _TRANSIENT_HTTP_ERRORS as exc:
            raise _RetryableError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP client error fetching {TITLES_URL}: {exc}") from exc
        if response.status_code in RETRYABLE_STATUS:
            raise _retryable_status(response)
        if response.status_code != 200:
            raise FetchError(f"eCFR titles endpoint returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FetchError(f"eCFR titles endpoint returned invalid JSON: {exc}") from exc
        # While the versioner rebuilds its daily snapshot, the title entries
        # may describe a stale or partially imported version that would still
        # pass the coarse size/section-count gate. Wait for the import to
        # finish; fail closed if it never does (plan §32.13).
        meta = payload.get("meta") if isinstance(payload, dict) else None
        if isinstance(meta, dict) and meta.get("import_in_progress"):
            raise _RetryableError("eCFR import in progress (meta.import_in_progress=true)")
        return payload

    payload = _retrying(attempt, what="eCFR titles.json", sleep=sleep)

    titles = payload.get("titles") if isinstance(payload, dict) else None
    if not isinstance(titles, list):
        raise FetchError("eCFR titles endpoint response has no 'titles' list")
    entry = next(
        (t for t in titles if isinstance(t, dict) and t.get("number") == TITLE_NUMBER), None
    )
    if entry is None:
        raise FetchError(f"eCFR titles endpoint response has no entry for Title {TITLE_NUMBER}")
    if entry.get("reserved"):
        raise FetchError(f"eCFR reports Title {TITLE_NUMBER} as reserved; refusing to proceed")
    issue_date = entry.get("latest_issue_date")
    if not _is_calendar_date(issue_date):
        raise FetchError(
            f"Title {TITLE_NUMBER} 'latest_issue_date' is missing or not a valid "
            f"calendar date: {issue_date!r}"
        )

    def optional_date(key: str) -> str | None:
        value = entry.get(key)
        return value if _is_calendar_date(value) else None

    return TitleDiscovery(
        latest_issue_date=issue_date,
        latest_amended_on=optional_date("latest_amended_on"),
        up_to_date_as_of=optional_date("up_to_date_as_of"),
    )


def _sha256_of(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _download_xml(
    client: httpx.Client, url: str, dest: Path, *, sleep: Sleep
) -> tuple[str, int]:
    """Stream ``url`` to ``dest``, returning (sha256 checksum, byte count)."""

    def attempt() -> tuple[str, int]:
        try:
            with client.stream("GET", url) as response:
                if response.status_code in RETRYABLE_STATUS:
                    raise _retryable_status(response)
                if response.status_code != 200:
                    raise FetchError(
                        f"eCFR full-title endpoint returned HTTP {response.status_code}"
                    )
                content_type = response.headers.get("content-type", "")
                if "xml" not in content_type.lower():
                    raise FetchError(
                        f"expected XML from {url}, got content-type {content_type!r}"
                    )
                hasher = hashlib.sha256()
                written = 0
                with dest.open("wb") as fh:
                    for chunk in response.iter_bytes(1 << 16):
                        fh.write(chunk)
                        hasher.update(chunk)
                        written += len(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                # For identity transfers, Content-Length must match the bytes
                # received (plan §25.4). Compressed transfers can't be checked
                # this way; there the HTTP layer itself detects truncation.
                declared = response.headers.get("content-length")
                if declared is not None and "content-encoding" not in response.headers:
                    try:
                        expected = int(declared)
                    except ValueError as exc:
                        raise FetchError(
                            f"malformed Content-Length header from {url}: {declared!r}"
                        ) from exc
                    if expected != written:
                        raise _RetryableError(
                            f"truncated transfer: got {written} of {expected} bytes"
                        )
                if written == 0:
                    raise FetchError(f"empty response from {url}")
                return f"sha256:{hasher.hexdigest()}", written
        except _TRANSIENT_HTTP_ERRORS as exc:
            # Transport failures and corrupt/truncated compressed bodies
            # (DecodingError) are both worth another attempt.
            raise _RetryableError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP client error fetching {url}: {exc}") from exc

    return _retrying(attempt, what=f"eCFR Title {TITLE_NUMBER} download", sleep=sleep)


def _validate_title14_xml(path: Path) -> int:
    """Source-integrity gate (plan §17.1): well-formed, right root, not truncated.

    Returns the number of ``DIV8 TYPE="SECTION"`` elements. This is not the
    Phase 2 parser — only a fail-closed guard that the archived bytes are a
    plausible complete Title 14.
    """
    size = path.stat().st_size
    if size < MIN_XML_BYTES:
        raise FetchError(
            f"downloaded Title {TITLE_NUMBER} XML is suspiciously small "
            f"({size} bytes < {MIN_XML_BYTES}); refusing to accept"
        )
    root_tag: str | None = None
    section_count = 0
    try:
        for event, elem in ET.iterparse(path, events=("start", "end")):
            if root_tag is None:
                root_tag = elem.tag
            if event == "end":
                if elem.tag == "DIV8" and elem.get("TYPE") == "SECTION":
                    section_count += 1
                elem.clear()
    except ET.ParseError as exc:
        raise FetchError(
            f"downloaded Title {TITLE_NUMBER} XML is not well-formed: {exc}"
        ) from exc
    if root_tag != EXPECTED_ROOT_TAG:
        raise FetchError(
            f"unexpected XML root element {root_tag!r} (expected {EXPECTED_ROOT_TAG!r}); "
            "upstream format may have changed"
        )
    if section_count < MIN_SECTION_COUNT:
        raise FetchError(
            f"only {section_count} SECTION elements found (expected ≥ {MIN_SECTION_COUNT}); "
            "download looks incomplete or upstream markup changed"
        )
    return section_count


_METADATA_KEYS = frozenset(
    {
        "provider",
        "title_number",
        "source_version",
        "url",
        "retrieved_at",
        "raw_hash",
        "byte_count",
        "section_count",
        "latest_amended_on",
        "up_to_date_as_of",
    }
)


def _metadata_intact(
    path: Path, *, version: str, raw_hash: str, url: str, byte_count: int
) -> bool:
    """True when metadata.json is a complete provenance record for the accepted bytes.

    Anything less — missing keys, wrong provider/version/hash/size, malformed
    timestamps — is regenerated from the verified archive.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or set(data) != _METADATA_KEYS:
        return False
    retrieved_at = data.get("retrieved_at")
    section_count = data.get("section_count")
    return (
        data.get("provider") == "ecfr"
        and data.get("title_number") == TITLE_NUMBER
        and data.get("source_version") == version
        and data.get("url") == url
        and isinstance(retrieved_at, str)
        and _TIMESTAMP_RE.fullmatch(retrieved_at) is not None
        and data.get("raw_hash") == raw_hash
        and data.get("byte_count") == byte_count
        and isinstance(section_count, int)
        and not isinstance(section_count, bool)
        and section_count >= MIN_SECTION_COUNT
        and all(data.get(k) is None or _is_calendar_date(data.get(k)) for k in _OPTIONAL_DATES)
    )


def _write_metadata(
    path: Path,
    *,
    version: str,
    url: str,
    retrieved_at: str,
    raw_hash: str,
    byte_count: int,
    section_count: int,
    discovery: TitleDiscovery,
) -> None:
    metadata = {
        "provider": "ecfr",
        "title_number": TITLE_NUMBER,
        "source_version": version,
        "url": url,
        "retrieved_at": retrieved_at,
        "raw_hash": raw_hash,
        "byte_count": byte_count,
        "section_count": section_count,
        "latest_amended_on": discovery.latest_amended_on,
        "up_to_date_as_of": discovery.up_to_date_as_of,
    }
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _publish_xml(tmp_path: Path, xml_path: Path, *, new_hash: str, version: str) -> None:
    """Move the validated download into the archive path, preserving differing old bytes.

    If upstream changed this point-in-time URL in place, the old bytes may no
    longer be reconstructible from the API, so the last known-good snapshot
    is kept under a hash-qualified name (plan §32.13) before overwriting.
    """
    if xml_path.exists():
        existing_hash = _sha256_of(xml_path)
        if existing_hash != new_hash:
            superseded = _superseded_path(xml_path, existing_hash)
            os.replace(xml_path, superseded)
            # The superseded copy is the only recoverable last known-good;
            # make that rename durable before the replacement is installed.
            _fsync_dir(xml_path.parent)
            log.warning(
                "eCFR Title 14 %s: previous snapshot (%s) preserved at %s",
                version,
                existing_hash,
                superseded,
            )
    os.replace(tmp_path, xml_path)
    _fsync_dir(xml_path.parent)


def _reconcile_accepted_archive(xml_path: Path, accepted_hash: str) -> None:
    """Put the accepted bytes back at their canonical path if they were displaced.

    Runs before discovery on every fetch, so recovery from an interrupted
    same-version acceptance does not depend on upstream still reporting that
    version, nor on the network at all. If the archive is simply missing (no
    preserved copy either) nothing is done; the point-in-time API can
    re-supply it.
    """
    if xml_path.exists() and _sha256_of(xml_path) == accepted_hash:
        return
    _restore_known_good(xml_path, accepted_hash)


def _manifest_records(manifest_path: Path, *, raw_hash: str) -> bool:
    """True if the on-disk manifest still records ``raw_hash`` for this source."""
    try:
        return SourceManifest.load(manifest_path).sources[SOURCE_NAME].raw_hash == raw_hash
    except Exception:  # noqa: BLE001 - unreadable manifest: cannot prove the old state
        return False


def _hash_suffix(raw_hash: str) -> str:
    return raw_hash.removeprefix("sha256:")[:12]


def _superseded_path(xml_path: Path, raw_hash: str) -> Path:
    return xml_path.with_name(f"title-14.xml.superseded-{_hash_suffix(raw_hash)}")


def _restore_known_good(xml_path: Path, known_good_hash: str) -> bool:
    """Roll the archive back to the preserved last known-good bytes, if available.

    Used when ``xml_path`` does not hold the manifest's accepted hash — e.g.
    an interruption between archive publish and manifest commit. The bytes
    currently at ``xml_path`` (validated but never accepted) are kept as
    ``title-14.xml.unaccepted-<hash>``. Returns True if ``xml_path`` now
    holds ``known_good_hash``.
    """
    superseded = _superseded_path(xml_path, known_good_hash)
    if not superseded.exists() or _sha256_of(superseded) != known_good_hash:
        return False
    if xml_path.exists():
        current_hash = _sha256_of(xml_path)
        unaccepted = xml_path.with_name(f"title-14.xml.unaccepted-{_hash_suffix(current_hash)}")
        os.replace(xml_path, unaccepted)
        log.warning(
            "archive %s held unaccepted bytes (%s); moved to %s", xml_path, current_hash, unaccepted
        )
    os.replace(superseded, xml_path)
    _fsync_dir(xml_path.parent)
    log.warning("restored last known-good snapshot (%s) to %s", known_good_hash, xml_path)
    return True


@dataclass(frozen=True)
class FetchResult:
    version: str
    xml_path: Path
    raw_hash: str
    byte_count: int
    section_count: int | None
    downloaded: bool


def fetch_title14(
    config: Config,
    client: httpx.Client | None = None,
    *,
    force: bool = False,
    sleep: Sleep | None = None,
    now: Callable[[], str] = _utc_now_iso,
) -> FetchResult:
    """Check → download → archive raw → hash → update manifest (plan §6).

    Idempotent: if the manifest's accepted version matches upstream and the
    cached snapshot's checksum verifies, nothing is re-downloaded and only
    ``last_checked_at`` moves (plan Phase 1 exit criteria).
    """
    if sleep is None:
        sleep = time.sleep
    manifest_path = config.manifest_path
    if not manifest_path.exists():
        raise FetchError(f"source registry missing: {manifest_path}")
    with _fetch_lock(fetch_lock_path(config)):
        return _fetch_title14_locked(
            config, client, manifest_path=manifest_path, force=force, sleep=sleep, now=now
        )


def fetch_lock_path(config: Config) -> Path:
    """Lock file serializing all fetches that update the shared manifest."""
    return config.manifests_dir / ".sources.lock"


def _fetch_title14_locked(
    config: Config,
    client: httpx.Client | None,
    *,
    manifest_path: Path,
    force: bool,
    sleep: Sleep,
    now: Callable[[], str],
) -> FetchResult:
    manifest = SourceManifest.load(manifest_path)
    state = manifest.sources[SOURCE_NAME]

    # Reconcile the *accepted* snapshot's archive with the manifest before
    # anything else — offline, and independent of whatever version upstream
    # reports next. An interrupted acceptance must never leave the last
    # known-good snapshot displaced at its canonical path.
    if state.accepted_version is not None and state.raw_hash is not None:
        _reconcile_accepted_archive(
            config.raw_dir / "ecfr" / state.accepted_version / "title-14.xml", state.raw_hash
        )

    own_client = client is None
    if client is None:
        client = make_client()
    try:
        discovery = discover_title14(client, sleep=sleep)
        version = discovery.latest_issue_date
        checked_at = now()
        snapshot_dir = config.raw_dir / "ecfr" / version
        xml_path = snapshot_dir / "title-14.xml"
        metadata_path = snapshot_dir / "metadata.json"
        url = full_title14_url(version)

        if not force and state.accepted_version is not None and version < state.accepted_version:
            # Issue dates only move forward; an older date from upstream means
            # an eCFR glitch or a rollback, never routine progress. Accepting
            # it would silently regress authoritative content (plan §32.13).
            raise FetchError(
                f"upstream reports issue date {version}, older than accepted "
                f"{state.accepted_version}; refusing to roll back automatically. "
                "Investigate upstream, then re-run with --force to accept it."
            )

        xml_cache_intact = (
            not force
            and state.accepted_version == version
            and state.raw_hash is not None
            and xml_path.exists()
            and _sha256_of(xml_path) == state.raw_hash
        )
        if xml_cache_intact:
            section_count: int | None = None
            if not _metadata_intact(
                metadata_path,
                version=version,
                raw_hash=state.raw_hash,
                url=url,
                byte_count=xml_path.stat().st_size,
            ):
                # The XML checksum verifies but the snapshot's metadata is
                # missing or stale (e.g. partial cache cleanup): rebuild it
                # from the verified bytes so the archive stays complete.
                # retrieved_at is best-effort here; the original fetch time
                # was lost with the metadata file.
                section_count = _validate_title14_xml(xml_path)
                _write_metadata(
                    metadata_path,
                    version=version,
                    url=url,
                    retrieved_at=checked_at,
                    raw_hash=state.raw_hash,
                    byte_count=xml_path.stat().st_size,
                    section_count=section_count,
                    discovery=discovery,
                )
                log.info("eCFR Title 14 %s: regenerated missing/stale metadata.json", version)
            log.info("eCFR Title 14 %s already accepted; cached snapshot verified", version)
            state.last_checked_at = checked_at
            manifest.save(manifest_path)
            return FetchResult(
                version=version,
                xml_path=xml_path,
                raw_hash=state.raw_hash,
                byte_count=xml_path.stat().st_size,
                section_count=section_count,
                downloaded=False,
            )

        snapshot_dir.mkdir(parents=True, exist_ok=True)
        # Unique per process: even if the lock were bypassed, two fetches can
        # never hash one inode while writing another's bytes into it.
        fd, tmp_name = tempfile.mkstemp(
            dir=snapshot_dir, prefix="title-14.xml.", suffix=".download"
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            log.info("downloading eCFR Title 14 issue %s from %s", version, url)
            raw_hash, byte_count = _download_xml(client, url, tmp_path, sleep=sleep)
            section_count = _validate_title14_xml(tmp_path)
            if (
                not force
                and state.accepted_version == version
                and state.raw_hash is not None
                and raw_hash != state.raw_hash
            ):
                # Same issue date, different bytes: either the earlier archive was
                # corrupt or upstream changed content in place. Fail closed
                # (plan §32.13) and keep the bytes for inspection.
                quarantine = snapshot_dir / "title-14.xml.mismatch"
                os.replace(tmp_path, quarantine)
                raise FetchError(
                    f"re-fetch of accepted version {version} returned different content: "
                    f"manifest records {state.raw_hash}, download is {raw_hash}. "
                    f"Downloaded bytes kept at {quarantine}. "
                    "Investigate, then re-run with --force to accept the new content."
                )

            # Acceptance order: the manifest commit is last, so it never
            # records a snapshot that is not durably archived, and every
            # interrupted state self-heals on the next fetch without looping
            # into the mismatch quarantine:
            #   1. metadata.json (new)  — crash: manifest + archive still the
            #      old snapshot; stale metadata is regenerated from it.
            #   2. archive publish      — old differing bytes first preserved
            #      as title-14.xml.superseded-<hash>. Crash: manifest old,
            #      archive new; the next fetch (or the in-process rollback
            #      below) restores the preserved bytes, parking the new ones
            #      as title-14.xml.unaccepted-<hash>.
            #   3. manifest (commit)    — atomic; after this, archive,
            #      metadata and manifest all describe the same bytes.
            _write_metadata(
                metadata_path,
                version=version,
                url=url,
                retrieved_at=checked_at,
                raw_hash=raw_hash,
                byte_count=byte_count,
                section_count=section_count,
                discovery=discovery,
            )
            previous_hash = state.raw_hash if state.accepted_version == version else None
            try:
                _publish_xml(tmp_path, xml_path, new_hash=raw_hash, version=version)
                state.last_checked_at = checked_at
                if state.accepted_version != version or state.raw_hash != raw_hash:
                    # The canonical layer (Phase 2) no longer corresponds to
                    # the accepted raw bytes; drop the stale pairing.
                    state.canonical_hash = None
                state.accepted_version = version
                state.raw_hash = raw_hash
                manifest.save(manifest_path)
            except BaseException:
                # Roll the archive back only if acceptance verifiably did not
                # commit. manifest.save() may fail *after* its rename (e.g. the
                # directory fsync); then the visible manifest already records
                # the new hash and the new archive must stay in place.
                if (
                    previous_hash is not None
                    and previous_hash != raw_hash
                    and _manifest_records(manifest_path, raw_hash=previous_hash)
                ):
                    _restore_known_good(xml_path, previous_hash)
                raise
        finally:
            tmp_path.unlink(missing_ok=True)

        log.info(
            "accepted eCFR Title 14 issue %s (%d bytes, %d sections)",
            version,
            byte_count,
            section_count,
        )
        return FetchResult(
            version=version,
            xml_path=xml_path,
            raw_hash=raw_hash,
            byte_count=byte_count,
            section_count=section_count,
            downloaded=True,
        )
    finally:
        if own_client:
            client.close()
