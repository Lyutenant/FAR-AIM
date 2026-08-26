"""eCFR Title 14 XML → canonical CFR JSON (plan §7, Phase 2).

Input is the archived point-in-time full-title XML (``ECFR`` root). The
element hierarchy in Title 14 is::

    DIV1 TITLE → DIV3 CHAPTER → DIV4 SUBCHAP → DIV5 PART
        → DIV6 SUBPART → DIV7 SUBJGRP → DIV8 SECTION
    DIV9 APPENDIX (under DIV5: both SFARs and lettered appendices)

with intermediate levels freely absent (sections may sit directly under a
part or subpart; Title 14 has no Subtitle level — plan §5.1).

Hard rules implemented here:

- **Fail loudly** (plan §32.2, §32.13): any element tag this parser does not
  explicitly handle raises :class:`ParseError`; nothing is silently dropped.
- **Lossless capture**: after building each part document, the multiset of
  whitespace/punctuation-delimited words in the source subtree must equal
  the multiset of words stored in the built document. A mismatch raises
  :class:`ParseError`. (A multiset, not a sequence, because e.g. a section's
  trailing CITA is stored in a field rather than positionally; ordering
  within each container is preserved by construction.)
- **Exact wording** (plan §32.3): text is captured verbatim; the only
  normalization is whitespace collapsing (including NBSP). Inline styling
  (italics, super/subscripts) is flattened to its character content; GPO
  ``<AC/>`` accent elements are decoded to Unicode combining marks (W̄, ẋ)
  so mathematical notation survives; any *unknown* tag inside a text run
  fails loudly rather than being flattened, since its meaning may live in
  attributes. The raw XML archive remains the styled record.
- **Determinism**: no ambient state; the same bytes produce the same output.

Nested paragraph labels
-----------------------

CFR paragraphs nest ``(a) → (1) → (i) → (A)`` with two italic levels below
that. The XML gives a *flat* run of ``<P>`` elements whose labels must be
re-nested. Tokens like ``(i)`` are ambiguous — roman ``(i)`` under ``(h)(2)``
versus alphabetic ``(i)`` after ``(h)`` — and are resolved deterministically:

1. a token may *continue* an open level (successor of that level's last
   label) or *start* a child level at its first value (``a``/``1``/``i``…) —
   the allowed child kinds per level are the ``_CHILD_KINDS`` graph, since
   Title 14's parts do not all nest identically;
2. a token repeating an open level's current value *re-enters* it when more
   labels follow in the same paragraph (150.21's ``(f)(1)`` … ``(f)(2)``);
3. a label-less paragraph opening with an italic defined term is a
   *definition* and opens its own child list whose numbering restarts per
   term (1.1, 16.3, 61.1, 121.7 …);
4. if no exact reading fits, amendment gaps are tolerated: a forward *jump*
   within an open level, or a child list starting mid-sequence at any open
   depth (91.107(a)(3)(iii)(B)(3) starts at italic ``(ii)``; 61.157 buries
   a ``(1)`` run-in mid-text so its roman list is followed by a bare
   ``(2)``);
5. if several readings are possible, the one under which the **next** label
   in the section remains parseable *without* gap tolerance wins (one-token
   lookahead);
6. if still ambiguous, continuing the shallowest open level wins;
7. if no reading is possible, parsing fails loudly with the section cited.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from far_aim.models import cfr as model

TITLE_NUMBER = 14


class ParseError(ValueError):
    """The source XML does not match the structure this parser understands."""


# ---------------------------------------------------------------------------
# Text flattening
# ---------------------------------------------------------------------------

# Private-use sentinels marking italic runs (<I>, <E T="03">) during label
# and subject detection. Always stripped from stored text.
_IT_OPEN = "\ue000"
_IT_CLOSE = "\ue001"
# Positional placeholder left in the flattened text where an inline block
# (img/MATH/FTNT) was collected, so callers can split the surrounding text
# and keep the source order text \u2192 block \u2192 text (never stored).
_INLINE_MARK = "\ue002"

_WS_RE = re.compile("[\\s\u00a0]+")

# Elements whose payload is not character data; flattening them would drop
# content silently, so their appearance must fail loudly instead. (FTREF is
# an *empty* footnote-reference marker and flattens to nothing by design.)
_NON_TEXT_TAGS = frozenset({"img", "MATH", "FTNT"})

# Inline markup that continues the surrounding text run; any other child
# element is a block whose content must not concatenate onto its neighbour
# ("<HED>Authority:</HED><PSPACE>49 U.S.C…" is two words, not one).
_INLINE_TAGS = frozenset(
    {"I", "E", "B", "SU", "sup", "sub", "strong", "em", "br", "FR", "FTREF", "AC"}
)

# GPO accent elements: an empty <AC T="…"/> places an accent over the
# preceding character. Decoded to Unicode combining marks so the notation
# survives in canonical text: T="8" is an overbar (mean quantities — W̄az
# "mean wind speed at azimuth" in part 420, Ā in §25.341's gust-load
# formula), T="b" a dot above (ẋ, ẏ, ż velocity components in §420.5).
# Unknown codes fail loudly rather than dropping notation (plan §32.2).
_ACCENT_MARKS = {"8": "\u0304", "b": "\u0307"}


def _attach_accent(parts: list[str], mark: str) -> None:
    """Append a combining mark directly to the last non-space character.

    The source often puts a newline between the base character and its
    ``<AC/>`` ("x\\n<AC T=\"b\"/>"); the mark must combine with the "x",
    not float after a collapsed space.
    """
    while parts and not parts[-1].strip():
        parts.pop()
    if not parts:
        raise ParseError("AC accent element with no preceding character")
    parts[-1] = parts[-1].rstrip()
    parts.append(mark)


def _is_italic(elem: ET.Element) -> bool:
    return elem.tag == "I" or (elem.tag == "E" and elem.get("T") == "03")


def _flatten(
    elem: ET.Element,
    *,
    marked: bool = False,
    inline: list[dict] | None = None,
    lenient: bool = False,
) -> str:
    """Concatenated character content of ``elem``, whitespace-normalized.

    With ``marked=True`` italic runs are wrapped in sentinel characters so
    the paragraph lexer can distinguish italic labels and run-in subject
    headings. ``<br/>`` becomes a space (its source carries no whitespace).

    Some paragraphs embed non-text elements (an equation ``img``, ``MATH``,
    a ``FTNT`` footnote) in their flow. With ``inline`` given, those are
    parsed into blocks appended to that list (the caller attaches them after
    the paragraph); without it their appearance fails loudly. ``lenient``
    instead keeps their character content in the flow — used only by the
    lossless-capture check, whose word stream must cover the whole subtree.
    """
    parts: list[str] = []

    def visit(e: ET.Element) -> None:
        if e.tag in _NON_TEXT_TAGS and not lenient:
            if inline is None:
                raise ParseError(f"non-text element {e.tag!r} where text was expected")
            if e.tag == "img":
                inline.append(_image_block(e))
            elif e.tag == "MATH":
                inline.append(_parse_math(e, "inline"))
            else:
                inline.append(_parse_footnote(e, "inline"))
            parts.append(_INLINE_MARK)  # position marker for the caller
            return  # tail is appended by the parent loop
        if e.tag == "AC":
            code = e.get("T") or ""
            accent = _ACCENT_MARKS.get(code)
            if accent is None:
                raise ParseError(f"unrecognized AC accent code {code!r}")
            _attach_accent(parts, accent)
            return  # empty element; its tail is appended by the parent loop
        italic = marked and _is_italic(e)
        if italic:
            parts.append(_IT_OPEN)
        if e.tag == "br":
            parts.append(" ")
        if e.text:
            parts.append(e.text)
        for child in e:
            is_inline = child.tag in _INLINE_TAGS
            # In storage mode only known markup may appear inside a text
            # run: an unknown tag could carry attribute-borne meaning that
            # generic descent would silently strip (as AC once did). The
            # lenient verification pass walks whole structural subtrees and
            # stays permissive.
            if not is_inline and not lenient and child.tag not in _NON_TEXT_TAGS:
                raise ParseError(
                    f"unexpected element {child.tag!r} inside {e.tag!r} text"
                )
            if not is_inline:
                parts.append(" ")
            visit(child)
            if not is_inline:
                parts.append(" ")
            if child.tail:
                parts.append(child.tail)
        if italic:
            parts.append(_IT_CLOSE)

    visit(elem)
    text = _WS_RE.sub(" ", "".join(parts))
    if marked:
        # Move whitespace outside the sentinels and drop empty runs so the
        # lexer sees clean `(x) Subject. rest` shapes.
        text = text.replace(_IT_OPEN + " ", " " + _IT_OPEN)
        text = text.replace(" " + _IT_CLOSE, _IT_CLOSE + " ")
        text = text.replace(_IT_OPEN + _IT_CLOSE, "")
        text = _WS_RE.sub(" ", text)
    return text.strip()


def _strip_marks(text: str) -> str:
    return text.replace(_IT_OPEN, "").replace(_IT_CLOSE, "").strip()


# ---------------------------------------------------------------------------
# Paragraph label lexing
# ---------------------------------------------------------------------------

# CFR paragraph level kinds (plan Phase 2: nested paragraph labels). The
# canonical order is (a)→(1)→(i)→(A) with italic levels below, but Title 14
# is not uniform: certification parts nest romans directly under alphas
# (27.143), emissions parts nest (A) directly under (1) (34.21), and the
# DOT economic regulations use italic *letters* — (a)(2)(ii)(𝑎)(𝑏) in
# 372.24, (b)(1)(i)(A)(𝟏)(𝟐) in 302.203. So each kind maps to the child
# kinds observed below it, in preference order.
_CHILD_KINDS: dict[str | None, tuple[str, ...]] = {
    None: ("alpha", "num"),  # top level of a section
    "alpha": ("num", "roman"),
    "num": ("roman", "ALPHA"),
    "roman": ("ALPHA", "alpha_italic"),
    "ALPHA": ("num_italic", "alpha_italic"),
    "alpha_italic": ("num_italic", "roman_italic"),
    "num_italic": ("roman_italic",),
    "roman_italic": (),
    # Definitions (see _lex_paragraph): each defined term opens its own
    # sub-list, restarting numbering (1.1, 16.3, 61.1, 121.7, …).
    "definition": ("num", "roman", "alpha"),
}
_FIRST_VALUE = {
    "alpha": "a",
    "num": "1",
    "roman": "i",
    "ALPHA": "A",
    "alpha_italic": "a",
    "num_italic": "1",
    "roman_italic": "i",
}

_ROMAN_CHARS = frozenset("ivxlcdm")


def _roman_sequence(limit: int = 200) -> dict[str, int]:
    values = (
        (100, "c"), (90, "xc"), (50, "l"), (40, "xl"), (10, "x"),
        (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
    )
    table: dict[str, int] = {}
    for n in range(1, limit + 1):
        remaining, glyphs = n, ""
        for value, glyph in values:
            while remaining >= value:
                glyphs += glyph
                remaining -= value
        table[glyphs] = n
    return table


_ROMAN_VALUE = _roman_sequence()


def _alpha_value(token: str) -> int | None:
    """``a``..``z`` → 1..26, then doubled ``aa``..``zz`` → 27..52 (CFR style)."""
    if len(token) == 1 and token.isalpha():
        return ord(token) - ord("a") + 1
    if len(token) == 2 and token[0] == token[1] and token[0].isalpha():
        return 26 + ord(token[0]) - ord("a") + 1
    return None


@dataclass(frozen=True)
class _LabelToken:
    designator: str  # e.g. "a", "1", "ii"
    italic: bool

    @property
    def display(self) -> str:
        return f"({self.designator})"


def _possible_kinds(token: _LabelToken) -> tuple[str, ...]:
    d = token.designator
    if d.isdigit():
        return ("num_italic",) if token.italic else ("num",)
    if token.italic:
        if not d.islower():
            return ()
        kinds = []
        if set(d) <= _ROMAN_CHARS and d in _ROMAN_VALUE:
            kinds.append("roman_italic")
        if _alpha_value(d) is not None:
            kinds.append("alpha_italic")
        return tuple(kinds)
    if d.islower():
        kinds = []
        if _alpha_value(d) is not None:
            kinds.append("alpha")
        if set(d) <= _ROMAN_CHARS and d in _ROMAN_VALUE:
            kinds.append("roman")
        return tuple(kinds)
    if d.isupper():
        return ("ALPHA",) if _alpha_value(d.lower()) is not None else ()
    return ()


def _label_value(kind: str, designator: str) -> int | None:
    """Ordinal position of ``designator`` within its level kind's sequence."""
    if kind == "definition":
        return None  # definition levels have no label sequence
    if kind in ("num", "num_italic"):
        return int(designator) if designator.isdigit() else None
    if kind in ("roman", "roman_italic"):
        return _ROMAN_VALUE.get(designator)
    return _alpha_value(designator.lower())


