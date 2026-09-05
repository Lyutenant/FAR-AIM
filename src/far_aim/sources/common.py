"""Acquisition helpers shared by every source fetcher (plan §6, §26).

HTTP etiquette (identifying User-Agent, retries with backoff honoring
``Retry-After``), durable filesystem primitives (directory fsync, atomic
JSON writes), the inter-process source lock, and checksum helpers. Nothing
here knows about a particular upstream; ``sources.ecfr`` and ``sources.aim``
build their fetchers on top of it.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from far_aim import __version__
from far_aim.config import Config

log = logging.getLogger(__name__)

USER_AGENT = f"far-aim-vault/{__version__} (FAR/AIM Obsidian knowledge-vault pipeline)"

RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
TRANSIENT_HTTP_ERRORS = (httpx.TransportError, httpx.DecodingError)
# Upper bound on a server-requested Retry-After wait; anything longer is
# treated as "come back later" rather than blocking a scheduled job.
RETRY_AFTER_MAX_SECONDS = 300.0

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

Sleep = Callable[[float], None]


class FetchError(RuntimeError):
    """Source acquisition failed; nothing was accepted into the manifest."""


class RetryableError(Exception):
    """Transient transport/server failure worth another attempt.

    ``retry_after`` carries a server-requested minimum delay (seconds) from
    a ``Retry-After`` header, honored over the default backoff (plan §26).
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def is_calendar_date(value: object) -> bool:
    """True for a real ``YYYY-MM-DD`` calendar date (``2026-02-31`` is rejected)."""
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_retry_after(response: httpx.Response) -> float | None:
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


def retryable_status(response: httpx.Response) -> RetryableError:
    return RetryableError(f"HTTP {response.status_code}", retry_after=parse_retry_after(response))


def make_client() -> httpx.Client:
    """Polite HTTP client (plan §26): identifying User-Agent, sane timeouts."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        follow_redirects=True,
    )


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def retrying(operation, *, what: str, sleep: Sleep, attempts: int | None = None):
    """Run ``operation`` until it stops raising :class:`RetryableError`.

    Backoff doubles from ``RETRY_BACKOFF_SECONDS``; a server ``Retry-After``
    carried by the last failure is honored when longer. After the final
    attempt the failure becomes a :class:`FetchError`.
    """
    if attempts is None:
        attempts = RETRY_ATTEMPTS
    last_error: RetryableError | None = None
    for attempt in range(attempts):
        if attempt:
            delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            if last_error is not None and last_error.retry_after is not None:
                delay = max(delay, last_error.retry_after)
            log.info("%s: retrying in %.0fs (attempt %d/%d)", what, delay, attempt + 1, attempts)
            sleep(delay)
        try:
            return operation()
        except RetryableError as exc:
            last_error = exc
            log.warning("%s: attempt %d failed: %s", what, attempt + 1, exc)
    raise FetchError(f"{what} failed after {attempts} attempts: {last_error}") from last_error


# Errors meaning "this platform/filesystem cannot fsync a directory" — not
# durability failures. Anything else (EIO, ENOSPC, ...) must abort acceptance.
_FSYNC_DIR_UNSUPPORTED = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF, errno.EACCES, errno.EPERM}
)


def fsync_dir(path: Path) -> None:
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


def fetch_lock_path(config: Config) -> Path:
    """Lock file serializing fetches and parses that update the shared manifest."""
    return config.manifests_dir / ".sources.lock"


@contextlib.contextmanager
def exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Exclusive, non-blocking inter-process lock around a fetch or parse.

    Overlapping operations would race on the manifest's read-modify-write
    and on the snapshot/normalized directories; the loser fails closed
    immediately instead of publishing the winner's bytes under its own
    checksum.
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
                f"another far-aim fetch or parse is already in progress (lock {lock_path} held); "
                "wait for it to finish and re-run"
            ) from exc
        yield
    finally:
        os.close(fd)  # releases the lock


# The FAA site is fronted by Akamai, whose bot manager injects three
# elements into HTML responses — a numeric token
# (``<script >bazadebezolkohpepadr="…"</script>``), a loader
# (``<script src="https://www.faa.gov/akam/…" defer></script>``) and a
# ``<noscript>`` tracking pixel (``<img src="https://www.faa.gov/akam/…/
# pixel_…?a=…">``) — whose values vary between edges and deployments, and
# which are absent for some clients altogether. They are CDN
# instrumentation, not FAA content: left in the archive they made the raw
# tree hash of an unchanged edition differ from one download to the next,
# failing every re-verification (plan §32.6). They are removed from HTML
# bodies before archiving and hashing; nothing else in a page is touched.
CDN_INJECTION_PATTERNS = (
    re.compile(rb'<script\s*>bazadebezolkohpepadr="[^"]*"</script>'),
    re.compile(rb'<script\b[^>]*\ssrc="https://www\.faa\.gov/akam/[^"]*"[^>]*></script>'),
    re.compile(
        rb'<noscript><img\b[^>]*\ssrc="https://www\.faa\.gov/akam/[^"]*"[^>]*></noscript>'
    ),
)


def strip_cdn_injection(body: bytes) -> bytes:
    """``body`` without the Akamai bot-manager script tags (see above)."""
    for pattern in CDN_INJECTION_PATTERNS:
        body = pattern.sub(b"", body)
    return body


# Akamai edges cache FAA corpus files for weeks (observed max-age ≈ 25 days).
# When the FAA replaces files without bumping the edition, edges serve a
# mix of old and new copies until their caches expire, so two downloads
# minutes apart disagree. Standard ``Cache-Control: no-cache`` request
# headers do not make the edge revalidate; a query string the edge has not
# seen does. Every corpus request in one download carries the same one-off
# parameter, so the snapshot reflects the origin at that moment. Only the
# request is decorated — archived provenance keeps the canonical URL.
CACHE_BUST_PARAM = "far-aim-nocache"


def download_nonce() -> str:
    return secrets.token_hex(8)


def cache_busted(url: str, nonce: str) -> str:
    """``url`` with the one-off cache-busting query parameter appended."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{CACHE_BUST_PARAM}={nonce}"


def sha256_of(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def sha256_of_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def hash_suffix(raw_hash: str) -> str:
    return raw_hash.removeprefix("sha256:")[:12]


def write_json_durable(path: Path, obj: object) -> None:
    """Deterministic JSON (sorted keys, LF, trailing newline), atomic + fsynced."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)
