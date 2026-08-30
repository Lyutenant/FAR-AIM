"""Vault path, filename, and ordering rules (plan §9 naming policy).

Generated notes are named by stable citation only — never by mutable
headings — so curated links to them survive upstream heading edits.
Every rule here is pure string derivation from canonical identifiers.
"""

from __future__ import annotations

import re

# Filename stems must stay portable and unambiguous across filesystems:
# printable ASCII, no leading/trailing dots or spaces, wikilink-safe.
STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-]*$")
# Vault assets keep the FAA's own figure filenames (underscores allowed); they
# share the wikilink namespace with note stems, so they are collision-checked
# alongside them.
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

_DIGIT_RUN_RE = re.compile(r"[0-9]+")
_DIGIT_RUN_SPLIT_RE = re.compile(r"([0-9]+)")
_APPENDIX_SUFFIX_RE = re.compile(r"^appendix-(?P<letter>[A-Z]+)$")
_SFAR_SUFFIX_RE = re.compile(r"^sfar-(?P<num>[0-9]+(?:-[0-9]+)?)$")
_APPENDIX_RANGE_SUFFIX_RE = re.compile(r"^appendixes-(?P<first>[A-Z]+)-(?P<last>[A-Z]+)$")


def part_folder_name(part: str) -> str:
    """``91`` → ``Part 091``; ``50-59`` → ``Part 050-059``; ``374a`` → ``Part 374a``.

    Digit runs are padded to three places so folders list in citation order;
    the padding is display-only — filenames inside never pad.
    """
    return "Part " + _DIGIT_RUN_RE.sub(lambda m: m.group().zfill(3), part)


def part_index_stem(part: str) -> str:
    return f"Part {part}"


def section_stem(section: str) -> str:
    """The ``section`` field verbatim: ``91.155``, ``91.27-91.99``, ``1-1``."""
    return section


def appendix_suffix(part: str, appendix_id: str) -> str:
    """The stable id suffix after ``cfr-14-part-{part}-``."""
    prefix = f"-part-{part}-"
    head, sep, suffix = appendix_id.partition(prefix)
    if not sep or not suffix:
        raise ValueError(f"appendix id {appendix_id!r} does not match part {part!r}")
    return suffix


def appendix_label(part: str, appendix_id: str) -> str:
    """Human citation label from the stable id: ``Appendix A``, ``SFAR 50-2``.

    Fallback-slug ids (``Table-A-to-Part-117``) are already self-describing
    and become the label verbatim.
    """
    suffix = appendix_suffix(part, appendix_id)
    if match := _APPENDIX_SUFFIX_RE.match(suffix):
        return f"Appendix {match.group('letter')}"
    if match := _SFAR_SUFFIX_RE.match(suffix):
        return f"SFAR {match.group('num')}"
    if match := _APPENDIX_RANGE_SUFFIX_RE.match(suffix):
        return f"Appendixes {match.group('first')}-{match.group('last')}"
    return suffix


def appendix_stem(part: str, appendix_id: str) -> str:
    """``Part 91 Appendix A``, ``Part 91 SFAR 50-2``; fallback slugs verbatim."""
    suffix = appendix_suffix(part, appendix_id)
    label = appendix_label(part, appendix_id)
    if label == suffix:
        return suffix
    return f"Part {part} {label}"


def part_sort_key(part: str) -> tuple[int, str]:
    """Citation order for parts: leading number first, remainder breaks ties."""
    match = _DIGIT_RUN_RE.match(part)
    leading = int(match.group()) if match else 0
    return (leading, part[match.end() :] if match else part)


def natural_key(text: str) -> tuple[object, ...]:
    """Digit-run-aware sort key: ``91.20`` orders before ``91.155``."""
    parts = _DIGIT_RUN_SPLIT_RE.split(text)
    key: list[object] = []
    for i, piece in enumerate(parts):
        if i % 2:
            key.append((1, int(piece)))
        elif piece:
            key.append((0, piece))
    return tuple(key)


# ---------------------------------------------------------------------------
# AIM (plan §9: ``4-1-9.md`` for paragraphs; container notes carry an ``AIM``
# prefix so they can never collide with FAR part-local section stems such as
# part 241's ``4-1``)
# ---------------------------------------------------------------------------


def aim_chapter_folder(chapter: int) -> str:
    """``4`` → ``Chapter 04`` (zero-padded so folders list in order)."""
    return f"Chapter {chapter:02d}"


def aim_chapter_stem(chapter: int) -> str:
    return f"AIM Chapter {chapter}"


def aim_section_stem(chapter: int, section: int) -> str:
    return f"AIM {chapter}-{section}"


def aim_paragraph_stem(paragraph: str) -> str:
    """The paragraph number verbatim: ``4-1-9``."""
    return paragraph


def aim_appendix_stem(appendix: int) -> str:
    return f"AIM Appendix {appendix}"


# ---------------------------------------------------------------------------
# PCG (plan §9: the term itself is the citation; the note is named by the
# term, deterministically sanitized to a portable, wikilink-safe filename)
# ---------------------------------------------------------------------------

# Term stems additionally allow characters glossary terms actually contain
# and that are safe in filenames and inside ``[[wikilinks]]``: parentheses,
# comma, apostrophe. Brackets, slashes, colons and unicode hyphens are NOT
# safe (wikilink syntax, path separators, Windows) and are sanitized away.
PCG_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'()\-]*$")

_PCG_DASHES = str.maketrans({"‐": "-", "‑": "-", "–": "-", "—": "-", "/": "-"})
_PCG_DROP_RE = re.compile(r"[^A-Za-z0-9 .,'()\-]+")
_WS_RUN_RE = re.compile(r"\s+")
# Stems a term may never claim: fixed generated notes, and the citation
# shapes FAR/AIM notes are named by (section numbers, AIM paragraph
# numbers, ``Part …``/``AIM …``/``Chapter …`` containers). A term matching
# one gets a ``(PCG)`` suffix so the namespaces can never collide (today
# only the term ``AIM`` needs it).
_RESERVED_STEMS = frozenset({"aim", "far", "pcg", "title 14", "source status"})
_RESERVED_SHAPE_RE = re.compile(
    r"^(?:\d[\d.\-]*|(?:part|aim|chapter|appendix|section|title)\b.*)$", re.IGNORECASE
)


def pcg_letter_folder(letter: str) -> str:
    """``a`` → ``A`` (the glossary section folder)."""
    return letter.upper()


def pcg_term_stem(term: str) -> str:
    """Deterministic filename stem for a glossary term.

    ``DECISION ALTITUDE/DECISION HEIGHT [ICAO Annex 6]`` →
    ``DECISION ALTITUDE-DECISION HEIGHT (ICAO Annex 6)``;
    ``CHART SUPPLEMENT U.S.`` → ``CHART SUPPLEMENT U.S`` (no trailing dot);
    ``AIM`` → ``AIM (PCG)`` (reserved by the AIM index note).
    The term's verbatim text remains the citation and the note title; the
    stem is only its portable filename form.
    """
    stem = term.translate(_PCG_DASHES).replace("[", "(").replace("]", ")")
    stem = _PCG_DROP_RE.sub("", stem)
    stem = _WS_RUN_RE.sub(" ", stem).strip().rstrip(" .")
    if not stem:
        raise ValueError(f"term {term!r} yields an empty filename stem")
    if stem.casefold() in _RESERVED_STEMS or _RESERVED_SHAPE_RE.match(stem):
        stem = f"{stem} (PCG)"
    return stem
