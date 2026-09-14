"""Canonical ACS model helpers: stable identifiers and hashing (plan §39.2).

The Airman Certification Standards code an element by its position —
``PA.I.A.K1`` is ACS ``PA`` (Private Pilot Airplane), Area of Operation
``I``, Task ``A``, Knowledge element 1 — so the code *is* the citation
(plan §9) and every identifier derives from it:

- publication ``acs-pa``                (the ACS document as a whole)
- area        ``acs-PA.I``
- task        ``acs-PA.I.A``
- appendix    ``acs-pa-appendix-1``

``canonical_hash`` reuses the CFR rule (provenance stripped before hashing,
plan §14.4), so a re-fetch that changes only URLs, timestamps or the
extractor version hashes identically.
"""

from __future__ import annotations

import re

from far_aim.models.cfr import canonical_hash, strip_provenance

__all__ = [
    "DOCUMENT_TYPE_APPENDIX",
    "DOCUMENT_TYPE_AREA",
    "DOCUMENT_TYPE_PUBLICATION",
    "DOCUMENT_TYPE_TASK",
    "ELEMENT_CODE_RE",
    "ELEMENT_KINDS",
    "appendix_id",
    "area_id",
    "canonical_hash",
    "int_to_roman",
    "publication_id",
    "roman_to_int",
    "strip_provenance",
    "task_id",
]

DOCUMENT_TYPE_PUBLICATION = "acs_publication"
DOCUMENT_TYPE_AREA = "acs_area"
DOCUMENT_TYPE_TASK = "acs_task"
DOCUMENT_TYPE_APPENDIX = "acs_appendix"

# ``PA.I.C.K2a``: ACS prefix, Area roman numeral, Task letter, element kind
# (Knowledge / Risk management / Skill), number, optional sub-element letter.
ELEMENT_CODE_RE = re.compile(
    r"^(?P<acs>[A-Z]{2,3})\.(?P<area>[IVX]+)\.(?P<task>[A-Z])\."
    r"(?P<kind>[KRS])(?P<number>\d+)(?P<sub>[a-z]?)$"
)
ELEMENT_KINDS = {"K": "knowledge", "R": "risk", "S": "skills"}

_ROMAN_VALUES = (("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1))


def roman_to_int(roman: str) -> int:
    """``VIII`` → 8; only the well-formed numerals the ACS uses (I–XX)."""
    total = 0
    rest = roman
    for symbol, value in _ROMAN_VALUES:
        while rest.startswith(symbol):
            total += value
            rest = rest[len(symbol) :]
    if rest or total == 0 or int_to_roman(total) != roman:
        raise ValueError(f"not a roman numeral: {roman!r}")
    return total


def int_to_roman(number: int) -> str:
    if not 0 < number < 40:
        raise ValueError(f"out of range for roman numerals: {number}")
    out = ""
    for symbol, value in _ROMAN_VALUES:
        while number >= value:
            out += symbol
            number -= value
    return out


def publication_id(acs_prefix: str) -> str:
    """``PA`` → ``acs-pa``."""
    return f"acs-{acs_prefix.lower()}"


def area_id(acs_prefix: str, roman: str) -> str:
    return f"acs-{acs_prefix}.{roman}"


def task_id(acs_prefix: str, roman: str, letter: str) -> str:
    return f"acs-{acs_prefix}.{roman}.{letter}"


def appendix_id(acs_prefix: str, number: int) -> str:
    return f"acs-{acs_prefix.lower()}-appendix-{number}"
