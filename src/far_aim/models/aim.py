"""Canonical AIM model helpers: stable identifiers and hashing (plan §7.2, §7.4).

Identifiers derive from the FAA's own citation scheme — chapter, section,
and ``chapter-section-paragraph`` numbers — never from headings or page
filenames:

- publication ``aim`` (the index page: title, description, edition summary)
- chapter    ``aim-chapter-4``
- section    ``aim-4-1``
- paragraph  ``aim-4-1-9``
- appendix   ``aim-appendix-3``

``canonical_hash`` reuses the CFR rule: provenance (``source`` blocks and
nested hashes) is stripped before hashing, so a re-fetch that changes only
URLs or timestamps hashes identically (plan §14.4).
"""

from __future__ import annotations

import re

from far_aim.models.cfr import canonical_hash, strip_provenance

__all__ = [
    "DOCUMENT_TYPE_APPENDIX",
    "DOCUMENT_TYPE_CHAPTER",
    "DOCUMENT_TYPE_PARAGRAPH",
    "DOCUMENT_TYPE_PUBLICATION",
    "DOCUMENT_TYPE_SECTION",
    "PARAGRAPH_RE",
    "PUBLICATION_ID",
    "appendix_id",
    "canonical_hash",
    "chapter_id",
    "paragraph_id",
    "section_id",
    "strip_provenance",
]

DOCUMENT_TYPE_PUBLICATION = "aim_publication"
DOCUMENT_TYPE_CHAPTER = "aim_chapter"
DOCUMENT_TYPE_SECTION = "aim_section"
DOCUMENT_TYPE_PARAGRAPH = "aim_paragraph"
DOCUMENT_TYPE_APPENDIX = "aim_appendix"

PARAGRAPH_RE = re.compile(r"^(?P<chapter>\d{1,2})-(?P<section>\d{1,2})-(?P<number>\d{1,3})$")


PUBLICATION_ID = "aim"


def chapter_id(chapter: int) -> str:
    return f"aim-chapter-{chapter}"


def section_id(chapter: int, section: int) -> str:
    return f"aim-{chapter}-{section}"


def paragraph_id(paragraph: str) -> str:
    """``4-1-9`` → ``aim-4-1-9`` (the citation must be a valid paragraph number)."""
    if PARAGRAPH_RE.match(paragraph) is None:
        raise ValueError(f"not an AIM paragraph number: {paragraph!r}")
    return f"aim-{paragraph}"


def appendix_id(appendix: int) -> str:
    return f"aim-appendix-{appendix}"