def _is_successor(kind: str, prev: str, cur: str) -> bool:
    a, b = _label_value(kind, prev), _label_value(kind, cur)
    return a is not None and b is not None and b == a + 1


def _is_forward_jump(kind: str, prev: str, cur: str) -> bool:
    """Non-adjacent continuation: a later value in the same sequence."""
    a, b = _label_value(kind, prev), _label_value(kind, cur)
    return a is not None and b is not None and b > a + 1


_TOKEN_PATTERN = r"[0-9]{1,3}|[A-Za-z]{1,7}"
_LABEL_RE = re.compile(
    rf"\((?:{_IT_OPEN}({_TOKEN_PATTERN}){_IT_CLOSE}|({_TOKEN_PATTERN}))\)"
)
_ITALIC_RUN_RE = re.compile(rf"{_IT_OPEN}([^{_IT_OPEN}{_IT_CLOSE}]+?){_IT_CLOSE}")
_DASHES = "—–"


@dataclass
class _Segment:
    """One labelled run inside a ``<P>``: labels, optional subject, text.

    ``term`` marks a *definition* paragraph — a label-less ``<P>`` opening
    with an italic defined term (``<I>Air carrier</I> means …``); each such
    paragraph opens its own sub-list whose numbering restarts.
    """

    labels: list[_LabelToken]
    subject: str | None
    text: str
    term: str | None = None


def _match_label(text: str, pos: int) -> tuple[_LabelToken, int] | None:
    match = _LABEL_RE.match(text, pos)
    if not match:
        return None
    italic = match.group(1) is not None
    token = _LabelToken(match.group(1) or match.group(2), italic)
    if not _possible_kinds(token):
        return None
    return token, match.end()


def _match_subject(marked: str, pos: int) -> tuple[str, int] | None:
    """A run-in subject heading after a label.

    Two source shapes: ``(h) <I>Subject.</I> text`` (subject ends with a
    period) and ``(b) <I>Subject</I>—(1) …`` (subject joined to a deeper
    run-in label by an em-dash, common in DOT economic regulations).
    """
    match = _ITALIC_RUN_RE.match(marked, pos)
    if match is None:
        return None
    subject = match.group(1).strip()
    end = match.end()
    while end < len(marked) and marked[end] == " ":
        end += 1
    if end < len(marked) and marked[end] in _DASHES:
        dash_end = end + 1
        while dash_end < len(marked) and marked[dash_end] == " ":
            dash_end += 1
        if _match_label(marked, dash_end) is not None:
            return subject, dash_end
    if subject.endswith("."):
        return subject, end
    if _match_label(marked, end) is not None:
        return subject, end
    return None


