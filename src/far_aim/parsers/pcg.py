"""FAA PCG HTML edition → canonical PCG JSON (plan §7.3, Phase 5).

Input is an accepted raw snapshot (``sources.pcg``): ``pages/*.html``.
Output is one document per glossary letter (terms nested) plus one for the
publication front matter, each with stable IDs (``pcg-controlled-airspace``),
provenance, and content hashes.

Grammar of the FAA pages (verified over the whole 2026-07-09 Change 3
edition — unlike the DITA-generated AIM, the PCG HTML carries legacy
word-processor classes, so the grammar is wider and messier):

- letter page: ``article.pcg-content`` whose children are
  ``p.CLASS_12`` (the big letter heading, first), term-entry paragraphs
  (``p.glossary-term-entry`` — usually with a ``dfn.term-name`` holding the
  term — or the two ``p.CLASS_21`` entries published without the entry
  class), cross-reference rows (``p.glossary-cross-reference`` with one
  ``span.cross-ref`` see/refer structure), ``ol.glossary-sub-list`` lists,
  ``aside.glossary-note`` note boxes, ``p.CLASS_4`` italic NOTE-/REFERENCE-
  boxes, and plain continuation paragraphs (``CLASS_19`` "OR" between two
  definitions of the same term, ``CLASS_22`` "(a) …" sub-items,
  ``CLASS_20``/``CLASS_23`` parenthetical "(See X.)"/"(Refer to X.)" rows);
- index page: ``h1.pcg-publication-name`` title, the intro/purpose section,
  and the ``aside.pcg-publication-summary`` (edition line, office,
  department) — archived front matter, parsed and lossless-checked. The
  breadcrumb, sidebar, letter-card grid and statistics are navigation
  chrome with derived counts (the printed glossary has none of them) and
  are excluded from the canonical content, like the AIM's toggle buttons.

**Terms are identified by their text**, not by upstream anchors: anchor
``id`` attributes are demonstrably non-unique (``ACROBATIC FLIGHT`` and
``ACROBATIC FLIGHT [ICAO]`` share one), and 165 entries carry no ``dfn`` at
all. Five entries close their ``dfn`` mid-term ("<dfn>AUTOMATIC DEPENDENT
SURVEILLANCE</dfn>-BROADCAST (ADS‐B)- …", "…BROADCAST IN (ADS</dfn>-B
In)-"); a dfn ending without whitespace whose paragraph then continues the
term — an unbalanced parenthetical inside the dfn, a dash directly
followed by an all-caps word, or a letter with no separator at all — is
detected as truncated and the full term is recovered from the verbatim
entry text (it must extend the dfn's text, else the parse fails). For
dfn-less entries the term is split from the entry text deterministically:
the first dash-before-whitespace whose remainder does not begin with an
all-caps word ("AUTOMATIC DEPENDENT SURVEILLANCE- REBROADCAST (ADS-R)-"
splits at the second dash), else the first dash (a definition opening with
an acronym: "DOWNLINK– CPDLC message"), else a trailing dash/colon or the
whole text (stub entries like ``RC``), else a no-space dash before a
capitalized word ("AUTOMATED SERVICES–Services"). A dfn-less entry whose
term-part carries two or more fully-lowercase words is a continuation
paragraph of the current term ("Types of icing are:"), not a new term.
The glossary lists a few terms twice with an "OR" row between alternative
definitions (``COMMON ROUTE``, ``OUTER FIX``) — repeated entries merge
into one term document, in order.

The full entry paragraph is stored **verbatim** as the definition text
(term, separator and all); the extracted ``term`` field is derived
metadata for identity and display, never a rewrite (plan §32.3).

Cross-reference rows resolve to term ids (plan §12.1 Tier 1): linked rows
via the archived page+anchor, unlinked rows by exact term text (an "ICAO
term X" target tries "X [ICAO]" first). Unlinked targets that match no
term stay unresolved rather than failing — the current edition contains
upstream typos ("PREFFERED IFR ROUTES") and truncated markup whose tail
leaks out of the span (re-attached here). External links (the AIM, eCFR
parts) keep their absolute URL as content, as in the AIM parser. The
decorative "📖" and "↗" glyphs on external rows and the screen-reader-only
"(opens in new tab)" spans are presentation chrome and are excluded from
both the captured text and the lossless comparison.

Whitespace runs collapse to one space (``<br>`` → newline); nothing else
about the wording is touched (plan §3.1). Any element outside the grammar
raises :class:`ParseError` — nothing is silently dropped (plan §32.2) —
and every page must pass a lossless-capture check: the multiset of words
in the page's content region equals the multiset of words stored in its
documents.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from far_aim.htmltree import HTMLStructureError, Node, parse_html
from far_aim.models import pcg as model
from far_aim.sources import pcg as pcg_source

_ALL_WS_RE = re.compile(r"\s+")
_WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")
_NEWLINE_WS_RE = re.compile(r" *\n[ \n]*")
# Presentation chrome on external cross-reference rows (see module doc).
_CHROME_CHARS = str.maketrans({"📖": "", "↗": ""})

_SEP_BEFORE_WS_RE = re.compile(r"[-–—](?=\s|$)")
_SEP_NO_SPACE_RE = re.compile(r"[-–—](?=[A-Z][a-z])")
_TRAILING_SEP_RE = re.compile(r"[-–—\s]+$")
_TRAILING_COLON_RE = re.compile(r":\s*$")
_PAREN_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
# For cross-reference lookup only parentheses are stripped: the bracketed
# ``[ICAO]`` tag distinguishes real term pairs and must stay significant.
_PARENS_ONLY_RE = re.compile(r"\([^)]*\)")
_TARGET_DASHES = str.maketrans({"‐": "-", "‑": "-", "–": "-", "—": "-"})
_PARENTHETICAL_REF_RE = re.compile(r"^\((?P<kind>See|Refer to)\s+(?P<target>.+?)\.?\)$")
_ICAO_TERM_RE = re.compile(r"^ICAO term\s+(?P<term>.+)$", re.IGNORECASE)

_INLINE_TAGS = frozenset({"b", "i", "u", "font", "sup", "sub", "dfn", "strong", "em"})
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "ftp://")

# An in-corpus cross-reference link (glossary-X.html#ANCHOR) must name an
# anchor the corpus actually holds; a dangling one means a broken or
# incomplete edition and fails the parse. Tests parsing a deliberately
# partial fixture corpus switch this off. Unlinked textual targets are
# always tolerated (the source contains typos; see module doc).
REQUIRE_RESOLVED_REFERENCES = True


class ParseError(ValueError):
    """The snapshot does not match the parser grammar; nothing was written."""


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def normalize_text(raw: str) -> str:
    """Collapse whitespace runs to one space; ``\\n`` (from ``<br>``) survives."""
    text = _WS_RE.sub(" ", raw)
    text = _NEWLINE_WS_RE.sub("\n", text)
    return text.strip(" \n")


def _words(text: str) -> list[str]:
    return text.split()


def _flat_inline(node: Node, what: str, *, allow_anchor: bool = True) -> str:
    """Flattened inline text of ``node``'s children (chrome stripped)."""
    parts: list[str] = []
    _collect_inline(node, what, parts, allow_anchor=allow_anchor)
    return normalize_text("".join(parts))


