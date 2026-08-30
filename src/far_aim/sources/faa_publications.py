"""FAA Air Traffic Plans and Publications page: edition discovery (plan §5.2, §14.2).

``https://www.faa.gov/air_traffic/publications/`` is the version source for
the AIM and the Pilot/Controller Glossary. Each publication is listed as

    <li><a href="atpubs/aim_html/index.html">Aeronautical Information Manual
    (AIM) Basic with Change 1, 2 and 3</a> (HTML) (effective 7/9/2026)</li>

so the current edition label, change number, effective date, and HTML
target are read from the landing page rather than assumed from a dated URL
(plan §5.2). Parsing is deliberately strict: exactly one HTML listing per
publication, with a parseable effective date, or discovery fails closed.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from far_aim.htmltree import HTMLStructureError, Node, parse_html
from far_aim.sources.common import (
    RETRYABLE_STATUS,
    TRANSIENT_HTTP_ERRORS,
    FetchError,
    RetryableError,
    Sleep,
    retryable_status,
    retrying,
)

log = logging.getLogger(__name__)

PUBLICATIONS_URL = "https://www.faa.gov/air_traffic/publications/"
# The only origin an edition may be fetched from: a listing that resolves
# anywhere else (another host, a protocol-relative link, plain http) is not
# authoritative FAA content and fails discovery (plan §5.2, §5.4).
APPROVED_SCHEME = "https"
APPROVED_HOSTS = frozenset({"www.faa.gov", "faa.gov"})

PUBLICATION_NAMES = {
    "aim": "Aeronautical Information Manual",
    "pcg": "Pilot/Controller Glossary",
}

_EFFECTIVE_RE = re.compile(r"\(effective\s+(\d{1,2})/(\d{1,2})/(\d{4})\)", re.IGNORECASE)
_FORMAT_RE = re.compile(r"\((HTML|PDF)\)", re.IGNORECASE)
_CHANGE_RE = re.compile(r"\bChanges?\b([\s\d,]+(?:and\s+\d+)?)", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class PublicationListing:
    """One publication's current HTML edition as listed by the FAA."""

    publication: str
    label: str
    effective_date: str
    change: int
    html_url: str


def origin_defect(url: object) -> str | None:
    """Why ``url`` is not on the approved FAA origin (None when it is)."""
    parts = urlsplit(str(url))
    if parts.scheme != APPROVED_SCHEME or parts.hostname not in APPROVED_HOSTS:
        return f"{str(url)!r} is outside the approved FAA origin"
    return None


def check_response_origin(response: httpx.Response, what: str) -> None:
    """Every hop of a (possibly redirected) FAA request must stay on the FAA origin.

    The client follows redirects; content that arrives via or from another
    host is not authoritative FAA material and fails closed (plan §5.2).
    """
    for hop in [*response.history, response]:
        defect = origin_defect(hop.url)
        if defect is not None:
            raise FetchError(f"{what}: redirected to {defect}")