def _lex_paragraph(marked: str) -> list[_Segment]:
    """Split a flattened+marked ``<P>`` into labelled segments.

    ``(h) SUBJ. (1) text`` and ``(2) (i) text`` are run-ins: the later label
    starts a new segment. Labels are only ever recognized at the start of a
    segment, never inside running text. A ``<P>`` with no leading label
    yields a single label-less segment — a *definition* segment when it
    opens with an italic defined term.
    """
    segments: list[_Segment] = []
    pos = 0
    while True:
        labels: list[_LabelToken] = []
        while (hit := _match_label(marked, pos)) is not None:
            token, end = hit
            labels.append(token)
            pos = end
            if end < len(marked) and marked[end] == " ":
                pos = end + 1
                break
        if not labels:
            break
        subject = None
        if (hit := _match_subject(marked, pos)) is not None:
            subject, pos = hit
        # A label immediately after the labels/subject is a run-in child.
        if _match_label(marked, pos) is not None:
            segments.append(_Segment(labels, subject, ""))
            continue
        segments.append(_Segment(labels, subject, _strip_marks(marked[pos:])))
        return segments
    if segments:
        return segments
    if marked.startswith(_IT_OPEN) and (match := _ITALIC_RUN_RE.match(marked)):
        # Defined terms may continue past the italic run without a space —
        # §1.2's V-speeds are an italic "V" plus a subscript ("V1", "V2min").
        end = match.end()
        while end < len(marked) and marked[end] != " ":
            end += 1
        term = _strip_marks(match.group(1) + marked[match.end() : end]).strip()
        return [_Segment([], None, _strip_marks(marked[end:]), term=term)]
    return [_Segment([], None, _strip_marks(marked))]


# ---------------------------------------------------------------------------
# Paragraph tree building
# ---------------------------------------------------------------------------


def _paragraph_node(token: _LabelToken, subject: str | None, text: str) -> dict:
    return {
        "type": "paragraph",
        "label": token.display,
        "designator": token.designator,
        "subject": subject,
        "text": text,
        "children": [],
    }


@dataclass
class _Level:
    kind: str
    last: str
    node: dict


class _ParaTreeBuilder:
    """Re-nests a flat run of labelled paragraphs into a tree.

    ``lookahead`` is the full ordered list of label tokens in the container,
    used to resolve ambiguous tokens (see module docstring).
    ``definitions_context`` marks sections whose heading names them a
    definitions/terms section — see :meth:`add_segment`.
    """

    def __init__(
        self,
        context: str,
        lookahead: list[_LabelToken],
        *,
        definitions_context: bool = False,
    ) -> None:
        self.context = context
        self.lookahead = lookahead
        self.definitions_context = definitions_context
        self.consumed = 0
        self.stack: list[_Level] = []
        self.blocks: list[dict] = []

    def reset_stack(self) -> None:
        self.stack.clear()

    def attach(self, block: dict) -> None:
        """Attach a non-paragraph block under the deepest open paragraph."""
        if self.stack:
            self.stack[-1].node["children"].append(block)
        else:
            self.blocks.append(block)

    def add_segment(self, segment: _Segment) -> None:
        if segment.term is not None:
            # An italic-led unlabelled paragraph is a defined term in a
            # definitions section (§1.1's "Restricted area.", §234.2's
            # "Reportable flight."), where terms may end any way and may
            # carry child lists. Elsewhere, only a term NOT ending with a
            # period reads as an inline definition ("<i>Term</i> means …");
            # a period-terminated italic run is a run-in subject heading
            # (§29.755's "Water-based and amphibian rotorcraft.") and must
            # not open a definition level that would swallow the labels
            # that follow.
            if self.definitions_context or not segment.term.endswith("."):
                self._add_definition(segment.term, segment.text)
            else:
                self.attach(
                    {
                        "type": "paragraph",
                        "label": None,
                        "designator": None,
                        "subject": segment.term,
                        "text": segment.text,
                        "children": [],
                    }
                )
            return
        if not segment.labels:
            self.attach({"type": "text", "style": "plain", "text": segment.text})
            return
        prefix = ""
        for index, token in enumerate(segment.labels):
            last = index == len(segment.labels) - 1
            reentered = self._place(
                token,
                segment.subject if last else None,
                segment.text if last else "",
                allow_reenter=not last,
                label_prefix=prefix,
            )
            prefix = prefix + token.display if reentered else ""

    def _add_definition(self, term: str, text: str) -> None:
        """A defined term opens its own restartable sub-list (1.1, 61.1, …).

        A new definition replaces any previous definition level at the same
        position, so each term's ``(1)``/``(i)`` list nests under it.
        """
        node = {"type": "definition", "term": term, "text": text, "children": []}
        for depth, level in enumerate(self.stack):
            if level.kind == "definition":
                del self.stack[depth:]
                break
        self.attach(node)
        self.stack.append(_Level("definition", "", node))

    def _place(
        self,
        token: _LabelToken,
        subject: str | None,
        text: str,
        *,
        allow_reenter: bool = False,
        label_prefix: str = "",
    ) -> bool:
        """Place one label; returns True if it re-entered an existing level.

        Re-entry covers sources that repeat the parent label per paragraph —
        150.21 has ``(f)(1)`` followed by ``(f)(2)``: the second ``(f)``
        creates no node, and the child that follows keeps the full source
        label (``(f)(2)``) for lossless capture.
        """
        kinds = _possible_kinds(token)
        shape = [(lvl.kind, lvl.last) for lvl in self.stack]
        candidates = self._candidates(shape, token, kinds, relaxed=False)
        if not candidates and allow_reenter:
            for depth in range(len(self.stack) - 1, -1, -1):
                level = self.stack[depth]
                if level.kind in kinds and level.last == token.designator:
                    del self.stack[depth + 1 :]
                    self.consumed += 1
                    return True
        if not candidates:
            candidates = self._candidates(shape, token, kinds, relaxed=True)
        self.consumed += 1
        if len(candidates) > 1:
            candidates = self._resolve_by_lookahead(candidates, token)
        if not candidates:
            raise ParseError(
                f"{self.context}: cannot place paragraph label {token.display} "
                f"(open levels: {[(lvl.kind, lvl.last) for lvl in self.stack]})"
            )
        role, depth, kind = candidates[0]
        node = _paragraph_node(token, subject, text)
        if label_prefix:
            node["label"] = label_prefix + node["label"]
        if role == "sibling":
            del self.stack[depth + 1 :]
            level = self.stack[depth]
            level.last = token.designator
            parent = self.stack[depth - 1].node["children"] if depth else self.blocks
            parent.append(node)
            level.node = node
        else:
            del self.stack[depth:]
            parent = self.stack[-1].node["children"] if self.stack else self.blocks
            parent.append(node)
            self.stack.append(_Level(kind, token.designator, node))
        return False

    @staticmethod
    def _candidates(
        stack: list[tuple[str, str]],
        token: _LabelToken,
        kinds: tuple[str, ...],
        *,
        relaxed: bool,
    ) -> list[tuple[str, int, str]]:
        """Valid placements, shallowest continuation first.

        With ``relaxed=True``, amendment gaps are tolerated (docstring rule 2):
        a forward jump within an open level, or a child level starting at a
        value other than its first.
        """
        found: list[tuple[str, int, str]] = []
        for depth, (kind, last) in enumerate(stack):
            if kind not in kinds:
                continue
            if _is_successor(kind, last, token.designator) or (
                relaxed and _is_forward_jump(kind, last, token.designator)
            ):
                found.append(("sibling", depth, kind))
        # A child candidate at depth d keeps stack[:d] and nests under
        # stack[d-1]. Strictly, only the deepest level may gain a first
        # child; relaxed also admits a list starting mid-sequence at ANY
        # depth — the source pattern "(a) … must: (1) Be …" buries the (1)
        # run-in mid-text, so its (i)…(v) list is followed by a bare (2)
        # that belongs under (a) (61.157, 298.2, 399.79).
        depths = range(len(stack), -1, -1) if relaxed else (len(stack),)
        for depth in depths:
            parent_kind = stack[depth - 1][0] if depth else None
            for child_kind in _CHILD_KINDS.get(parent_kind, ()):
                if child_kind in kinds and (
                    token.designator == _FIRST_VALUE[child_kind]
                    or (
                        relaxed
                        and _label_value(child_kind, token.designator) is not None
                    )
                ):
                    found.append(("child", depth, child_kind))
        return found

    def _resolve_by_lookahead(
        self, candidates: list[tuple[str, int, str]], token: _LabelToken
    ) -> list[tuple[str, int, str]]:
        if self.consumed >= len(self.lookahead):
            return candidates  # no next token; rule 3 (shallowest) applies
        nxt = self.lookahead[self.consumed]
        nxt_kinds = _possible_kinds(nxt)
        shapes = []
        for role, depth, kind in candidates:
            shape = [(lvl.kind, lvl.last) for lvl in self.stack]
            if role == "sibling":
                shape = shape[: depth + 1]
                shape[depth] = (kind, token.designator)
            else:
                shape = [*shape[:depth], (kind, token.designator)]
            shapes.append(((role, depth, kind), shape))
        # A branch that keeps the next label parseable *without* gap
        # tolerance beats one that needs it; gap tolerance in the lookahead
        # would make almost any branch survive and defeat the comparison.
        for relaxed in (False, True):
            surviving = [
                cand
                for cand, shape in shapes
                if self._candidates(shape, nxt, nxt_kinds, relaxed=relaxed)
            ]
            if surviving:
                return surviving
        return candidates


