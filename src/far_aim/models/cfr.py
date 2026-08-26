"""Canonical CFR model helpers: stable identifiers and content hashing (plan §7).

Canonical objects are plain JSON-compatible dicts built by the parsers. This
module owns the two cross-cutting rules:

- **Stable IDs** (plan §7.4): derived from the citation (`14`, `91.155`),
  never from mutable headings or filenames.
- **`canonical_hash`** (plan §14.4): computed over the object with all
  provenance stripped (`source` blocks, nested `canonical_hash` fields), so a
  re-fetch that changes only markup or volatile metadata hashes identically.
"""

from __future__ import annotations

import hashlib
import json
import re

DOCUMENT_TYPE_PART = "cfr_part"
DOCUMENT_TYPE_SECTION = "cfr_section"
DOCUMENT_TYPE_APPENDIX = "cfr_appendix"

# Keys excluded from canonical hashing: provenance and the hash fields
# themselves. Everything else — including editorial notes and amendment
# links, which are genuine point-in-time upstream content — is hashed.
_VOLATILE_KEYS = frozenset({"source", "canonical_hash"})

_SFAR_RE = re.compile(r"^Special Federal Aviation Regulation No\.\s*(?P<num>[0-9]+(?:-[0-9]+)?)")
_APPENDIX_RE = re.compile(r"^Appendix (?P<letter>[A-Z]+) to Part [0-9]+")
_APPENDIX_RANGE_RE = re.compile(r"^Appendixes (?P<first>[A-Z]+)\s*-\s*(?P<last>[A-Z]+)$")


def section_id(title_number: int, part: str | None, section: str) -> str:
    """``cfr-14-91.155``; reserved ranges keep their range (``cfr-14-91.27-91.99``).

    Sections numbered locally within their part (part 241's ``Sec. 1-1``)
    are part-qualified — ``cfr-14-241-1-1`` — since local numbers are not
    unique across the title.
    """
    if part is not None and not (section == part or section.startswith(f"{part}.")):
        return f"cfr-{title_number}-{part}-{section}"
    return f"cfr-{title_number}-{section}"


def part_id(title_number: int, part: str) -> str:
    return f"cfr-{title_number}-part-{part}"


def appendix_id(
    title_number: int, part: str, n_attribute: str, heading: str | None = None
) -> str:
    """Stable ID for a DIV9 appendix from its ``N`` attribute.

    Recognized forms (all observed in Title 14):
    - ``Special Federal Aviation Regulation No. 50-2`` → ``…-sfar-50-2``
    - ``Appendix A to Part 91``                        → ``…-appendix-A``
    - ``Appendixes B - C`` (reserved range)            → ``…-appendixes-B-C``
    When the ``N`` attribute matches none (part 380 has malformed ones), the
    heading is tried; otherwise a deterministic slug of the attribute.
    """
    base = f"cfr-{title_number}-part-{part}"
    for candidate in (n_attribute, heading or ""):
        if match := _SFAR_RE.match(candidate):
            return f"{base}-sfar-{match.group('num')}"
        if match := _APPENDIX_RE.match(candidate):
            return f"{base}-appendix-{match.group('letter')}"
        if match := _APPENDIX_RANGE_RE.match(candidate):
            return f"{base}-appendixes-{match.group('first')}-{match.group('last')}"
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", n_attribute).strip("-")
    return f"{base}-{slug}"


def strip_provenance(obj: object) -> object:
    """Recursively drop provenance/hash fields for canonical hashing."""
    if isinstance(obj, dict):
        return {
            key: strip_provenance(value)
            for key, value in obj.items()
            if key not in _VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [strip_provenance(item) for item in obj]
    return obj


def canonical_hash(obj: dict) -> str:
    """Deterministic content hash of a canonical object, provenance excluded."""
    payload = json.dumps(
        strip_provenance(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
