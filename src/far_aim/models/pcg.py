"""Canonical PCG model helpers: stable identifiers and hashing (plan §7.3, §7.4).

Identifiers derive from the glossary term text itself — the term is the
citation (plan §9) — never from page filenames or upstream anchor ids
(which are demonstrably non-unique in the FAA HTML: both ``ACROBATIC
FLIGHT`` and ``ACROBATIC FLIGHT [ICAO]`` carry ``id="ACROBATIC_FLIGHT"``):

- publication ``pcg`` (the index page: title, purpose, edition summary)
- letter      ``pcg-letter-a``
- term        ``pcg-controlled-airspace``, ``pcg-acc-icao``

``canonical_hash`` reuses the CFR rule: provenance (``source`` blocks and
nested hashes) is stripped before hashing, so a re-fetch that changes only
URLs or timestamps hashes identically (plan §14.4).
"""

from __future__ import annotations

import re

from far_aim.models.cfr import canonical_hash, strip_provenance

__all__ = [
    "DOCUMENT_TYPE_LETTER",
    "DOCUMENT_TYPE_PUBLICATION",
    "DOCUMENT_TYPE_TERM",
    "PUBLICATION_ID",
    "canonical_hash",
    "letter_id",
    "strip_provenance",
    "term_id",
]

DOCUMENT_TYPE_PUBLICATION = "pcg_publication"
DOCUMENT_TYPE_LETTER = "pcg_letter"
DOCUMENT_TYPE_TERM = "pcg_term"

PUBLICATION_ID = "pcg"

_LETTER_RE = re.compile(r"^[a-z]$")
# Slug: every run of non-alphanumeric characters (ASCII or not — the FAA
# uses U+2010 hyphens, slashes, brackets, apostrophes) becomes one hyphen.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def letter_id(letter: str) -> str:
    """``a`` → ``pcg-letter-a`` (the glossary section letter, lowercase)."""
    if _LETTER_RE.match(letter) is None:
        raise ValueError(f"not a glossary section letter: {letter!r}")
    return f"pcg-letter-{letter}"


def term_id(term: str) -> str:
    """``CONTROLLED AIRSPACE`` → ``pcg-controlled-airspace``; ``ACC [ICAO]``
    → ``pcg-acc-icao``. The slug must be non-empty."""
    slug = _SLUG_RE.sub("-", term.lower()).strip("-")
    if not slug:
        raise ValueError(f"term {term!r} yields an empty id slug")
    return f"pcg-{slug}"