# ---------------------------------------------------------------------------
# Block-level parsing (shared by sections and appendices)
# ---------------------------------------------------------------------------

_FLUSH_STYLES = {
    "FP": "flush",
    "FP-1": "flush-1",
    "FP-2": "flush-2",
    "FP1-2": "flush-1-2",
    "FP-DASH": "flush-dash",
}
_HEADING_LEVELS = {"HD1": 1, "HD2": 2, "HD3": 3, "HD5": 5}


def _text_block(elem: ET.Element, style: str) -> dict:
    return {"type": "text", "style": style, "text": _flatten(elem)}


def _table_cell(elem: ET.Element, context: str) -> dict:
    cell: dict = {"text": _flatten(elem)}
    if elem.tag == "TH":
        cell["header"] = True
    for attr in ("colspan", "rowspan"):
        value = elem.get(attr)
        if value is not None and value != "1":
            if not value.isdigit() or int(value) < 1:
                raise ParseError(f"{context}: invalid table cell {attr} {value!r}")
            cell[attr] = int(value)
    return cell


def _caption_text(elem: ET.Element, context: str) -> str:
    """Caption text; captions may wrap their footnote lines in ``<P>``s."""
    for nested in elem.iter():
        if nested is elem:
            continue
        if nested.tag not in _INLINE_TAGS and nested.tag != "P":
            raise ParseError(
                f"{context}: unexpected {nested.tag!r} inside {elem.tag} caption"
            )
    return _flatten(elem, lenient=True)


def _row_cells(row: ET.Element, context: str) -> list[dict]:
    cells = []
    for cell in row:
        if cell.tag not in ("TD", "TH"):
            raise ParseError(f"{context}: unexpected {cell.tag!r} inside TR")
        cells.append(_table_cell(cell, context))
    return cells


def _table_rows(group: ET.Element, context: str) -> list[list[dict]]:
    rows = []
    for row in group:
        if row.tag != "TR":
            raise ParseError(f"{context}: unexpected {row.tag!r} inside table row group")
        rows.append(_row_cells(row, context))
    return rows


def _parse_table(table: ET.Element, context: str) -> dict:
    header_rows: list[list[dict]] = []
    rows: list[list[dict]] = []
    foot_rows: list[list[dict]] = []
    caption = None
    for child in table:
        if child.tag in ("CAPTION", "TCAP"):
            caption = _caption_text(child, context)
        elif child.tag == "THEAD":
            header_rows.extend(_table_rows(child, context))
        elif child.tag == "TBODY":
            rows.extend(_table_rows(child, context))
        elif child.tag == "TFOOT":
            foot_rows.extend(_table_rows(child, context))
        elif child.tag == "TR":
            rows.append(_row_cells(child, context))
        else:
            raise ParseError(f"{context}: unexpected {child.tag!r} inside TABLE")
    return {
        "type": "table",
        "caption": caption,
        "header_rows": header_rows,
        "rows": rows,
        "foot_rows": foot_rows,
    }


def _parse_table_div(div: ET.Element, context: str) -> list[dict]:
    """A ``DIV`` wrapper holds one or more tables (possibly nested in DIVs)."""
    tables = []
    for child in div:
        if child.tag == "DIV":
            tables.extend(_parse_table_div(child, context))
        elif child.tag == "TABLE":
            tables.append(_parse_table(child, context))
        else:
            raise ParseError(f"{context}: unexpected {child.tag!r} inside table DIV")
    return tables


def _image_block(elem: ET.Element) -> dict:
    src = elem.get("src")
    if not src:
        raise ParseError("img element without src attribute")
    return {"type": "image", "src": src}


def _parse_math(math: ET.Element, context: str) -> dict:
    """eCFR MATH elements wrap rendered-equation images (plus layout attrs)."""
    images = []
    for child in math:
        if child.tag == "img":
            images.append(_image_block(child)["src"])
        else:
            raise ParseError(f"{context}: unexpected {child.tag!r} inside MATH")
    text = " ".join((math.text or "").split())
    return {"type": "math", "images": images, "text": text}


def _parse_footnote(ftnt: ET.Element, context: str) -> dict:
    blocks = []
    for child in ftnt:
        if child.tag == "P":
            blocks.append(_text_block(child, "plain"))
        elif child.tag in _FLUSH_STYLES:
            blocks.append(_text_block(child, _FLUSH_STYLES[child.tag]))
        else:
            raise ParseError(f"{context}: unexpected {child.tag!r} inside FTNT")
    return {"type": "footnote", "blocks": blocks}


def _parse_extract(extract: ET.Element, context: str) -> dict:
    blocks = []
    for child in extract:
        if child.tag == "P":
            blocks.append(_text_block(child, "plain"))
        elif child.tag == "P2":
            blocks.append(_text_block(child, "paragraph-2"))
        elif child.tag in _FLUSH_STYLES:
            blocks.append(_text_block(child, _FLUSH_STYLES[child.tag]))
        elif child.tag in _HEADING_LEVELS:
            blocks.append(
                {"type": "heading", "level": _HEADING_LEVELS[child.tag], "text": _flatten(child)}
            )
        elif child.tag == "img":
            blocks.append(_image_block(child))
        elif child.tag == "MATH":
            blocks.append(_parse_math(child, context))
        elif child.tag == "FTNT":
            blocks.append(_parse_footnote(child, context))
        elif child.tag == "DIV":
            blocks.extend(_parse_table_div(child, context))
        elif child.tag == "TABLE":
            blocks.append(_parse_table(child, context))
        elif child.tag == "CITA":
            blocks.append(_text_block(child, "citation"))
        elif child.tag == "NOTE":
            blocks.append(_parse_note(child, context))
        else:
            raise ParseError(f"{context}: unexpected {child.tag!r} inside EXTRACT")
    return {"type": "extract", "blocks": blocks}


def _parse_note(note: ET.Element, context: str) -> dict:
    heading = None
    blocks = []
    for child in note:
        if child.tag == "HED":
            heading = _flatten(child)
        elif child.tag == "P":
            blocks.append(_text_block(child, "plain"))
        elif child.tag in _FLUSH_STYLES:
            blocks.append(_text_block(child, _FLUSH_STYLES[child.tag]))
        else:
            raise ParseError(f"{context}: unexpected {child.tag!r} inside NOTE")
    return {"type": "note", "heading": heading, "blocks": blocks}


def _parse_hed_pspace(elem: ET.Element, context: str) -> dict:
    """``HED`` + body pairs (AUTH, SOURCE, EDNOTE, EFFDNOT, CROSSREF).

    Bodies are usually ``PSPACE`` runs, but multi-item editorial notes
    (part 21) and cross references use plain ``P`` elements.
    """
    heading = None
    texts = []
    for child in elem:
        if child.tag == "HED":
            heading = _flatten(child)
        elif child.tag in ("PSPACE", "P"):
            texts.append(_flatten(child))
        else:
            raise ParseError(f"{context}: unexpected {child.tag!r} inside {elem.tag}")
    return {"heading": heading, "text": " ".join(texts)}


# ---------------------------------------------------------------------------
# Section parsing (DIV8)
# ---------------------------------------------------------------------------

# Section heads open with a citation marker: "§ 91.155", "§§ 91.27-91.99",
# or — in the CAB-era economic regulations (part 241) — "Section 03" and
# "Sec. 1-1".
_SECTION_MARKER_RE = re.compile(r"^(?P<marker>§§|§|Sections?|Secs?\.)\s*")
_RESERVED_HEADINGS = frozenset({"[Reserved]", "[RESERVED]"})