def _flat(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _own_text(li: Node) -> str:
    """Text of a list item excluding nested lists (change-publication sub-lists)."""
    parts: list[str] = []

    def walk(node: Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag != "ul":
                walk(child)

    walk(li)
    return _flat("".join(parts))


def change_number(label: str) -> int:
    """``Basic with Change 1, 2 and 3`` → 3; ``Basic`` → 0; ``Change 2`` → 2."""
    match = _CHANGE_RE.search(label)
    if match is None:
        if re.search(r"\bBasic\b", label):
            return 0
        raise FetchError(f"cannot determine change number from edition label {label!r}")
    numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
    if not numbers:
        raise FetchError(f"cannot determine change number from edition label {label!r}")
    return max(numbers)


def _effective_date(text: str) -> str | None:
    match = _EFFECTIVE_RE.search(text)
    if match is None:
        return None
    month, day, year = (int(match.group(i)) for i in (1, 2, 3))
    try:
        from datetime import date

        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise FetchError(f"invalid effective date in publication listing: {text!r}") from exc


def parse_publications_page(
    html: str,
    base_url: str = PUBLICATIONS_URL,
    publications: tuple[str, ...] = tuple(PUBLICATION_NAMES),
) -> dict[str, PublicationListing]:
    """Current HTML editions keyed by publication (``aim``, ``pcg``).

    Only the requested ``publications`` are validated and returned, so a
    problem with one listing (missing, duplicated, reformatted) cannot make
    another publication unavailable.
    """
    unknown = sorted(set(publications) - set(PUBLICATION_NAMES))
    if unknown:
        raise ValueError(f"unknown publications {unknown}")
    try:
        root = parse_html(html)
    except HTMLStructureError as exc:
        raise FetchError(f"FAA publications page: malformed or truncated HTML ({exc})") from exc
    found: dict[str, list[PublicationListing]] = {name: [] for name in publications}
    for li in root.find_all(lambda n: n.tag == "li"):
        anchor = next((c for c in li.elements() if c.tag == "a"), None)
        if anchor is None or not anchor.get("href"):
            continue
        anchor_text = _flat(anchor.text())
        item_text = _own_text(li)
        if not _FORMAT_RE.search(item_text):
            continue
        formats = {m.group(1).upper() for m in _FORMAT_RE.finditer(item_text)}
        if "HTML" not in formats:
            continue
        for key in publications:
            name = PUBLICATION_NAMES[key]
            if not anchor_text.startswith(name):
                continue
            label = anchor_text[len(name) :].strip()
            # The AIM listing carries its abbreviation: "(AIM) Basic with ...".
            label = re.sub(r"^\([A-Z]+\)\s*", "", label)
            effective = _effective_date(item_text)
            if effective is None:
                raise FetchError(
                    f"{name} HTML listing has no parseable effective date: {item_text!r}"
                )
            html_url = urljoin(base_url, anchor.get("href") or "")
            if origin_defect(html_url) is not None:
                raise FetchError(
                    f"{name} HTML listing resolves outside the approved FAA origin: {html_url!r}"
                )
            found[key].append(
                PublicationListing(
                    publication=key,
                    label=label,
                    effective_date=effective,
                    change=change_number(label),
                    html_url=html_url,
                )
            )
    listings: dict[str, PublicationListing] = {}
    for key, entries in found.items():
        if len(entries) != 1:
            raise FetchError(
                f"expected exactly one HTML listing for {PUBLICATION_NAMES[key]} on the FAA "
                f"publications page, found {len(entries)}; upstream page layout may have changed"
            )
        listings[key] = entries[0]
    return listings


def fetch_publications_page(client: httpx.Client, *, sleep: Sleep | None = None) -> str:
    if sleep is None:
        sleep = time.sleep

    def attempt() -> str:
        try:
            response = client.get(PUBLICATIONS_URL)
        except TRANSIENT_HTTP_ERRORS as exc:
            raise RetryableError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP client error fetching {PUBLICATIONS_URL}: {exc}") from exc
        if response.status_code in RETRYABLE_STATUS:
            raise retryable_status(response)
        if response.status_code != 200:
            raise FetchError(f"FAA publications page returned HTTP {response.status_code}")
        check_response_origin(response, "FAA publications page")
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            raise FetchError(
                f"expected HTML from {PUBLICATIONS_URL}, got content-type {content_type!r}"
            )
        return response.text

    return retrying(attempt, what="FAA publications page", sleep=sleep)


def discover_publications(
    client: httpx.Client,
    *,
    sleep: Sleep | None = None,
    publications: tuple[str, ...] = tuple(PUBLICATION_NAMES),
) -> dict[str, PublicationListing]:
    """Fetch and parse the FAA publications page for ``publications``."""
    html = fetch_publications_page(client, sleep=sleep)
    return parse_publications_page(html, base_url=PUBLICATIONS_URL, publications=publications)
