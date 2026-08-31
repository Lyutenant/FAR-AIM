"""Explicit in-text CFR citation extraction (plan §12.1, Tier 1).

Recognizes deterministic citation shapes in official Title 14 and FAA
(AIM) text:

- ``§ 91.155`` / ``§§ 91.101 through 91.135`` (endpoints only, no expansion)
- ``14 CFR 121.317(c), 121.571(a)(1)(i), 129.29, and 135.127(a)``
- ``14 CFR section 91.171`` / ``14 CFR Sections 91.131, 91.215, and 91.225``
  / ``14 CFR § 91.225`` / ``14CFR §91.113`` (the AIM's house styles)

A citation preceded by another title's ``CFR`` (``49 CFR § 830.5``) is
rejected; a bare ``§`` inside Title 14 text always means Title 14.
Paragraph suffixes (``(a)(1)``) are stripped — links target whole sections.

Part references are extracted separately (``extract_part_citations``):
``14 CFR part 91``, ``14 CFR Parts 121, 125, and 135``, and bare ``part 107``
/ ``Part 91 operators`` — a bare ``part`` in FAA text means Title 14 unless
the text attributes that number to another title (``49 CFR part 1542``).
The FAR generator does not yet link parts; the AIM generator does.

Out of scope (left as plain official text): bare ``section 91.185`` without
a ``CFR``/``§`` anchor, ``appendix A to part 91``, SFAR references,
part-local section numbers, and citations to the U.S.C. or other CFR titles.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from far_aim.generate.naming import natural_key, part_sort_key

# Dotted global section numbers only: 91.155, 121.583a, 91.27-91.99.
_TOKEN_RE = re.compile(r"[0-9]{1,4}[a-z]?\.[0-9]+[a-z]?(?:-[0-9]{1,4}[a-z]?(?:\.[0-9]+[a-z]?)?)?")
# Section-sign anchor; the guard below rejects other-title contexts.
_SIGN_ANCHOR_RE = re.compile(r"§§?\s*")
# "14 CFR 121.317" / "14 CFR section 91.171" / "14 CFR § 91.225" anchors
# with the title number captured; the optional section word or sign is the
# AIM's house style (any capitalization, and "14CFR" without a space).
_CFR_ANCHOR_RE = re.compile(
    r"\b([0-9]{1,3})\s*C\.?F\.?R\.?\s*(?:(?:[Ss]ections?|SECTIONS?)\s+|§§?\s*)?(?=[0-9])"
)
# Preceding-context guard for a bare §: "... 49 CFR § 830.5".
_OTHER_TITLE_RE = re.compile(r"([0-9]{1,3})\s+C\.?F\.?R\.?\s*$")
# Paragraph suffixes after a token: (a)(1)(i).
_SUFFIX_RE = re.compile(r"(?:\([0-9a-zA-Z]+\))*")
# List continuation between tokens, optionally re-introducing a § or the
# AIM's section word ("14 CFR section 71.33, sections 91.167 through 91.193").
_CONT_RE = re.compile(
    r"(?:\s*(?:,|;|\bthrough\b|\bto\b|\band\b|\bor\b)\s*)+"
    r"(?:§§?\s*|(?:[Ss]ections?|SECTIONS?)\s+)?"
)

# Part references. Title 14 part numbers are plain integers (letter and
# range ids like "374a" or "50-59" are never cited as such); a trailing
# uppercase letter is the FAA's subpart shorthand ("Parts 91K, 121") and is
# dropped. A digit or dotted continuation means the token is a section, and
# a following "CFR" means it is a title number ("part 830 and 14 CFR ...").
_PART_TOKEN_RE = re.compile(
    r"[0-9]{1,4}(?:[A-Z](?![A-Za-z]))?(?![0-9]|\.[0-9]|\s*C\.?F\.?R)"
)
_PART_CFR_ANCHOR_RE = re.compile(r"\b([0-9]{1,3})\s*C\.?F\.?R\.?,?\s*[Pp]arts?\s+(?=[0-9])")
_PART_BARE_ANCHOR_RE = re.compile(r"\b[Pp]arts?\s+(?=[0-9])")
_PART_CONT_RE = re.compile(r"(?:\s*(?:,|;|\bthrough\b|\band\b|\bor\b)\s*)+(?:[Pp]arts?\s+)?")


def _consume_list(text: str, pos: int, out: list[str]) -> int:
    """Consume a citation token list starting at ``pos``; return the end."""
    while True:
        token = _TOKEN_RE.match(text, pos)
        if token is None:
            return pos
        out.append(token.group())
        pos = _SUFFIX_RE.match(text, token.end()).end()
        cont = _CONT_RE.match(text, pos)
        if cont is None or not _TOKEN_RE.match(text, cont.end()):
            return pos
        pos = cont.end()


def _scan(text: str) -> tuple[list[str], set[str]]:
    """(Title 14 tokens in order, tokens claimed by another title's CFR).

    A token that appears anywhere in ``text`` under another title —
    ``49 CFR 21.7``, or a ``§`` immediately preceded by ``49 CFR`` — is
    collected as *banned*: prose often introduces an other-title section
    with a bare ``§`` first and names the title only later (``§ 21.7 of
    the Regulations … (49 CFR 21.7)``), so the same section number seen
    with a bare ``§`` in that text cannot be assumed to mean Title 14.
    """
    found: list[str] = []
    banned: set[str] = set()
    pos = 0
    while pos < len(text):
        sign = _SIGN_ANCHOR_RE.search(text, pos)
        cfr = _CFR_ANCHOR_RE.search(text, pos)
        anchor = min(
            (m for m in (sign, cfr) if m is not None),
            key=lambda m: m.start(),
            default=None,
        )
        if anchor is None:
            break
        other_title = False
        if anchor is cfr and anchor.group(1) != "14":
            other_title = True
        if anchor is sign:
            context = text[max(0, anchor.start() - 12) : anchor.start()]
            other = _OTHER_TITLE_RE.search(context)
            other_title = other is not None and other.group(1) != "14"
        out: list[str] = []
        end = _consume_list(text, anchor.end(), out)
        if other_title:
            banned.update(out)
        else:
            found.extend(out)
        pos = max(end, anchor.end())
    return found, banned


def extract_citations(text: str) -> list[str]:
    """Recognized Title 14 section tokens in ``text``, in order.

    Tokens the same text also attributes to another title are excluded
    (see ``_scan``); callers linking across a whole document should apply
    ``extract_other_title_citations`` over all of its text as well.
    """
    found, banned = _scan(text)
    return [token for token in found if token not in banned]


def extract_other_title_citations(text: str) -> set[str]:
    """Section tokens ``text`` attributes to a CFR title other than 14."""
    return _scan(text)[1]


def collect_text(blocks: Iterable[dict]) -> Iterator[str]:
    """Every text-bearing string of a content tree, in document order."""
    for block in blocks:
        kind = block.get("type")
        if kind == "paragraph":
            if block.get("subject"):
                yield block["subject"]
            if block.get("text"):
                yield block["text"]
            yield from collect_text(block.get("children") or [])
        elif kind == "definition":
            yield block["term"]
            yield block["text"]
            yield from collect_text(block.get("children") or [])
        elif kind in ("text", "heading"):
            if block.get("text"):
                yield block["text"]
        elif kind == "example":
            if block.get("heading"):
                yield block["heading"]
            yield block["text"]
        elif kind == "table":
            if block.get("caption"):
                yield block["caption"]
            for rows in (block["header_rows"], block["rows"], block["foot_rows"]):
                for row in rows:
                    for cell in row:
                        if cell["text"]:
                            yield cell["text"]
        elif kind == "note":
            if block.get("heading"):
                yield block["heading"]
            yield from collect_text(block.get("blocks") or [])
        elif kind in ("extract", "footnote"):
            yield from collect_text(block.get("blocks") or [])
        elif kind == "math":
            if block.get("text"):
                yield block["text"]
        # image: no text content


def resolve(
    tokens: Iterable[str], known_sections: set[str], *, exclude: str | None = None
) -> list[str]:
    """In-corpus targets only, deduplicated, in natural citation order."""
    kept = {token for token in tokens if token in known_sections and token != exclude}
    return sorted(kept, key=natural_key)


def _consume_part_list(text: str, pos: int, out: list[str]) -> int:
    while True:
        token = _PART_TOKEN_RE.match(text, pos)
        if token is None:
            return pos
        out.append(token.group().rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        pos = token.end()
        cont = _PART_CONT_RE.match(text, pos)
        if cont is None or not _PART_TOKEN_RE.match(text, cont.end()):
            return pos
        pos = cont.end()


def _scan_parts(text: str) -> tuple[list[str], set[str]]:
    """(Title 14 part numbers in order, parts claimed by another title).

    Mirrors ``_scan``: a ``49 CFR part 1542`` anywhere in the text bans that
    part number for the whole text, so a later bare ``part 1542`` is never
    read as Title 14.
    """
    found: list[str] = []
    banned: set[str] = set()
    pos = 0
    while pos < len(text):
        cfr = _PART_CFR_ANCHOR_RE.search(text, pos)
        bare = _PART_BARE_ANCHOR_RE.search(text, pos)
        anchor = min(
            (m for m in (cfr, bare) if m is not None),
            key=lambda m: m.start(),
            default=None,
        )
        if anchor is None:
            break
        if anchor is cfr:
            other_title = anchor.group(1) != "14"
        else:
            context = text[max(0, anchor.start() - 12) : anchor.start()]
            other = _OTHER_TITLE_RE.search(context)
            other_title = other is not None and other.group(1) != "14"
        out: list[str] = []
        end = _consume_part_list(text, anchor.end(), out)
        if other_title:
            banned.update(out)
        else:
            found.extend(out)
        pos = max(end, anchor.end())
    return found, banned


def extract_part_citations(text: str) -> list[str]:
    """Recognized Title 14 part numbers in ``text``, in order.

    Parts the same text attributes to another title are excluded; callers
    linking a whole document should also apply ``extract_other_title_parts``
    over all of its text.
    """
    found, banned = _scan_parts(text)
    return [part for part in found if part not in banned]


def extract_other_title_parts(text: str) -> set[str]:
    """Part numbers ``text`` attributes to a CFR title other than 14."""
    return _scan_parts(text)[1]


def resolve_parts(tokens: Iterable[str], known_parts: set[str]) -> list[str]:
    """In-corpus part targets only, deduplicated, in part order."""
    kept = {token for token in tokens if token in known_parts}
    return sorted(kept, key=part_sort_key)