@dataclass
class _Context:
    """Hierarchy position carried down the DIV tree (plan §7.1 fields)."""

    title_heading: str | None = None
    subtitle: str | None = None
    subtitle_heading: str | None = None
    chapter: str | None = None
    chapter_heading: str | None = None
    subchapter: str | None = None
    subchapter_heading: str | None = None
    part: str | None = None
    subpart: str | None = None
    subject_group: str | None = None

    def child(self, **overrides: str | None) -> _Context:
        return _Context(**{**self.__dict__, **overrides})


def _collect_section_label_tokens(div8: ET.Element) -> list[_LabelToken]:
    tokens = []
    for p in div8:
        if p.tag != "P":
            continue
        discard: list[dict] = []
        marked = _flatten(p, marked=True, inline=discard)
        # Mirror parse_section: labels are lexed only from the text before
        # the first inline block; later chunks are continuation text.
        for segment in _lex_paragraph(marked.split(_INLINE_MARK)[0]):
            tokens.extend(segment.labels)
    return tokens


def _parse_section_head(
    div8: ET.Element, context_name: str
) -> tuple[str, str, str, bool]:
    """Section HEAD → (marker, number, heading, reserved).

    The section number is not re-derived by pattern-matching the HEAD;
    instead the ``N`` attribute (whitespace-normalized — part 13 has
    ``N="13.21 -13.29"``) must appear verbatim, spaces ignored, right after
    the marker. Anything else is a HEAD/attribute mismatch and fails.
    """
    head = div8.find("HEAD")
    if head is None:
        raise ParseError(f"{context_name}: SECTION has no HEAD")
    text = _flatten(head)
    match = _SECTION_MARKER_RE.match(text)
    # A few reserved-range heads carry no marker at all ("13.83-13.87
    # [Reserved]").
    marker = match.group("marker") if match else ""
    number = re.sub(r"\s+", "", div8.get("N") or "")
    if not number:
        raise ParseError(f"{context_name}: SECTION has no N attribute")
    pos = match.end() if match else 0
    for char in number:
        while pos < len(text) and text[pos] == " ":
            pos += 1
        if pos >= len(text) or text[pos] != char:
            raise ParseError(
                f"{context_name}: HEAD {text!r} does not cite N attribute {number!r}"
            )
        pos += 1
    if pos < len(text) and text[pos] == ".":
        pos += 1  # "§ 91.155." style trailing period on the citation
    # The citation must end at a delimiter: a HEAD citing a longer number
    # (N="9.1" but "§ 9.10 …") is a mismatch, not a prefix match.
    if pos < len(text) and text[pos] != " ":
        raise ParseError(
            f"{context_name}: HEAD {text!r} does not cite N attribute {number!r}"
        )
    heading = text[pos:].strip()
    if not heading:
        raise ParseError(f"{context_name}: section HEAD {text!r} has no heading")
    reserved = heading in _RESERVED_HEADINGS
    # A markerless HEAD is legitimate only where the corpus actually omits
    # the citation marker: reserved-range heads ("13.83-13.87 [Reserved]")
    # and CAB-era locally numbered sections, recognizable — like in
    # parse_section — by the hyphenated prefix ("19-8.1 Purpose."). On an
    # ordinary section a missing marker means the citation markup was
    # lost, which must fail rather than silently enter the canonical
    # layer with an empty head_marker.
    local_numbering = "-" in number.split(".", 1)[0]
    if not marker and not (reserved or local_numbering):
        raise ParseError(
            f"{context_name}: section HEAD {text!r} has no citation marker"
        )
    return marker, number, heading, reserved


# Sections whose heading names them a definitions section ("General
# definitions.", "Definitions.", "What do the terms in this rule mean?",
# "Applicability and definitions.", "Meaning of terms.").
_DEFINITIONS_HEADING_RE = re.compile(r"\bdefinitions?\b|\bterms?\b|\bmeanings?\b", re.IGNORECASE)