def _collect_inline(node: Node, what: str, out: list[str], *, allow_anchor: bool) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(_ALL_WS_RE.sub(" ", child).translate(_CHROME_CHARS))
        elif child.tag == "br":
            out.append("\n")
        elif child.tag in _INLINE_TAGS:
            _collect_inline(child, what, out, allow_anchor=allow_anchor)
        elif child.tag == "span" and child.has_class("sr-only"):
            continue  # screen-reader chrome, not glossary content
        elif child.tag == "a" and allow_anchor:
            _collect_inline(child, what, out, allow_anchor=allow_anchor)
        else:
            raise ParseError(
                f"{what}: unexpected inline element <{child.tag}> "
                f"(class={child.get('class')!r})"
            )


def _flat_inline_child(node: Node, what: str) -> str:
    """Flattened text of one inline element itself (``<br>`` → newline)."""
    if node.tag == "br":
        return ""
    wrapper = Node("#run", {}, None)
    wrapper.children = [node]
    return _flat_inline(wrapper, what)


# Elements whose edges separate words in the rendered page (the inline set
# above does not: "4/10" built from sup/sub is one word).
_BOUNDARY_TAGS = frozenset({"p", "ol", "li", "aside", "span", "br", "h1", "h2", "div", "section"})


def _source_words(node: Node) -> list[str]:
    """Words of a content region, chrome excluded, block edges as separators."""
    parts: list[str] = []

    def walk(current: Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                parts.append(child.translate(_CHROME_CHARS))
            elif child.tag == "span" and child.has_class("sr-only"):
                continue
            else:
                boundary = child.tag in _BOUNDARY_TAGS
                if boundary:
                    parts.append(" ")
                walk(child)
                if boundary:
                    parts.append(" ")

    walk(node)
    return "".join(parts).split()


# ---------------------------------------------------------------------------
# Term extraction
# ---------------------------------------------------------------------------


def split_term(text: str) -> str | None:
    """The term named by a dfn-less entry paragraph, or None for a stub-less
    unsplittable text (the whole text is then the term). See the module doc
    for the validated rule order."""
    candidates = list(_SEP_BEFORE_WS_RE.finditer(text))
    for match in candidates:
        rest = text[match.end() :].strip()
        if not rest:
            continue
        first = rest.split()[0]
        if not (len(first) > 1 and first.isupper()):
            return text[: match.start()].strip()
    if candidates:
        return text[: candidates[0].start()].strip()
    match = _TRAILING_COLON_RE.search(text)
    if match is not None:
        return text[: match.start()].strip()
    match = _SEP_NO_SPACE_RE.search(text)
    if match is not None:
        return text[: match.start()].strip()
    return None


def is_continuation(term: str) -> bool:
    """True when an extracted dfn-less "term" is really continuation prose.

    Two or more fully-lowercase words outside parens/brackets ("Types of
    icing are") — official terms carry at most one connector ("EXPECT
    (ALTITUDE) AT (TIME) or (FIX)").
    """
    outside = _PAREN_RE.sub("", term)
    lower = [t for t in outside.split() if t.isalpha() and t.islower() and len(t) > 1]
    return len(lower) >= 2


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------


class _Page:
    def __init__(self, name: str) -> None:
        self.name = name

    def error(self, message: str) -> ParseError:
        return ParseError(f"{self.name}: {message}")

    def parse(self, html: str) -> Node:
        try:
            return parse_html(html)
        except HTMLStructureError as exc:
            raise self.error(f"malformed or truncated HTML ({exc})") from exc


def _single(root: Node, page: _Page, pred, what: str) -> Node:
    found = root.find_all(pred)
    if len(found) != 1:
        raise page.error(f"expected exactly one {what}, found {len(found)}")
    return found[0]


def _content_article(root: Node, page: _Page) -> Node:
    return _single(
        root, page, lambda n: n.tag == "article" and n.has_class("pcg-content"), "content article"
    )


def _sub_list(ol: Node, page: _Page) -> dict:
    style = (ol.get("type") or "").strip()
    if style not in ("a", "1"):
        raise page.error(f"glossary sub-list with unsupported type {style!r}")
    for attr in ("start", "reversed"):
        if attr in ol.attrs:
            raise page.error(f"glossary sub-list with unsupported numbering control {attr!r}")
    items: list[dict] = []
    for child in ol.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside a glossary sub-list")
            continue
        if child.tag != "li":
            raise page.error(f"unexpected <{child.tag}> inside a glossary sub-list")
        if "value" in child.attrs:
            raise page.error("list item with unsupported numbering control 'value'")
        items.append({"text": _flat_inline(child, page.name)})
    return {"type": "list", "style": style, "items": items}


def _aside_note(aside: Node, page: _Page) -> dict:
    label: str | None = None
    text: str | None = None
    for child in aside.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside a glossary note")
            continue
        if child.tag == "span" and child.has_class("note-label"):
            if label is not None:
                raise page.error("glossary note with two labels")
            label = _flat_inline(child, page.name)
        elif child.tag == "span" and child.has_class("note-content"):
            if text is not None:
                raise page.error("glossary note with two content spans")
            text = _flat_inline(child, page.name)
        else:
            raise page.error(f"unexpected <{child.tag}> inside a glossary note")
    if text is None:
        raise page.error("glossary note without content")
    return {"type": "note", "label": label, "text": text}


def _italic_note(p: Node, page: _Page) -> dict:
    """A ``p.CLASS_4`` italic box: ``<i><b>NOTE-</b><br> text</i>``."""
    bold = p.find(lambda n: n.tag == "b")
    if bold is None:
        raise page.error("italic note box without a bold label")
    label = _flat_inline(bold, page.name)
    text = _flat_inline(p, page.name)
    if not text.startswith(label):
        raise page.error(f"italic note box does not open with its label {label!r}")
    return {"type": "note", "label": label, "text": text[len(label) :].strip(" \n")}


def _cross_reference_row(p: Node, page: _Page) -> dict:
    """A ``p.glossary-cross-reference`` row → a ``reference`` block."""
    spans = [c for c in p.elements() if c.tag == "span" and c.has_class("cross-ref")]
    if len(spans) != 1:
        raise page.error(f"cross-reference row with {len(spans)} cross-ref spans")
    span = spans[0]
    # Markup defects in the source leak the tail of the target text out of
    # the span ("…(CTAF</span></span> AREA.)"); re-attach it.
    leaked = normalize_text(
        "".join(c for c in p.children if isinstance(c, str)).translate(_CHROME_CHARS)
    )
    if span.has_class("see-ref"):
        kind = "see"
    elif span.has_class("refer-ref"):
        kind = "refer"
    else:
        raise page.error(f"cross-reference span with unknown kind {span.classes!r}")
    label: str | None = None
    href: str | None = None
    text_parts: list[str] = []
    for child in span.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("stray text inside a cross-reference span")
            continue
        if child.tag == "span" and child.has_class("cross-ref-label"):
            if label is not None:
                raise page.error("cross-reference row with two labels")
            label = _flat_inline(child, page.name)
        elif child.tag == "span" and (
            child.has_class("cross-ref-term") or child.has_class("cross-ref-target")
        ):
            text_parts.append(_flat_inline(child, page.name))
        elif child.tag == "a" and (
            child.has_class("cross-ref-link") or child.has_class("external-ref-link")
        ):
            text_parts.append(_flat_inline(child, page.name))
            if href is None:
                href = (child.get("href") or "").strip() or None
        elif child.tag in _INLINE_TAGS or child.tag == "br":
            # An <i>/<b> run directly inside the cross-ref span (rare
            # legacy markup): part of the target text.
            text_parts.append(_flat_inline_child(child, page.name))
        else:
            raise page.error(f"unexpected <{child.tag}> inside a cross-reference span")
    text = normalize_text(" ".join(part for part in text_parts if part))
    if label is None or not text:
        raise page.error("cross-reference row without label or target")
    if leaked:
        text = f"{text} {leaked}"
    return {
        "type": "reference",
        "kind": kind,
        "label": label,
        "form": "row",
        "text": text,
        "target": None,
        "source": {"href": href} if href else {},
    }


def _parenthetical_reference(text: str, page: _Page) -> dict:
    match = _PARENTHETICAL_REF_RE.match(text)
    if match is None:
        raise page.error(f"unparseable parenthetical cross-reference {text!r}")
    kind = "see" if match.group("kind") == "See" else "refer"
    return {
        "type": "reference",
        "kind": kind,
        "label": None,
        "form": "parenthetical",
        "text": match.group("target"),
        "raw": text,
        "target": None,
        "source": {},
    }


def _entry_anchors(p: Node) -> list[str]:
    """External hrefs embedded in an entry paragraph (rare; kept as refs)."""
    hrefs: list[str] = []
    for anchor in p.find_all(lambda n: n.tag == "a"):
        href = (anchor.get("href") or "").strip()
        if href:
            hrefs.append(href)
    return hrefs


class _TermBuilder:
    """Accumulates one letter page's terms in document order."""

    def __init__(self, page: _Page, letter: str) -> None:
        self.page = page
        self.letter = letter
        self.order: list[str] = []  # term text, in first-appearance order
        self.terms: dict[str, dict] = {}  # term text → partial term doc
        self.current: str | None = None

    def start(self, term: str, anchor: str | None) -> None:
        if term not in self.terms:
            self.order.append(term)
            self.terms[term] = {"term": term, "content": [], "anchor": anchor}
        elif anchor and not self.terms[term]["anchor"]:
            self.terms[term]["anchor"] = anchor
        self.current = term

    def add(self, block: dict) -> None:
        if self.current is None:
            raise self.page.error(
                f"content block of type {block.get('type')!r} before the first term entry"
            )
        self.terms[self.current]["content"].append(block)


_DASH_CAPS_RE = re.compile(r"^[-–—][A-Z]{2,}")
_WORD_START_RE = re.compile(r"^[A-Za-z0-9]")


def _dfn_truncated(p: Node, dfn: Node, page: _Page) -> bool:
    """True when the ``dfn`` element closes mid-term (see the module doc).

    The dfn ends without trailing whitespace and the paragraph continues
    the term: an unbalanced parenthetical inside the dfn ("…BROADCAST IN
    (ADS" + "-B In)"), a dash directly followed by an all-caps word
    ("…SURVEILLANCE" + "-BROADCAST (ADS‐B)"), or — defensively — a letter
    with no separator at all. A dash followed by a definition ("-A
    transportation system…", a bare trailing "-") is the normal separator,
    not truncation. Validated over every dfn entry of the accepted
    2026-07-09 Change 3 edition: exactly five truncations, no false
    positives.
    """
    dfn_raw: list[str] = []
    after: list[str] = []
    seen = False

    def walk(node: Node) -> None:
        nonlocal seen
        for child in node.children:
            if isinstance(child, Node) and child is dfn:
                _collect_inline(child, page.name, dfn_raw, allow_anchor=True)
                seen = True
            elif isinstance(child, Node) and child.find(lambda n: n is dfn) is not None:
                walk(child)
            elif seen:
                wrapper = Node("#run", {}, None)
                wrapper.children = [child]
                _collect_inline(wrapper, page.name, after, allow_anchor=True)

    walk(p)
    joined = "".join(dfn_raw)
    if not joined or joined[-1:].isspace():
        return False
    term = _TRAILING_SEP_RE.sub("", normalize_text(joined)).strip()
    if term.count("(") > term.count(")") or term.count("[") > term.count("]"):
        return True
    rest = "".join(after)
    return bool(_DASH_CAPS_RE.match(rest) or _WORD_START_RE.match(rest))


def _entry_term(p: Node, page: _Page) -> tuple[str | None, str]:
    """(term or None-for-continuation, verbatim entry text) for an entry ``p``."""
    text = _flat_inline(p, page.name)
    dfn = p.find(lambda n: n.tag == "dfn")
    if dfn is not None:
        term = _TRAILING_SEP_RE.sub("", _flat_inline(dfn, page.name)).strip()
        if term:
            if _dfn_truncated(p, dfn, page):
                recovered = split_term(text) or text
                recovered = _TRAILING_SEP_RE.sub("", recovered).strip()
                if not recovered.startswith(term):
                    raise page.error(
                        f"cannot recover the term truncated by a malformed dfn "
                        f"boundary: dfn says {term!r}, entry text yields {recovered!r}"
                    )
                term = recovered
            return term, text
    term = split_term(text)
    if term is None:
        term = text.strip()
    term = _TRAILING_SEP_RE.sub("", term).strip()
    if not term:
        raise page.error(f"cannot extract a term from entry {text[:60]!r}")
    if dfn is None and is_continuation(term):
        return None, text
    return term, text


def parse_letter_page(name: str, html: str) -> tuple[str, list[dict], list[str]]:
    """(letter, partial term docs in order, source words) for one letter page."""
    kind = pcg_source.classify_page(name)
    assert kind[0] == "letter"
    page_letter = kind[1]
    page = _Page(name)
    root = page.parse(html)
    article = _content_article(root, page)
    source_words = _source_words(article)

    letter: str | None = None
    builder = _TermBuilder(page, page_letter)
    for child in article.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside the content article")
            continue
        if child.tag == "p" and child.has_class("CLASS_12"):
            if letter is not None:
                raise page.error("letter page with two letter headings")
            letter = _flat_inline(child, page.name)
            if letter.lower() != page_letter:
                raise page.error(
                    f"letter heading {letter!r} does not match page letter {page_letter!r}"
                )
            continue
        if letter is None:
            raise page.error(f"content before the letter heading (<{child.tag}>)")
        if child.tag == "p" and (
            child.has_class("glossary-term-entry") or child.has_class("CLASS_21")
        ):
            term, text = _entry_term(child, page)
            if term is None:
                builder.add({"type": "text", "text": text})
                continue
            builder.start(term, child.get("id"))
            builder.add({"type": "entry", "text": text})
            for href in _entry_anchors(child):
                if not href.startswith(_EXTERNAL_SCHEMES):
                    raise page.error(f"entry paragraph links to a non-external URL {href!r}")
                builder.add(
                    {
                        "type": "reference",
                        "kind": "link",
                        "label": None,
                        "form": "embedded",
                        "text": "",
                        "target": None,
                        "url": href,
                        "source": {"href": href},
                    }
                )
        elif child.tag == "p" and child.has_class("glossary-cross-reference"):
            builder.add(_cross_reference_row(child, page))
        elif child.tag == "p" and (child.has_class("CLASS_20") or child.has_class("CLASS_23")):
            builder.add(_parenthetical_reference(_flat_inline(child, page.name), page))
        elif child.tag == "p" and (child.has_class("CLASS_19") or child.has_class("CLASS_22")):
            builder.add({"type": "text", "text": _flat_inline(child, page.name)})
        elif child.tag == "p" and child.has_class("CLASS_4"):
            builder.add(_italic_note(child, page))
        elif child.tag == "ol" and child.has_class("glossary-sub-list"):
            builder.add(_sub_list(child, page))
        elif child.tag == "aside" and child.has_class("glossary-note"):
            builder.add(_aside_note(child, page))
        else:
            raise page.error(
                f"unexpected element <{child.tag}> (class={child.get('class')!r}) "
                "on a letter page"
            )
    if letter is None:
        raise page.error("letter page without a letter heading")
    return letter, [builder.terms[term] for term in builder.order], source_words


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

_INDEX_SECTIONS = {
    "pcg-intro-section": "purpose",
    "pcg-letter-grid": None,  # navigation chrome: letter cards with derived counts
    "pcg-stats": None,  # derived statistics, not glossary content
}


def parse_index_page(name: str, html: str) -> tuple[dict, list[str]]:
    """The index page's front matter as a ``pcg_publication`` document (no
    hash yet) plus the source words of its canonical regions."""
    page = _Page(name)
    root = page.parse(html)
    main = _single(root, page, lambda n: n.tag == "main" and n.has_class("pcg-main"), "main")
    title_node = _single(
        root, page, lambda n: n.tag == "h1" and n.has_class("pcg-publication-name"), "title"
    )
    title = _flat_inline(title_node, page.name)
    article = _content_article(root, page)
    purpose: list[dict] = []
    for section in article.elements():
        if section.tag != "section":
            raise page.error(f"unexpected <{section.tag}> inside the index article")
        known = [cls for cls in section.classes if cls in _INDEX_SECTIONS]
        if len(known) != 1:
            raise page.error(f"unrecognized index section (class={section.get('class')!r})")
        if _INDEX_SECTIONS[known[0]] is None:
            continue
        for child in section.elements():
            if child.tag == "p":
                text = _flat_inline(child, page.name)
                if text:
                    purpose.append({"type": "text", "text": text})
            elif child.tag == "ol":
                items = []
                for li in child.elements():
                    if li.tag != "li":
                        raise page.error(f"unexpected <{li.tag}> in the purpose list")
                    items.append({"text": _flat_inline(li, page.name)})
                style = (child.get("type") or "").strip()
                if style not in ("a", "1"):
                    raise page.error(f"purpose list with unsupported type {style!r}")
                purpose.append({"type": "list", "style": style, "items": items})
            else:
                raise page.error(f"unexpected <{child.tag}> in an index section")
    summary_node = _single(
        main,
        page,
        lambda n: n.tag == "aside" and n.has_class("pcg-publication-summary"),
        "publication summary",
    )
    summary: list[dict] = []
    for p in summary_node.find_all(lambda n: n.tag == "p"):
        text = _flat_inline(p, page.name)
        if text:
            summary.append({"type": "text", "text": text})

    built: list[str] = _words(title)
    _block_words(purpose, built)
    _block_words(summary, built)
    intro = article.find(lambda n: n.tag == "section" and n.has_class("pcg-intro-section"))
    source_words = _words(title)
    if intro is not None:
        source_words += _source_words(intro)
    source_words += _source_words(summary_node)
    _verify_lossless(name, source_words, built)
    return {
        "id": model.PUBLICATION_ID,
        "document_type": model.DOCUMENT_TYPE_PUBLICATION,
        "title": title,
        "purpose": purpose,
        "summary": summary,
    }, source_words


# ---------------------------------------------------------------------------
# Lossless capture
# ---------------------------------------------------------------------------


def _block_words(blocks: list[dict], out: list[str]) -> None:
    for block in blocks:
        kind = block["type"]
        if kind in ("entry", "text"):
            out.extend(_words(block["text"]))
        elif kind == "list":
            for item in block["items"]:
                out.extend(_words(item["text"]))
        elif kind == "note":
            if block.get("label"):
                out.extend(_words(block["label"]))
            out.extend(_words(block["text"]))
        elif kind == "reference":
            if block["form"] == "row":
                out.extend(_words(block["label"]))
                out.extend(_words(block["text"]))
            elif block["form"] == "parenthetical":
                out.extend(_words(block["raw"]))
            # embedded: the link text is already part of its entry block
        else:
            raise ParseError(f"unknown block type {kind!r} during verification")


def _counts(words: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    return counts


def _verify_lossless(name: str, source_words: list[str], built_words: list[str]) -> None:
    source = _counts(source_words)
    built = _counts(built_words)
    if source == built:
        return
    missing = {w: n - built.get(w, 0) for w, n in source.items() if n > built.get(w, 0)}
    extra = {w: n - source.get(w, 0) for w, n in built.items() if n > source.get(w, 0)}
    raise ParseError(
        f"{name}: lossless-capture check failed; "
        f"missing from document: {dict(list(missing.items())[:8])}, "
        f"not in source: {dict(list(extra.items())[:8])}"
    )


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------

_XREF_LINK_RE = re.compile(r"^(?:\./)?glossary-(?P<letter>[a-z])\.html#(?P<anchor>.+)$")


class _TermIndex:
    """Deterministic term-text lookup for cross-reference resolution.

    Exact normalized match first; then "ICAO term X" → ``X [ICAO]``; then a
    match with bracketed/parenthesized segments stripped — the glossary
    routinely cites "ADVISORY CIRCULAR" for the term "ADVISORY CIRCULAR
    (AC)" — accepted only when unambiguous across the corpus.
    """

    def __init__(self) -> None:
        self.exact: dict[str, str] = {}
        self.stripped: dict[str, str | None] = {}  # None marks an ambiguous key

    def add(self, term: str, term_id: str) -> None:
        self.exact[_normalize_target(term)] = term_id
        stripped = _normalize_target(_PARENS_ONLY_RE.sub("", term))
        if stripped and stripped != _normalize_target(term):
            if stripped in self.stripped and self.stripped[stripped] != term_id:
                self.stripped[stripped] = None
            else:
                self.stripped.setdefault(stripped, term_id)

    def lookup(self, text: str) -> str | None:
        key = _normalize_target(text)
        if key in self.exact:
            return self.exact[key]
        icao = _ICAO_TERM_RE.match(text.strip())
        if icao is not None:
            base = _normalize_target(icao.group("term"))
            for candidate in (f"{base} [icao]", base):
                if candidate in self.exact:
                    return self.exact[candidate]
            resolved = self.stripped.get(f"{base} [icao]") or self.stripped.get(base)
            if resolved:
                return resolved
        resolved = self.stripped.get(key)
        if resolved:
            return resolved
        # The citing text itself may carry a parenthetical the term lacks.
        stripped_key = _normalize_target(_PARENS_ONLY_RE.sub("", text))
        if stripped_key != key:
            if stripped_key in self.exact:
                return self.exact[stripped_key]
            resolved = self.stripped.get(stripped_key)
            if resolved:
                return resolved
        return None


def _resolve_references(
    terms_index: _TermIndex,
    anchors: dict[tuple[str, str], str],
    term_doc: dict,
    index_url_base: str,
) -> None:
    """Resolve every reference block of ``term_doc`` in place."""
    for block in term_doc["content"]:
        if block.get("type") != "reference":
            continue
        href = block.get("source", {}).get("href")
        if block["kind"] == "link":
            continue  # embedded external link; url already set
        if href:
            match = _XREF_LINK_RE.match(href)
            if match is not None:
                key = (f"glossary-{match.group('letter')}.html", match.group("anchor"))
                target = anchors.get(key)
                if target is None:
                    if REQUIRE_RESOLVED_REFERENCES:
                        raise ParseError(
                            f"{term_doc['id']}: cross-reference {href!r} names an anchor "
                            "this edition does not contain; corpus is broken or incomplete"
                        )
                else:
                    block["target"] = target
                continue
            if href.startswith(_EXTERNAL_SCHEMES):
                block["url"] = href
                continue
            # A relative link outside the glossary (the AIM, sibling
            # publications): content is the absolute destination.
            block["url"] = urljoin(index_url_base, href)
            continue
        target = terms_index.lookup(block["text"])
        if target is not None:
            block["target"] = target


def _normalize_target(text: str) -> str:
    """Lookup key: whitespace collapsed, unicode dashes unified, lowercase."""
    text = text.translate(_TARGET_DASHES)
    return _ALL_WS_RE.sub(" ", text).strip().rstrip(".").strip().lower()


# ---------------------------------------------------------------------------
# Corpus assembly
# ---------------------------------------------------------------------------


def _with_hash(doc: dict) -> dict:
    doc["canonical_hash"] = model.canonical_hash(doc)
    return doc


def _doc_source(source: dict, page: str, anchor: str | None = None) -> dict:
    return {**source, "url": pcg_source.page_url(source["url"], page, anchor)}


def build_pcg_docs(snapshot_dir, metadata: dict, source: dict) -> dict[str, dict]:
    """Parse every page of an accepted snapshot into canonical documents.

    Returns documents keyed by output stem (``publication``, ``letter-a``).
    Every gate — grammar, per-page lossless capture, unique term ids —
    must pass before anything is returned.
    """
    files = metadata["files"]
    index_rel = f"{pcg_source.PAGES_DIR}/{pcg_source.INDEX_PAGE}"
    if index_rel not in files:
        raise ParseError("archived snapshot has no index page")
    page_names = sorted(
        (
            rel.split("/", 1)[1]
            for rel in files
            if rel.startswith(f"{pcg_source.PAGES_DIR}/") and rel != index_rel
        ),
        key=pcg_source.page_sort_key,
    )
    try:
        pcg_source.check_page_set(page_names)
    except pcg_source.FetchError as exc:
        raise ParseError(f"archived snapshot: {exc}") from exc

    letters: dict[str, tuple[str, str, list[dict], list[str]]] = {}
    for name in page_names:
        html = (snapshot_dir / pcg_source.PAGES_DIR / name).read_text(encoding="utf-8")
        letter, terms, source_words = parse_letter_page(name, html)
        letters[pcg_source.classify_page(name)[1]] = (name, letter, terms, source_words)

    # Global identity: term text → id, and (page, anchor) → id. Term ids
    # must be unique across the whole corpus; anchors are upstream layout
    # and non-unique, so the first entry bearing one wins deterministically.
    terms_index = _TermIndex()
    by_id: dict[str, str] = {}
    anchors: dict[tuple[str, str], str] = {}
    for key in sorted(letters):
        name, _, terms, _ = letters[key]
        for term in terms:
            term_id = model.term_id(term["term"])
            if term_id in by_id:
                raise ParseError(
                    f"{name}: terms {by_id[term_id]!r} and {term['term']!r} "
                    f"collide on id {term_id!r}"
                )
            terms_index.add(term["term"], term_id)
            by_id[term_id] = term["term"]
            if term["anchor"]:
                anchors.setdefault((name, term["anchor"]), term_id)

    docs: dict[str, dict] = {}
    index_html = (snapshot_dir / index_rel).read_text(encoding="utf-8")
    publication, _ = parse_index_page(pcg_source.INDEX_PAGE, index_html)
    publication["source"] = _doc_source(source, pcg_source.INDEX_PAGE)
    docs["publication"] = _with_hash(publication)

    index_url_base = source["url"]
    for key in sorted(letters):
        name, letter, terms, source_words = letters[key]
        term_docs: list[dict] = []
        built: list[str] = _words(letter)
        for term in terms:
            doc = {
                "id": model.term_id(term["term"]),
                "document_type": model.DOCUMENT_TYPE_TERM,
                "letter": letter,
                "term": term["term"],
                "content": term["content"],
                "source": _doc_source(source, name, term["anchor"]),
            }
            _resolve_references(terms_index, anchors, doc, index_url_base)
            _block_words(doc["content"], built)
            term_docs.append(_with_hash(doc))
        _verify_lossless(name, source_words, built)
        letter_doc = {
            "id": model.letter_id(key),
            "document_type": model.DOCUMENT_TYPE_LETTER,
            "letter": letter,
            "terms": term_docs,
            "source": _doc_source(source, name),
        }
        docs[f"letter-{key}"] = _with_hash(letter_doc)

    _verify_unique_ids(docs)
    return docs


def _verify_unique_ids(docs: dict[str, dict]) -> None:
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "document_type" in node:
                doc_id = node.get("id")
                if not isinstance(doc_id, str) or doc_id in seen:
                    raise ParseError(f"duplicate or missing document id {doc_id!r}")
                seen.add(doc_id)
            for value in node.values():
                if isinstance(value, list):
                    for item in value:
                        walk(item)

    for doc in docs.values():
        walk(doc)


def count_terms(doc: dict) -> int:
    if doc.get("document_type") != model.DOCUMENT_TYPE_LETTER:
        return 0
    return len(doc["terms"])