def parse_section(div8: ET.Element, context: _Context, source: dict) -> dict:
    context_name = f"section {div8.get('N', '?')}"
    marker, number, heading, reserved = _parse_section_head(div8, context_name)
    # Sections numbered "91.155" must belong to their part. CAB-era parts
    # (241: "Section 03", "Sec. 1-1", "Sec. 19-8.1") number sections locally
    # instead — recognizable by the hyphenated or dotless prefix.
    prefix = number.split(".", 1)[0]
    if (
        context.part is not None
        and "." in number
        and "-" not in prefix
        and prefix != context.part
    ):
        raise ParseError(
            f"{context_name}: section number does not belong to part {context.part}"
        )

    builder = _ParaTreeBuilder(
        context_name,
        _collect_section_label_tokens(div8),
        definitions_context=_DEFINITIONS_HEADING_RE.search(heading) is not None,
    )
    citations: list[str] = []
    approvals: list[str] = []
    amendment_notes: list[str] = []
    editorial_notes: list[dict] = []
    section_authority = None

    for child in div8:
        tag = child.tag
        if tag == "HEAD":
            continue
        if tag == "P":
            inline: list[dict] = []
            marked = _flatten(child, marked=True, inline=inline)
            # Keep source order text → block → text: the leading chunk is
            # the paragraph proper; each inline block is followed by its
            # trailing text as a continuation under the same paragraph.
            chunks = marked.split(_INLINE_MARK)
            if chunks[0].strip() or not inline:
                for segment in _lex_paragraph(chunks[0].strip()):
                    builder.add_segment(segment)
            for block, chunk in zip(inline, chunks[1:], strict=True):
                builder.attach(block)
                trailing = _strip_marks(chunk)
                if trailing:
                    builder.attach({"type": "text", "style": "plain", "text": trailing})
        elif tag == "P2":
            builder.attach(_text_block(child, "paragraph-2"))
        elif tag in _FLUSH_STYLES:
            builder.attach(_text_block(child, _FLUSH_STYLES[tag]))
        elif tag == "DIV":
            for table in _parse_table_div(child, context_name):
                builder.attach(table)
        elif tag == "TABLE":
            builder.attach(_parse_table(child, context_name))
        elif tag == "EXTRACT":
            builder.attach(_parse_extract(child, context_name))
        elif tag == "NOTE":
            builder.attach(_parse_note(child, context_name))
        elif tag == "img":
            builder.attach(_image_block(child))
        elif tag == "MATH":
            builder.attach(_parse_math(child, context_name))
        elif tag == "FTNT":
            builder.attach(_parse_footnote(child, context_name))
        elif tag in _HEADING_LEVELS:
            builder.reset_stack()
            builder.attach(
                {"type": "heading", "level": _HEADING_LEVELS[tag], "text": _flatten(child)}
            )
        elif tag == "CITA":
            citations.append(_flatten(child))
        elif tag == "APPRO":
            approvals.append(_flatten(child))
        elif tag == "SECAUTH":
            section_authority = _flatten(child)
        elif tag == "XREF":
            amendment_notes.append(_flatten(child))
        elif tag == "EXAMPLE":
            example = _parse_hed_pspace(child, context_name)
            builder.attach({"type": "example", **example})
        elif tag == "TCAP":
            builder.attach(_text_block(child, "table-caption"))
        elif tag in ("EDNOTE", "EFFDNOT"):
            editorial_notes.append(_parse_hed_pspace(child, context_name))
        else:
            raise ParseError(f"{context_name}: unhandled element {tag!r} in SECTION")

    return {
        "id": model.section_id(TITLE_NUMBER, context.part, number),
        "document_type": model.DOCUMENT_TYPE_SECTION,
        "title_number": TITLE_NUMBER,
        "chapter": context.chapter,
        "subchapter": context.subchapter,
        "part": context.part,
        "subpart": context.subpart,
        "subject_group": context.subject_group,
        "section": number,
        "head_marker": marker,
        "heading": heading,
        "reserved": reserved,
        "content": builder.blocks,
        "citations": citations,
        "approvals": approvals,
        "section_authority": section_authority,
        "amendment_notes": amendment_notes,
        "editorial_notes": editorial_notes,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Appendix parsing (DIV9): SFARs and lettered appendices
# ---------------------------------------------------------------------------


def parse_appendix(div9: ET.Element, context: _Context, source: dict) -> dict:
    """Appendix content is kept as a flat, ordered block list.

    Appendix material (SFARs, equipment tables, RVSM procedures) does not
    follow the section paragraph-label grammar, so labels stay inline in the
    text rather than being re-nested (documented in docs/data-model.md).
    """
    n_attribute = div9.get("N")
    if not n_attribute:
        raise ParseError("APPENDIX without N attribute")
    context_name = f"appendix {n_attribute!r}"
    # Part 380's appendices carry two HEAD elements (a short duplicate and
    # the full title); the longest is the heading, the rest are preserved.
    heads = [_flatten(h) for h in div9.findall("HEAD")]
    if not heads:
        raise ParseError(f"{context_name}: no HEAD")
    heading = max(heads, key=len)
    alternate_headings = [h for h in heads if h != heading]
    blocks: list[dict] = []
    citations: list[str] = []
    editorial_notes: list[dict] = []
    section_authority = None
    for child in div9:
        tag = child.tag
        if tag == "HEAD":
            continue
        if tag == "P":
            inline: list[dict] = []
            text = _flatten(child, inline=inline)
            chunks = text.split(_INLINE_MARK)
            if chunks[0].strip() or not inline:
                blocks.append(
                    {"type": "text", "style": "paragraph", "text": chunks[0].strip()}
                )
            for block, chunk in zip(inline, chunks[1:], strict=True):
                blocks.append(block)
                if chunk.strip():
                    blocks.append(
                        {"type": "text", "style": "paragraph", "text": chunk.strip()}
                    )
        elif tag == "P2":
            blocks.append(_text_block(child, "paragraph-2"))
        elif tag in _FLUSH_STYLES:
            blocks.append(_text_block(child, _FLUSH_STYLES[tag]))
        elif tag in _HEADING_LEVELS:
            blocks.append(
                {"type": "heading", "level": _HEADING_LEVELS[tag], "text": _flatten(child)}
            )
        elif tag == "DIV":
            blocks.extend(_parse_table_div(child, context_name))
        elif tag == "TABLE":
            blocks.append(_parse_table(child, context_name))
        elif tag == "EXTRACT":
            blocks.append(_parse_extract(child, context_name))
        elif tag == "NOTE":
            blocks.append(_parse_note(child, context_name))
        elif tag == "img":
            blocks.append(_image_block(child))
        elif tag == "MATH":
            blocks.append(_parse_math(child, context_name))
        elif tag == "FTNT":
            blocks.append(_parse_footnote(child, context_name))
        elif tag == "CITA":
            citations.append(_flatten(child))
        elif tag == "SECAUTH":
            section_authority = _flatten(child)
        elif tag in ("EDNOTE", "EFFDNOT"):
            editorial_notes.append(_parse_hed_pspace(child, context_name))
        else:
            raise ParseError(f"{context_name}: unhandled element {tag!r} in APPENDIX")
    return {
        "id": model.appendix_id(TITLE_NUMBER, context.part or "?", n_attribute, heading),
        "document_type": model.DOCUMENT_TYPE_APPENDIX,
        "title_number": TITLE_NUMBER,
        "part": context.part,
        "subpart": context.subpart,
        "heading": heading,
        "alternate_headings": alternate_headings,
        "reserved": heading.endswith(("[Reserved]", "[RESERVED]")),
        "content": blocks,
        "citations": citations,
        "section_authority": section_authority,
        "editorial_notes": editorial_notes,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Part / subpart / subject-group parsing (DIV5 / DIV6 / DIV7)
# ---------------------------------------------------------------------------

# Subpart labels are letters ("Subpart B", reserved ranges "Subparts I-J"),
# but NASA parts use numbers: "Subpart 1—Introduction" (1240),
# "Subpart 1212.1—Basic Policy", "Subparts 1214.1-1214.3 [Reserved]".
_SUBPART_HEAD_RE = re.compile(
    r"^Subparts?\s+"
    r"(?P<label>[A-Z]+(?:-[A-Z]+)?|[0-9]+(?:\.[0-9]+)?(?:-[0-9]+(?:\.[0-9]+)?)?)"
    r"\s*(?:—\s*)?(?P<heading>.*)$"
)
_PART_HEAD_RE = re.compile(
    r"^PARTS?\s+(?P<number>[0-9]+[A-Za-z]?(?:-[0-9]+)?)\s*(?:—\s*)?(?P<heading>.*)$"
)


def _with_hash(obj: dict) -> dict:
    obj["canonical_hash"] = model.canonical_hash(obj)
    return obj


def _parse_subject_group(div7: ET.Element, context: _Context, source: dict) -> dict:
    head = div7.find("HEAD")
    if head is None:
        raise ParseError("SUBJGRP without HEAD")
    heading = _flatten(head)
    group_context = context.child(subject_group=heading)
    sections = []
    for child in div7:
        if child.tag == "HEAD":
            continue
        if child.tag == "DIV8" and child.get("TYPE") == "SECTION":
            sections.append(_with_hash(parse_section(child, group_context, source)))
        else:
            raise ParseError(f"subject group {heading!r}: unhandled element {child.tag!r}")
    return {"type": "subject_group", "heading": heading, "sections": sections}


def _parse_subpart(div6: ET.Element, context: _Context, source: dict) -> dict:
    n_attribute = div6.get("N")
    head = div6.find("HEAD")
    if head is None or not n_attribute:
        raise ParseError(f"SUBPART {n_attribute!r} missing HEAD or N")
    head_text = _flatten(head)
    match = _SUBPART_HEAD_RE.match(head_text)
    if match is None:
        raise ParseError(f"cannot parse subpart HEAD {head_text!r}")
    if match.group("label") != n_attribute:
        raise ParseError(
            f"subpart HEAD cites {match.group('label')!r} but N attribute is {n_attribute!r}"
        )
    heading = match.group("heading").strip()
    subpart_context = context.child(subpart=n_attribute)
    source_note = None
    authority = None
    editorial_notes: list[dict] = []
    children: list[dict] = []
    for child in div6:
        if child.tag == "HEAD":
            continue
        if child.tag == "SOURCE":
            source_note = _parse_hed_pspace(child, f"subpart {n_attribute}")
        elif child.tag == "AUTH":
            authority = _parse_hed_pspace(child, f"subpart {n_attribute}")
        elif child.tag in ("EDNOTE", "EFFDNOT"):
            editorial_notes.append(_parse_hed_pspace(child, f"subpart {n_attribute}"))
        elif child.tag in _HEADING_LEVELS:
            children.append(
                {
                    "type": "heading",
                    "level": _HEADING_LEVELS[child.tag],
                    "text": _flatten(child),
                }
            )
        elif child.tag == "DIV7" and child.get("TYPE") == "SUBJGRP":
            children.append(_parse_subject_group(child, subpart_context, source))
        elif child.tag == "DIV8" and child.get("TYPE") == "SECTION":
            children.append(_with_hash(parse_section(child, subpart_context, source)))
        elif child.tag == "DIV9" and child.get("TYPE") == "APPENDIX":
            children.append(_with_hash(parse_appendix(child, subpart_context, source)))
        else:
            raise ParseError(f"subpart {n_attribute}: unhandled element {child.tag!r}")
    return {
        "type": "subpart",
        "label": n_attribute,
        "heading": heading,
        "reserved": heading in _RESERVED_HEADINGS,
        "source_note": source_note,
        "authority": authority,
        "editorial_notes": editorial_notes,
        "children": children,
    }


def parse_part(div5: ET.Element, context: _Context, source: dict) -> dict:
    """Build the canonical document for one part, then verify lossless capture."""
    n_attribute = div5.get("N")
    head = div5.find("HEAD")
    if head is None or not n_attribute:
        raise ParseError(f"PART {n_attribute!r} missing HEAD or N")
    head_text = _flatten(head)
    match = _PART_HEAD_RE.match(head_text)
    if match is None:
        raise ParseError(f"cannot parse part HEAD {head_text!r}")
    if match.group("number") != n_attribute:
        raise ParseError(
            f"part HEAD cites {match.group('number')!r} but N attribute is {n_attribute!r}"
        )
    heading = match.group("heading").strip()
    part_context = context.child(part=n_attribute)

    authority = None
    source_note = None
    editorial_notes: list[dict] = []
    notes: list[dict] = []
    cross_references: list[dict] = []
    children: list[dict] = []
    for child in div5:
        tag = child.tag
        if tag == "HEAD":
            continue
        if tag == "AUTH":
            authority = _parse_hed_pspace(child, f"part {n_attribute} AUTH")
        elif tag == "SOURCE":
            source_note = _parse_hed_pspace(child, f"part {n_attribute} SOURCE")
        elif tag in ("EDNOTE", "EFFDNOT"):
            editorial_notes.append(_parse_hed_pspace(child, f"part {n_attribute}"))
        elif tag == "NOTE":
            notes.append(_parse_note(child, f"part {n_attribute}"))
        elif tag == "CROSSREF":
            cross_references.append(_parse_hed_pspace(child, f"part {n_attribute}"))
        elif tag == "DIV6" and child.get("TYPE") == "SUBPART":
            children.append(_parse_subpart(child, part_context, source))
        elif tag == "DIV7" and child.get("TYPE") == "SUBJGRP":
            children.append(_parse_subject_group(child, part_context, source))
        elif tag == "DIV8" and child.get("TYPE") == "SECTION":
            children.append(_with_hash(parse_section(child, part_context, source)))
        elif tag == "DIV9" and child.get("TYPE") == "APPENDIX":
            children.append(_with_hash(parse_appendix(child, part_context, source)))
        else:
            raise ParseError(f"part {n_attribute}: unhandled element {tag!r}")

    doc = {
        "id": model.part_id(TITLE_NUMBER, n_attribute),
        "document_type": model.DOCUMENT_TYPE_PART,
        "title_number": TITLE_NUMBER,
        "title_heading": part_context.title_heading,
        "subtitle": part_context.subtitle,
        "subtitle_heading": part_context.subtitle_heading,
        "chapter": part_context.chapter,
        "chapter_heading": part_context.chapter_heading,
        "subchapter": part_context.subchapter,
        "subchapter_heading": part_context.subchapter_heading,
        "part": n_attribute,
        "heading": heading,
        "reserved": heading in _RESERVED_HEADINGS,
        "authority": authority,
        "source_note": source_note,
        "editorial_notes": editorial_notes,
        "notes": notes,
        "cross_references": cross_references,
        "children": children,
        "source": source,
    }
    _verify_lossless(div5, doc)
    return _with_hash(doc)


# ---------------------------------------------------------------------------
# Lossless-capture verification (plan §32.2: nothing silently omitted)
# ---------------------------------------------------------------------------

# Keys excluded from the per-part lossless word comparison: identifiers,
# metadata, and hierarchy headings captured from *outside* the DIV5 subtree
# (title/chapter/subchapter) — those words are not in the part's source
# stream. They all still participate in canonical hashing.
_NON_CONTENT_KEYS = frozenset({
    "id", "document_type", "title_number", "title_heading", "subtitle",
    "subtitle_heading", "chapter", "chapter_heading", "subchapter",
    "subchapter_heading", "part", "subpart", "subject_group", "section",
    "head_marker", "type", "style", "designator", "level", "reserved",
    "header", "colspan", "rowspan", "canonical_hash", "source", "src",
    "images",
})

# Structural punctuation the parser legitimately reshapes — and ONLY that:
# label parentheses ("(a)(1)" becomes two label fields), the em-dash joining
# a head citation to its heading or a run-in subject to its child label,
# and the hyphen in whitespace-normalized range citations ("13.21 -13.29").
# The comparison is strict (whitespace tokens) first; only the residual
# mismatch may be explained by splitting on these glyphs, and every residue
# token carrying one must itself look structural — embed a valid paragraph
# label, or dash-join onto one of this part's own designators or a section
# citation. Prose punctuation ("fixed-wing", "(FAA)") anchors on neither,
# so a transformation reshaping it fails the check, exactly like dropped
# periods, commas, semicolons, colons, brackets and quotes (plan §32.3).
_PUNCT_SPLIT_RE = re.compile(r"[()—–-]+")
_STRUCTURAL_GLYPH_RE = re.compile(r"[()—–-]")
_DASH_SPLIT_RE = re.compile(r"[—–-]+")
_EMBEDDED_LABEL_RE = re.compile(rf"\(({_TOKEN_PATTERN})\)")
_SECTION_CITE_RE = re.compile(r"^[0-9]+[A-Za-z]?\.[0-9]+[a-z]?$")


def _doc_words(obj: object, out: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key not in _NON_CONTENT_KEYS:
                _doc_words(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _doc_words(item, out)
    elif isinstance(obj, str):
        out.extend(obj.split())


def _built_words(doc: dict) -> list[str]:
    """Words stored in the document, plus the head tokens parsed into fields."""
    part_marker = "PARTS" if "-" in doc["part"] else "PART"
    words = [part_marker, doc["part"], *doc["heading"].split()]
    for key in ("authority", "source_note", "editorial_notes", "notes",
                "cross_references"):
        _doc_words(doc.get(key), words)
    for child in doc["children"]:
        _built_child_words(child, words)
    return words


def _built_child_words(node: dict, words: list[str]) -> None:
    kind = node.get("type") or node.get("document_type")
    if kind == "subpart":
        marker = "Subparts" if "-" in node["label"] else "Subpart"
        words.extend([marker, node["label"], *node["heading"].split()])
        _doc_words(node.get("source_note"), words)
        _doc_words(node.get("authority"), words)
        _doc_words(node.get("editorial_notes"), words)
        for child in node["children"]:
            _built_child_words(child, words)
    elif kind == "subject_group":
        words.extend(node["heading"].split())
        for section in node["sections"]:
            _built_child_words(section, words)
    elif kind == "heading":
        words.extend(node["text"].split())
    elif kind == model.DOCUMENT_TYPE_SECTION:
        words.extend([node["head_marker"], node["section"], *node["heading"].split()])
        for key in ("content", "citations", "approvals", "section_authority",
                    "amendment_notes", "editorial_notes"):
            _doc_words(node[key], words)
    elif kind == model.DOCUMENT_TYPE_APPENDIX:
        words.extend(node["heading"].split())
        for key in ("alternate_headings", "content", "citations", "section_authority",
                    "editorial_notes"):
            _doc_words(node[key], words)
    else:
        raise ParseError(f"unknown child node type {kind!r} during verification")


def _word_counts(words: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    return counts


def _piece_counts(tokens: dict[str, int]) -> dict[str, int]:
    """Token counts re-keyed by their structural-glyph-split pieces."""
    counts: dict[str, int] = {}
    for token, count in tokens.items():
        for piece in _PUNCT_SPLIT_RE.split(token):
            if piece:
                counts[piece] = counts.get(piece, 0) + count
    return counts


def _multiset_subtract(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {word: count - b.get(word, 0) for word, count in a.items()
            if count > b.get(word, 0)}


def _structural_designators(doc: dict) -> frozenset[str]:
    """Designators of this part that may anchor a dash join in a head: the
    part number and its subpart labels, plus the endpoints of range labels
    ("118-120", "F-G"), which flatten next to the joining em-dash."""
    labels = [doc["part"]]
    labels.extend(
        child["label"] for child in doc["children"] if child.get("type") == "subpart"
    )
    designators = set()
    for label in labels:
        designators.add(label)
        designators.update(p for p in _DASH_SPLIT_RE.split(label) if p)
    return frozenset(designators)


def _is_structural_join(token: str, designators: frozenset[str]) -> bool:
    """Whether a token the strict comparison could not match is one of the
    parser's known reshapes rather than altered prose: it embeds a valid
    paragraph label ("(a)(1)", "Subject—(1)"), or its dash-split pieces
    include one of the part's own designators ("91—GENERAL", "A—General")
    or a section citation ("-13.29", "13.21-13.29")."""
    for match in _EMBEDDED_LABEL_RE.finditer(token):
        candidate = match.group(1)
        if _possible_kinds(_LabelToken(candidate, italic=False)) or _possible_kinds(
            _LabelToken(candidate, italic=True)
        ):
            return True
    return any(
        piece in designators or _SECTION_CITE_RE.match(piece)
        for piece in _DASH_SPLIT_RE.split(token)
        if piece
    )


def _verify_lossless(div5: ET.Element, doc: dict) -> None:
    source = _word_counts(_flatten(div5, lenient=True).split())
    built = _word_counts(_built_words(doc))
    if source == built:
        return
    missing = _multiset_subtract(source, built)
    extra = _multiset_subtract(built, source)
    # The residual mismatch is acceptable only as structural reshaping: the
    # word pieces must still balance exactly once structural glyphs are
    # split away, and every residue token carrying such a glyph must itself
    # be a recognizable structural join — reshaped prose punctuation is not.
    if _piece_counts(missing) == _piece_counts(extra):
        designators = _structural_designators(doc)
        joins_missing = {t for t in missing if _is_structural_join(t, designators)}
        joins_extra = {t for t in extra if _is_structural_join(t, designators)}

        def explained(token: str) -> bool:
            # A residue token is a known reshape, or a fragment split off
            # one intact — subpart G of part 17 detaches the hyphenated
            # heading "Pre-Disputes" from the join "G—Pre-Disputes". Its
            # own punctuation survives verbatim inside the counterpart, so
            # this cannot excuse reshaped prose.
            if token in joins_missing or token in joins_extra:
                return True
            counterparts = joins_extra if token in missing else joins_missing
            return any(token in join for join in counterparts)

        suspect = [
            token
            for token in sorted({*missing, *extra})
            if _STRUCTURAL_GLYPH_RE.search(token) and not explained(token)
        ]
        if not suspect:
            return
        raise ParseError(
            f"part {doc['part']}: lossless-capture check failed; punctuation "
            f"reshaped outside structural fields: {suspect[:8]!r}"
        )
    raise ParseError(
        f"part {doc['part']}: lossless-capture check failed; "
        f"missing from document: {sorted(missing)[:8]!r}; "
        f"unexpected in document: {sorted(extra)[:8]!r}"
    )


# ---------------------------------------------------------------------------
# Title-level entry point
# ---------------------------------------------------------------------------


def _required_heading(division: ET.Element, what: str) -> str:
    """A hierarchy division's HEAD is authoritative wording; sitting outside
    every ``DIV5`` subtree it escapes the lossless check, so its absence —
    or a surplus HEAD the hierarchy walk would silently skip — must fail
    here rather than silently publish incomplete data."""
    heads = division.findall("HEAD")
    if len(heads) > 1:
        raise ParseError(
            f"{what} {division.get('N')!r} has {len(heads)} HEAD elements; "
            "only one heading is modeled"
        )
    heading = _flatten(heads[0]) if heads else ""
    if not heading:
        raise ParseError(f"{what} {division.get('N')!r} has no heading")
    return heading


def _required_designator(division: ET.Element, what: str) -> str:
    """A hierarchy division's ``N`` attribute is its citation designator.
    Attribute text sits outside the lossless word comparison, so a missing
    one would silently publish every part below it with a null hierarchy
    context; it must fail here instead."""
    designator = (division.get("N") or "").strip()
    if not designator:
        raise ParseError(f"{what} division has no N attribute")
    return designator


def iter_parts(root: ET.Element) -> list[tuple[_Context, ET.Element]]:
    """All (context, DIV5) pairs in document order; unknown structure fails.

    Everything must live inside exactly one ``DIV1 TYPE="TITLE" N="14"``
    container: IDs and ``title_number`` fields are fixed to Title 14, so a
    document for another title — or malformed XML with chapters directly
    under the root — must be rejected, not republished under the wrong
    citation.
    """
    if root.tag != "ECFR":
        raise ParseError(f"expected ECFR root, got {root.tag!r}")
    found: list[tuple[_Context, ET.Element]] = []
    title_divisions = 0

    def visit(elem: ET.Element, context: _Context, *, in_title: bool) -> None:
        nonlocal title_divisions
        for child in elem:
            div_type = child.get("TYPE")
            if child.tag == "HEAD":
                # Only the heading _required_heading captured (and counted)
                # for this container may be skipped; a HEAD at the ECFR
                # root has no home in the model and would vanish silently.
                if not in_title:
                    raise ParseError("unexpected HEAD element at the ECFR document root")
                continue
            if child.tag == "DIV1" and div_type == "TITLE":
                if child.get("N") != str(TITLE_NUMBER):
                    raise ParseError(
                        f"expected Title {TITLE_NUMBER}, found DIV1 N={child.get('N')!r}"
                    )
                title_divisions += 1
                # The title heading ("Title 14—Aeronautics and Space") is
                # authoritative wording; carry it into every part document
                # so it is stored and canonical-hashed, not dropped with
                # the generic HEAD skip.
                heading = _required_heading(child, "title division")
                visit(child, context.child(title_heading=heading), in_title=True)
                continue
            if not in_title:
                raise ParseError(
                    f"structural element {child.tag!r} TYPE={div_type!r} outside a "
                    f"Title {TITLE_NUMBER} division"
                )
            if child.tag == "DIV2":
                # Subtitle level, absent in Title 14 today (plan §5.1) but
                # tolerated; its designator and heading are authoritative
                # wording and must ride into the canonical model, not be
                # dropped with the generic HEAD skip.
                designator = _required_designator(child, "subtitle")
                heading = _required_heading(child, "subtitle")
                visit(
                    child,
                    context.child(subtitle=designator, subtitle_heading=heading),
                    in_title=True,
                )
            elif child.tag == "DIV3" and div_type == "CHAPTER":
                designator = _required_designator(child, "chapter")
                heading = _required_heading(child, "chapter")
                visit(
                    child,
                    context.child(chapter=designator, chapter_heading=heading),
                    in_title=True,
                )
            elif child.tag == "DIV4" and div_type == "SUBCHAP":
                designator = _required_designator(child, "subchapter")
                heading = _required_heading(child, "subchapter")
                visit(
                    child,
                    context.child(subchapter=designator, subchapter_heading=heading),
                    in_title=True,
                )
            elif child.tag == "DIV5" and div_type == "PART":
                found.append((context, child))
            else:
                raise ParseError(
                    f"unhandled structural element {child.tag!r} TYPE={div_type!r}"
                )

    visit(root, _Context(), in_title=False)
    if title_divisions != 1:
        raise ParseError(
            f"expected exactly one Title {TITLE_NUMBER} division, "
            f"found {title_divisions}"
        )
    return found


def build_part_docs(
    root: ET.Element, source: dict, only: set[str] | None = None
) -> dict[str, dict]:
    """Canonical documents for the requested parts, keyed by part number.

    ``only=None`` builds every part; unknown part numbers in ``only`` fail.
    """
    available = iter_parts(root)
    numbers = [div5.get("N") for _, div5 in available]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        # Keying by part number would silently replace one DIV5's content
        # with the other's — a lossless-capture violation (plan §32.2).
        raise ParseError(f"duplicate part number(s) in title: {', '.join(duplicates)}")
    if only is not None:
        missing = sorted(only - set(numbers))
        if missing:
            raise ParseError(f"part(s) not present in this title: {', '.join(missing)}")
    docs: dict[str, dict] = {}
    for context, div5 in available:
        number = div5.get("N")
        if only is not None and number not in only:
            continue
        docs[number] = parse_part(div5, context, source)
    _verify_unique_ids(docs)
    return docs


def _verify_unique_ids(docs: dict[str, dict]) -> None:
    """Every stable ID across the built documents must be unique.

    A repeated section citation, or two appendices normalizing to the same
    slug, would make citation-keyed notes overwrite or ambiguously
    reference content downstream (plan §7.4) — fail loudly instead.
    """
    counts: dict[str, int] = {}

    def collect(node: object) -> None:
        if isinstance(node, dict):
            node_id = node.get("id")
            if isinstance(node_id, str):
                counts[node_id] = counts.get(node_id, 0) + 1
            for value in node.values():
                if isinstance(value, list):
                    for item in value:
                        collect(item)

    for doc in docs.values():
        collect(doc)
    duplicates = sorted(i for i, n in counts.items() if n > 1)
    if duplicates:
        raise ParseError(f"duplicate stable id(s): {', '.join(duplicates[:8])}")


def count_sections(doc: dict) -> int:
    total = 0

    def visit(node: dict) -> None:
        nonlocal total
        if node.get("document_type") == model.DOCUMENT_TYPE_SECTION:
            total += 1
        for value in node.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        visit(item)

    visit(doc)
    return total
