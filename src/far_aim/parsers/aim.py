"""FAA AIM HTML edition → canonical AIM JSON (plan §7.2, Phase 4).

Input is an accepted raw snapshot (``sources.aim``): ``pages/*.html`` and
``figures/*``. Output is one document per chapter (sections nested,
paragraphs nested in sections) and one per appendix, each with stable IDs
(``aim-4-1-9``), provenance, and content hashes.

Grammar of the FAA pages (DITA-generated HTML5, verified over the whole
2026-07-09 Change 3 edition):

- section page: ``h1.chapter-title`` "Chapter 4. Air Traffic Control",
  ``h2.section-title`` "Section 1. Services Available to Pilots" (chapter 0's
  page uses ``h1.title.topictitle1`` alone), then ``div.body.conbody`` whose
  children are ``h4.paragraph-title`` (``id="4-1-9"``, text "4-1-9. Heading")
  followed by that paragraph's blocks; blocks before the first ``h4`` are
  section-level content;
- chapter page: ``h1.title.topictitle1`` "Chapter 4. …" and a
  ``ol.book-chapter`` table of contents (section → paragraph links) that must
  agree exactly with the section pages;
- appendix page: ``h1.title.topictitle1`` heading and ``div.body.conbody``
  opening with the designation "Appendix N. <heading>", which must agree
  with the filename's number and the title;
- index page: ``h1.title.topictitle1`` title, ``div.publication-description``
  and the ``div.publication-summary`` columns (edition line, issuing office,
  department) — archived front matter, parsed and lossless-checked like every
  other page;
- blocks: ``p.p`` text, ``ol.level-one`` … ``ol.level-six`` lists (markers
  ``a.`` ``1.`` ``(a)`` ``(1)`` ``[a]`` ``[1]`` are CSS-generated and recorded
  as list levels, never as text; explicit numbering controls — ``start``,
  ``reversed``, ``li value`` — are rejected, since markers derived from
  position would then misstate the official enumeration), ``aside`` note/example/reference/
  phraseology boxes (``p.block-title`` + ``div.block-content``),
  ``figure.fig`` (figcaption ``FIG n-n-n`` + title, one ``img``), ``table``
  (optionally wrapped in ``div.table-scroll-box``; caption ``TBL n-n-n`` +
  title), bare ``img`` (form reproductions), and ``h2.title.sectiontitle``;
- inline: ``strong``/``em``/``sup``/``sub``/``span``/``abbr`` are flattened
  to their text (the raw archive remains the styled record); ``br`` becomes a
  newline; ``a`` anchors keep their text inline and are additionally
  collected as explicit references (``chapN_section_M.html#N-M-K`` →
  ``aim-N-M-K``, ``#chapN_section_M`` → ``aim-N-M``, ``appendix_N.html`` →
  ``aim-appendix-N``; an in-corpus link to a document the edition lacks
  fails the parse; external URLs are recorded as hashed ``url`` values).

Source-layout locations — the page a document came from (``source.url``),
an anchor's raw ``href``, a figure's image path — live under ``source``
keys, which canonical hashing strips (plan §14.4): a renamed page or image
file with unchanged wording must not read as a content change.

Whitespace runs collapse to one space; nothing else about the wording is
touched (plan §3.1). Any element outside the grammar raises
:class:`ParseError` — nothing is silently dropped (plan §32.2) — and every
page must pass a lossless-capture check: the multiset of words in the
page's content region equals the multiset of words stored in its documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.htmltree import HTMLStructureError, Node, parse_html
from far_aim.models import aim as model
from far_aim.sources import aim as aim_source

# Source whitespace (including raw newlines) is insignificant; a newline in
# normalized text comes only from <br>.
_ALL_WS_RE = re.compile(r"\s+")
_WS_RE = re.compile(r"[ \t\r\f\v\xa0  ]+")
_NEWLINE_WS_RE = re.compile(r" *\n[ \n]*")
_CHAPTER_TITLE_RE = re.compile(r"^Chapter (?P<n>\d{1,2})\.\s*(?P<heading>.+)$")
_SECTION_TITLE_RE = re.compile(r"^Section (?P<n>\d{1,2})\.\s*(?P<heading>.+)$")
_PARAGRAPH_TITLE_RE = re.compile(r"^(?P<number>\d{1,2}-\d{1,2}-\d{1,3})\.\s*(?P<heading>.+)$")
_TOC_PARAGRAPH_RE = re.compile(r"^(?P<number>\d{1,2}-\d{1,2}-\d{1,3})\.\s*(?P<heading>.*)$")
_TOC_SECTION_LABEL_RE = re.compile(r"^Section (?P<n>\d{1,2})\.$")
_APPENDIX_DESIGNATION_RE = re.compile(r"^Appendix (?P<n>\d{1,2})\.\s*(?P<heading>.+)$")
_LIST_LEVELS = {
    "level-one": 1,
    "level-two": 2,
    "level-three": 3,
    "level-four": 4,
    "level-five": 5,
    "level-six": 6,
}
_NOTE_KINDS = {
    "note-box": "note",
    "example-box": "example",
    "reference-box": "reference",
    "phraseology-box": "phraseology",
}
_INLINE_TAGS = frozenset({"strong", "em", "sup", "sub", "span", "abbr", "b", "i", "u"})
_XREF_PARAGRAPH_RE = re.compile(
    r"^(?:\./)?chap(?P<c>\d{1,2})_section_(?P<s>\d{1,2})\.html#(?P<p>\d{1,2}-\d{1,2}-\d{1,3})$"
)
_XREF_SECTION_RE = re.compile(
    r"^(?:\./)?chap(?P<c>\d{1,2})_section_(?P<s>\d{1,2})\.html(?:#chap(?P=c)_section_(?P=s))?$"
)
_XREF_APPENDIX_RE = re.compile(r"^(?:\./)?appendix_(?P<n>\d{1,2})\.html(?:#.*)?$")
_XREF_CHAPTER_RE = re.compile(r"^(?:\./)?chap_(?P<c>\d{1,2})\.html(?:#.*)?$")


class ParseError(ValueError):
    """The snapshot does not match the parser grammar; nothing was written."""


# An in-corpus link (page + optional paragraph anchor) must name a document
# the corpus actually holds; a dangling one means a broken or incomplete
# edition and fails the parse. Tests parsing a deliberately partial fixture
# corpus switch this off.
REQUIRE_RESOLVED_REFERENCES = True
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "ftp://")


def _paragraph_href(href: str, page: _Page | None = None) -> str | None:
    """The paragraph number a ``chapN_section_M.html#N-M-K`` href denotes.

    Returns None for any other href. A paragraph href whose fragment names a
    different chapter/section than its page is internally inconsistent — the
    link cannot mean both — and is rejected as malformed source rather than
    resolved to either reading.
    """
    match = _XREF_PARAGRAPH_RE.match(href)
    if match is None:
        return None
    number = match.group("p")
    fragment = model.PARAGRAPH_RE.match(number)
    assert fragment is not None
    if (int(fragment.group("chapter")), int(fragment.group("section"))) != (
        int(match.group("c")),
        int(match.group("s")),
    ):
        message = f"paragraph link {href!r} names a paragraph outside its own page"
        raise page.error(message) if page is not None else ParseError(message)
    return number


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def normalize_text(raw: str) -> str:
    """Collapse whitespace runs to one space; ``\\n`` (from ``<br>``) survives.

    Blank lines produced by consecutive breaks collapse to a single newline;
    leading/trailing whitespace is stripped. Callers must already have
    mapped source newlines to spaces (:func:`_inline` does).
    """
    text = _WS_RE.sub(" ", raw)
    text = _NEWLINE_WS_RE.sub("\n", text)
    return text.strip(" \n")


def collapse_text(raw: str) -> str:
    """Raw element text with every whitespace run (newlines included) → one space."""
    return _ALL_WS_RE.sub(" ", raw).strip()


def _words(text: str) -> list[str]:
    return text.split()


# Elements whose edges separate words in the rendered page. Inline styling
# (strong/em/sup/sub) and anchors do not: "SO<sub>2</sub>" is one word, and
# anchor text is kept inline by the builders too.
_BOUNDARY_TAGS = frozenset(
    {
        "p", "div", "li", "ol", "ul", "td", "th", "tr", "thead", "tbody", "tfoot", "table",
        "colgroup", "aside", "figure", "figcaption", "caption", "h1", "h2", "h3", "h4",
        "span", "br", "img",
    }
)


def _source_words(node: Node) -> list[str]:
    """Words of a content region, with block edges counted as separators."""
    parts: list[str] = []

    def walk(current: Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                boundary = child.tag in _BOUNDARY_TAGS
                if boundary:
                    parts.append(" ")
                walk(child)
                if boundary:
                    parts.append(" ")

    walk(node)
    return "".join(parts).split()


@dataclass
class _Refs:
    """Explicit references collected, in order, while flattening a page's text.

    Each paragraph (or the section-level preamble) owns the slice appended
    while its own blocks were parsed; duplicates collapse per owner at
    resolution time.
    """

    items: list[dict] = field(default_factory=list)

    def add(self, href: str, text: str) -> None:
        self.items.append({"href": href, "text": text})


class _Page:
    """Parsing context for one page: name, node lookup, reference sink."""

    def __init__(self, name: str, figure_hashes: dict[str, str]) -> None:
        self.name = name
        self.figure_hashes = figure_hashes
        self.refs = _Refs()

    def error(self, message: str) -> ParseError:
        return ParseError(f"{self.name}: {message}")

    def parse(self, html: str) -> Node:
        """Strict parse: an archived page with unbalanced markup fails the parse."""
        try:
            return parse_html(html)
        except HTMLStructureError as exc:
            raise self.error(f"malformed or truncated HTML ({exc})") from exc


_BR_MARK = "\n"


def _inline(node: Node, page: _Page, out: list[str]) -> None:
    """Append the flattened inline text of ``node``'s children to ``out``."""
    for child in node.children:
        if isinstance(child, str):
            out.append(_ALL_WS_RE.sub(" ", child))
        elif child.tag == "br":
            out.append(_BR_MARK)
        elif child.tag == "a":
            text_parts: list[str] = []
            _inline(child, page, text_parts)
            text = normalize_text("".join(text_parts))
            href = (child.get("href") or "").strip()
            if href:
                page.refs.add(href, text)
            out.extend(text_parts)
        elif child.tag in _INLINE_TAGS:
            _inline(child, page, out)
        else:
            raise page.error(
                f"unexpected inline element <{child.tag}> (class={child.get('class')!r})"
            )


def _is_block(node: Node) -> bool:
    return node.tag in {"p", "div", "ol", "ul", "aside", "figure", "table", "img", "h2", "h3", "h4"}


def _image_block(img: Node, page: _Page) -> dict:
    src = (img.get("src") or "").strip()
    match = aim_source.IMAGE_SRC_RE.match(src)
    if match is None:
        raise page.error(f"image outside images/: {src!r}")
    name = match.group("name")
    sha = page.figure_hashes.get(name)
    if sha is None:
        raise page.error(f"figure {name!r} is referenced but missing from the archived snapshot")
    alt = normalize_text(img.get("alt") or "")
    return {"type": "image", "alt": alt, "sha256": sha, "source": {"src": f"images/{name}"}}


def _paragraph_blocks(p: Node, page: _Page) -> list[dict]:
    """A ``p.p``/``div.p``: text, split around any embedded image."""
    blocks: list[dict] = []
    buffer: list[str] = []

    def flush() -> None:
        text = normalize_text("".join(buffer))
        buffer.clear()
        if text:
            blocks.append({"type": "text", "text": text})

    for child in p.children:
        if isinstance(child, Node) and child.tag == "img":
            flush()
            blocks.append(_image_block(child, page))
        elif isinstance(child, Node) and _is_block(child):
            flush()
            blocks.extend(_block(child, page))
        else:
            wrapper = Node("#run", {}, None)
            wrapper.children = [child]
            _inline(wrapper, page, buffer)
    flush()
    return blocks


def _collect_blocks(container: Node, page: _Page) -> list[dict]:
    """Blocks of a container whose children mix inline runs and block elements."""
    blocks: list[dict] = []
    buffer: list[str] = []

    def flush() -> None:
        text = normalize_text("".join(buffer))
        buffer.clear()
        if text:
            blocks.append({"type": "text", "text": text})

    for child in container.children:
        if isinstance(child, Node) and _is_block(child):
            flush()
            blocks.extend(_block(child, page))
        else:
            wrapper = Node("#run", {}, None)
            wrapper.children = [child]
            _inline(wrapper, page, buffer)
    flush()
    return blocks


def _list_block(ol: Node, page: _Page) -> dict:
    level = next((_LIST_LEVELS[c] for c in ol.classes if c in _LIST_LEVELS), None)
    if level is None:
        raise page.error(f"ordered list without a level class: {ol.classes!r}")
    # Markers are derived from level and position (CSS convention) and are
    # outside the lossless word check, so an explicit numbering control the
    # renderer does not honor would silently mis-enumerate official text.
    for attr in ("start", "reversed"):
        if attr in ol.attrs:  # `reversed` is a bare boolean attribute (value None)
            raise page.error(f"ordered list with unsupported numbering control {attr!r}")
    items: list[dict] = []
    for child in ol.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside <ol>")
            continue
        if child.tag != "li":
            raise page.error(f"unexpected <{child.tag}> inside <ol>")
        if "value" in child.attrs:
            raise page.error("list item with unsupported numbering control 'value'")
        items.append({"blocks": _collect_blocks(child, page)})
    block: dict = {"type": "list", "level": level, "items": items}
    html_type = ol.get("type")
    if html_type:
        block["html_type"] = html_type
    return block


def _note_block(aside: Node, page: _Page) -> dict:
    kind = next((_NOTE_KINDS[c] for c in aside.classes if c in _NOTE_KINDS), None)
    if kind is None:
        raise page.error(f"aside without a known box class: {aside.classes!r}")
    title: str | None = None
    blocks: list[dict] = []
    for child in aside.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside <aside>")
            continue
        if child.tag == "p" and child.has_class("block-title"):
            if title is not None:
                raise page.error("aside with two block titles")
            parts: list[str] = []
            _inline(child, page, parts)
            title = normalize_text("".join(parts))
        elif child.tag == "div" and child.has_class("block-content"):
            blocks.extend(_collect_blocks(child, page))
        else:
            raise page.error(f"unexpected <{child.tag}> inside <aside>")
    if title is None:
        raise page.error(f"{kind} box without a title")
    return {"type": "note", "kind": kind, "title": title, "blocks": blocks}


def _caption_parts(caption: Node, page: _Page, number_class: str, title_class: str) -> tuple:
    number: str | None = None
    title: str | None = None
    for child in caption.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("stray text in caption")
            continue
        parts: list[str] = []
        _inline(child, page, parts)
        text = normalize_text("".join(parts))
        if child.tag == "span" and child.has_class(number_class):
            number = text
        elif child.tag == "span" and child.has_class(title_class):
            title = text
        else:
            raise page.error(f"unexpected <{child.tag}> in caption")
    return number, title


def _figure_block(figure: Node, page: _Page) -> dict:
    number: str | None = None
    title: str | None = None
    image: dict | None = None
    for child in figure.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside <figure>")
            continue
        if child.tag == "figcaption":
            number, title = _caption_parts(child, page, "fig-number", "fig-title")
        elif child.tag == "img":
            if image is not None:
                raise page.error("figure with more than one image")
            image = _image_block(child, page)
        elif child.tag == "br":
            continue
        else:
            raise page.error(f"unexpected <{child.tag}> inside <figure>")
    if image is None:
        raise page.error("figure without an image")
    return {
        "type": "figure",
        "number": number,
        "title": title,
        "image": {"alt": image["alt"], "sha256": image["sha256"], "source": image["source"]},
    }


def _cell(td: Node, page: _Page) -> dict:
    cell: dict = {"blocks": _collect_blocks(td, page)}
    for attr in ("colspan", "rowspan"):
        value = td.get(attr)
        if value:
            try:
                cell[attr] = int(value)
            except ValueError as exc:
                raise page.error(f"non-integer {attr}={value!r}") from exc
    if td.tag == "th":
        cell["header"] = True
    return cell


def _rows(group: Node, page: _Page) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for tr in group.children:
        if isinstance(tr, str):
            if tr.strip():
                raise page.error("text directly inside a table row group")
            continue
        if tr.tag != "tr":
            raise page.error(f"unexpected <{tr.tag}> in table row group")
        cells: list[dict] = []
        for td in tr.children:
            if isinstance(td, str):
                if td.strip():
                    raise page.error("text directly inside <tr>")
                continue
            if td.tag not in ("td", "th"):
                raise page.error(f"unexpected <{td.tag}> inside <tr>")
            cells.append(_cell(td, page))
        rows.append(cells)
    return rows


def _table_block(table: Node, page: _Page) -> dict:
    number: str | None = None
    title: str | None = None
    header_rows: list[list[dict]] = []
    rows: list[list[dict]] = []
    foot_rows: list[list[dict]] = []
    for child in table.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside <table>")
            continue
        if child.tag == "caption":
            number, title = _caption_parts(child, page, "tbl-number", "tbl-title")
        elif child.tag == "colgroup":
            continue
        elif child.tag == "thead":
            header_rows.extend(_rows(child, page))
        elif child.tag == "tbody":
            rows.extend(_rows(child, page))
        elif child.tag == "tfoot":
            foot_rows.extend(_rows(child, page))
        elif child.tag == "tr":
            rows.append(_rows_single(child, page))
        else:
            raise page.error(f"unexpected <{child.tag}> inside <table>")
    block: dict = {
        "type": "table",
        "number": number,
        "title": title,
        "header_rows": header_rows,
        "rows": rows,
        "foot_rows": foot_rows,
    }
    if table.has_class("borderless-header"):
        block["style"] = "borderless-header"
    return block


def _rows_single(tr: Node, page: _Page) -> list[dict]:
    wrapper = Node("tbody", {}, None)
    wrapper.children = [tr]
    return _rows(wrapper, page)[0]


def _block(node: Node, page: _Page) -> list[dict]:
    """Dispatch one block-level element to its builder (allowlist)."""
    tag = node.tag
    if tag == "p" and node.has_class("p"):
        return _paragraph_blocks(node, page)
    if tag == "div" and node.has_class("p"):
        return _paragraph_blocks(node, page)
    if tag == "div" and node.has_class("table-scroll-box"):
        blocks: list[dict] = []
        for child in node.children:
            if isinstance(child, str):
                if child.strip():
                    raise page.error("text directly inside table wrapper")
                continue
            if child.tag != "table":
                raise page.error(f"unexpected <{child.tag}> inside table wrapper")
            blocks.append(_table_block(child, page))
        return blocks
    if tag == "ol":
        return [_list_block(node, page)]
    if tag == "aside":
        return [_note_block(node, page)]
    if tag == "figure":
        return [_figure_block(node, page)]
    if tag == "table":
        return [_table_block(node, page)]
    if tag == "img":
        return [_image_block(node, page)]
    if tag == "h2" and node.has_class("sectiontitle"):
        parts: list[str] = []
        _inline(node, page, parts)
        return [{"type": "heading", "level": 2, "text": normalize_text("".join(parts))}]
    raise page.error(f"unexpected block element <{tag}> (class={node.get('class')!r})")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _single(root: Node, page: _Page, pred, what: str) -> Node:
    found = root.find_all(pred)
    if len(found) != 1:
        raise page.error(f"expected exactly one {what}, found {len(found)}")
    return found[0]


def _heading_text(node: Node, page: _Page) -> str:
    parts: list[str] = []
    _inline(node, page, parts)
    return normalize_text("".join(parts))


def _conbody(root: Node, page: _Page) -> Node:
    return _single(
        root, page, lambda n: n.tag == "div" and n.has_class("conbody"), "content body"
    )


@dataclass
class SectionPage:
    chapter: int
    section: int
    chapter_heading: str | None
    heading: str
    content: list[dict]
    paragraphs: list[dict]  # partial paragraph docs (no source/hash yet)
    refs: list[dict]  # references owned by the section-level content
    source_words: list[str]


def parse_section_page(name: str, html: str, figure_hashes: dict[str, str]) -> SectionPage:
    kind = aim_source.classify_page(name)
    if kind[0] != "section":
        raise ParseError(f"{name}: not a section page")
    chapter, section = kind[1], kind[2]
    page = _Page(name, figure_hashes)
    root = page.parse(html)

    chapter_heading: str | None = None
    chapter_titles = root.find_all(lambda n: n.tag == "h1" and n.has_class("chapter-title"))
    section_titles = root.find_all(lambda n: n.tag == "h2" and n.has_class("section-title"))
    if chapter_titles or section_titles:
        # Official headings sit outside the body's lossless check, so a
        # duplicated title element must fail rather than be half-dropped.
        if len(chapter_titles) != 1 or len(section_titles) != 1:
            raise page.error(
                f"expected exactly one chapter title and one section title, found "
                f"{len(chapter_titles)} and {len(section_titles)}"
            )
        h1_chapter, h2_section = chapter_titles[0], section_titles[0]
        match = _CHAPTER_TITLE_RE.match(_heading_text(h1_chapter, page))
        if match is None or int(match.group("n")) != chapter:
            raise page.error(f"chapter title does not match page chapter {chapter}")
        chapter_heading = match.group("heading")
        match = _SECTION_TITLE_RE.match(_heading_text(h2_section, page))
        if match is None or int(match.group("n")) != section:
            raise page.error(f"section title does not match page section {section}")
        heading = match.group("heading")
    else:
        h1 = _single(
            root, page, lambda n: n.tag == "h1" and n.has_class("topictitle1"), "page title"
        )
        heading = _heading_text(h1, page)

    body = _conbody(root, page)
    source_words = _source_words(body)
    content: list[dict] = []
    paragraphs: list[dict] = []
    current: dict | None = None
    buffer: list[str] = []
    seen_numbers: list[str] = []
    ref_start = 0
    content_refs: list[dict] = []

    def flush(target: list[dict]) -> None:
        text = normalize_text("".join(buffer))
        buffer.clear()
        if text:
            target.append({"type": "text", "text": text})

    def target() -> list[dict]:
        return content if current is None else current["content"]

    def close_scope() -> None:
        nonlocal ref_start
        owned = page.refs.items[ref_start:]
        ref_start = len(page.refs.items)
        if current is None:
            content_refs.extend(owned)
        else:
            current["_refs"] = owned

    for child in body.children:
        if isinstance(child, Node) and child.tag == "h4":
            flush(target())
            close_scope()
            if not child.has_class("paragraph-title"):
                raise page.error("h4 without paragraph-title class")
            title = _heading_text(child, page)
            match = _PARAGRAPH_TITLE_RE.match(title)
            if match is None:
                raise page.error(f"unparseable paragraph title {title!r}")
            number = match.group("number")
            if child.get("id") != number:
                raise page.error(f"paragraph {number} id attribute is {child.get('id')!r}")
            parsed = model.PARAGRAPH_RE.match(number)
            assert parsed is not None
            if int(parsed.group("chapter")) != chapter or int(parsed.group("section")) != section:
                raise page.error(
                    f"paragraph {number} does not belong to section {chapter}-{section}"
                )
            if seen_numbers and int(parsed.group("number")) <= int(
                model.PARAGRAPH_RE.match(seen_numbers[-1]).group("number")  # type: ignore[union-attr]
            ):
                raise page.error(f"paragraph {number} out of order after {seen_numbers[-1]}")
            seen_numbers.append(number)
            current = {
                "id": model.paragraph_id(number),
                "document_type": model.DOCUMENT_TYPE_PARAGRAPH,
                "chapter": chapter,
                "section": section,
                "paragraph": number,
                "number": int(parsed.group("number")),
                "heading": match.group("heading"),
                "content": [],
            }
            paragraphs.append(current)
        elif isinstance(child, Node) and _is_block(child):
            flush(target())
            target().extend(_block(child, page))
        else:
            wrapper = Node("#run", {}, None)
            wrapper.children = [child]
            _inline(wrapper, page, buffer)
    flush(target())
    close_scope()

    return SectionPage(
        chapter=chapter,
        section=section,
        chapter_heading=chapter_heading,
        heading=heading,
        content=content,
        paragraphs=paragraphs,
        refs=content_refs,
        source_words=source_words,
    )


@dataclass
class ChapterSection:
    """One entry of a chapter contents page."""

    page: str  # section page filename
    label: str  # "Section 1." as printed
    heading: str
    entries: list[tuple[str, str]]  # (paragraph number, heading)
    extra: list[str]  # entries duplicating the section entry (chapter 0); lossless only


@dataclass
class ChapterPage:
    chapter: int
    heading: str
    sections: list[ChapterSection]


# The chapter contents page is a fixed widget: heading, then one collapsible
# entry per section with its paragraph list. Every element is checked
# against this allowlist and every word (outside the toggle buttons, which
# are controls, not content) must be accounted for by the built structure.
_CHAPTER_ALLOWED = {
    "h1": {"topictitle1"},
    "div": {"chapter-sections", "section-header", "section-content"},
    "ol": {"book-chapter"},
    "li": {"collapsible-section", None},
    "button": {"section-toggle"},
    "span": {"toggle-icon", "section-number"},
    "a": {None},
    "ul": {"paragraph-list"},
}


def _check_chapter_grammar(node: Node, page: _Page) -> None:
    for child in node.elements():
        allowed = _CHAPTER_ALLOWED.get(child.tag)
        marker = next((c for c in child.classes if c in (allowed or ())), None)
        if allowed is None or (marker is None and None not in allowed):
            raise page.error(
                f"unexpected element <{child.tag}> (class={child.get('class')!r}) "
                "in chapter contents"
            )
        if child.tag != "button":
            _check_chapter_grammar(child, page)


def _chapter_source_words(article: Node) -> list[str]:
    parts: list[str] = []

    def walk(node: Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag != "button":
                parts.append(" ")
                walk(child)
                parts.append(" ")

    walk(article)
    return "".join(parts).split()


def parse_chapter_page(name: str, html: str) -> ChapterPage:
    kind = aim_source.classify_page(name)
    if kind[0] != "chapter":
        raise ParseError(f"{name}: not a chapter page")
    chapter = kind[1]
    page = _Page(name, {})
    root = page.parse(html)
    article = _single(
        root, page, lambda n: n.tag == "article" and n.get("role") == "article", "article"
    )
    _check_chapter_grammar(article, page)
    h1 = _single(article, page, lambda n: n.tag == "h1" and n.has_class("topictitle1"), "title")
    title = _heading_text(h1, page)
    match = _CHAPTER_TITLE_RE.match(title)
    if match is None or int(match.group("n")) != chapter:
        raise page.error(f"chapter title does not match page chapter {chapter}")
    toc = _single(
        article, page, lambda n: n.tag == "ol" and n.has_class("book-chapter"), "chapter contents"
    )
    sections: list[ChapterSection] = []
    for li in toc.elements():
        if li.tag != "li":
            raise page.error(f"unexpected <{li.tag}> in chapter contents")
        header = li.find(lambda n: n.tag == "div" and n.has_class("section-header"))
        if header is None:
            raise page.error("section entry without header")
        label_node = header.find(lambda n: n.tag == "span" and n.has_class("section-number"))
        anchor = header.find(lambda n: n.tag == "a")
        if anchor is None or label_node is None:
            raise page.error("section entry without number or link")
        href = (anchor.get("href") or "").strip()
        href_match = aim_source.PAGE_NAME_RE.match(href)
        if href_match is None:
            raise page.error(f"section entry links outside the corpus: {href!r}")
        section_page = href_match.group("name") + ".html"
        section_heading = collapse_text(anchor.text())
        entries: list[tuple[str, str]] = []
        extra: list[str] = []
        plist = li.find(lambda n: n.tag == "ul" and n.has_class("paragraph-list"))
        if plist is not None:
            for item in plist.elements():
                link = item.find(lambda n: n.tag == "a")
                if link is None:
                    raise page.error("paragraph entry without link")
                text = collapse_text(link.text())
                link_href = (link.get("href") or "").strip()
                number = _paragraph_href(link_href, page)
                if number is None:
                    # Chapter 0 lists its lone section itself in place of
                    # paragraphs. Such an entry carries no content of its own
                    # only if it duplicates the section entry exactly (same
                    # page, same heading); anything else would be official
                    # wording with no canonical home, so it fails the parse.
                    entry_match = aim_source.PAGE_NAME_RE.match(link_href)
                    if (
                        entry_match is not None
                        and entry_match.group("name") + ".html" == section_page
                        and text == section_heading
                    ):
                        extra.append(text)
                        continue
                    raise page.error(
                        f"contents entry {text!r} → {link_href!r} is neither a paragraph of "
                        f"section {section_page!r} nor a duplicate of its section entry"
                    )
                tm = _TOC_PARAGRAPH_RE.match(text)
                if tm is None or tm.group("number") != number:
                    raise page.error(f"paragraph entry text {text!r} does not match {link_href!r}")
                entries.append((number, tm.group("heading")))
        label = collapse_text(label_node.text())
        label_match = _TOC_SECTION_LABEL_RE.match(label)
        if label_match is None:
            raise page.error(f"unparseable section label {label!r} in chapter contents")
        linked = aim_source.classify_page(section_page)
        if linked[0] != "section" or linked[1] != chapter:
            raise page.error(f"section entry {label!r} links outside chapter {chapter}")
        # The FAA numbers chapter 0's lone "Explanation of Changes" section
        # "Section 1." while publishing it as chap0_section_0.html; that one
        # documented mismatch is tolerated (the label is still preserved in
        # the section document). Everywhere else label and page must agree.
        labelled = int(label_match.group("n"))
        if labelled != linked[2] and not (chapter == 0 and labelled == 1 and linked[2] == 0):
            raise page.error(
                f"section label {label!r} disagrees with its linked page {section_page!r}"
            )
        sections.append(
            ChapterSection(
                page=section_page,
                label=label,
                heading=section_heading,
                entries=entries,
                extra=extra,
            )
        )
    built: list[str] = _words(title)
    for section in sections:
        built.extend(_words(section.label))
        built.extend(_words(section.heading))
        for number, heading in section.entries:
            built.extend(_words(f"{number}. {heading}"))
        for text in section.extra:
            built.extend(_words(text))
    _verify_lossless(name, _chapter_source_words(article), built)
    return ChapterPage(chapter=chapter, heading=match.group("heading"), sections=sections)


_INDEX_ALLOWED = {
    "h1": {"topictitle1"},
    "div": {"publication-description", "publication-summary", "publication-info-left",
            "publication-info-right"},
    "p": {None},
}


def parse_index_page(name: str, html: str, figure_hashes: dict[str, str]) -> dict:
    """The index page's front matter as an ``aim_publication`` document (no hash yet)."""
    page = _Page(name, figure_hashes)
    root = page.parse(html)
    article = _single(
        root, page, lambda n: n.tag == "article" and n.get("role") == "article", "article"
    )
    title: str | None = None
    description: list[dict] = []
    summary: list[dict] = []
    for child in article.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside the index article")
            continue
        allowed = _INDEX_ALLOWED.get(child.tag, set())
        if child.tag == "h1" and child.has_class("topictitle1"):
            if title is not None:
                raise page.error("index page with two titles")
            title = _heading_text(child, page)
        elif child.tag == "div" and child.has_class("publication-description"):
            description.extend(_index_paragraphs(child, page))
        elif child.tag == "div" and child.has_class("publication-summary"):
            for column in child.children:
                if isinstance(column, str):
                    if column.strip():
                        raise page.error("text directly inside the publication summary")
                    continue
                if column.tag != "div" or not (
                    column.has_class("publication-info-left")
                    or column.has_class("publication-info-right")
                ):
                    raise page.error(
                        f"unexpected <{column.tag}> (class={column.get('class')!r}) "
                        "in publication summary"
                    )
                summary.extend(_index_paragraphs(column, page))
        else:
            raise page.error(
                f"unexpected element <{child.tag}> (class={child.get('class')!r}) "
                f"on the index page (allowed: {sorted(allowed) or 'none'})"
            )
    if title is None:
        raise page.error("index page without a title")
    built = _words(title)
    _block_words(description, built)
    _block_words(summary, built)
    _verify_lossless(name, _chapter_source_words(article), built)
    return {
        "id": model.PUBLICATION_ID,
        "document_type": model.DOCUMENT_TYPE_PUBLICATION,
        "title": title,
        "description": description,
        "summary": summary,
        "_refs": page.refs.items,
    }


def _index_paragraphs(container: Node, page: _Page) -> list[dict]:
    blocks: list[dict] = []
    for child in container.children:
        if isinstance(child, str):
            if child.strip():
                raise page.error("text directly inside an index container")
            continue
        if child.tag != "p":
            raise page.error(f"unexpected <{child.tag}> inside index container")
        blocks.extend(_paragraph_blocks(child, page))
    return blocks


@dataclass
class AppendixPage:
    appendix: int
    heading: str
    content: list[dict]
    refs: list[dict]
    source_words: list[str]


def parse_appendix_page(name: str, html: str, figure_hashes: dict[str, str]) -> AppendixPage:
    kind = aim_source.classify_page(name)
    if kind[0] != "appendix":
        raise ParseError(f"{name}: not an appendix page")
    page = _Page(name, figure_hashes)
    root = page.parse(html)
    h1 = _single(root, page, lambda n: n.tag == "h1" and n.has_class("topictitle1"), "page title")
    heading = _heading_text(h1, page)
    body = _conbody(root, page)
    content = _collect_blocks(body, page)
    # The page's own designation ("Appendix 3. Abbreviations/Acronyms", the
    # opening line of every appendix) must agree with the filename that
    # supplies the stable id and with the page title; a swapped or misnamed
    # appendix page would otherwise be published under the wrong citation.
    opening = content[0] if content else None
    designation = (
        _APPENDIX_DESIGNATION_RE.match(opening["text"])
        if opening is not None and opening["type"] == "text"
        else None
    )
    if designation is None:
        raise page.error("appendix page does not open with its 'Appendix N.' designation")
    if int(designation.group("n")) != kind[1] or designation.group("heading") != heading:
        raise page.error(
            f"appendix designation {opening['text']!r} does not match page appendix "
            f"{kind[1]} titled {heading!r}"
        )
    return AppendixPage(
        appendix=kind[1],
        heading=heading,
        content=content,
        refs=page.refs.items,
        source_words=_source_words(body),
    )


# ---------------------------------------------------------------------------
# Lossless capture
# ---------------------------------------------------------------------------


def _block_words(blocks: list[dict], out: list[str]) -> None:
    for block in blocks:
        kind = block["type"]
        if kind in ("text", "heading"):
            out.extend(_words(block["text"]))
        elif kind == "list":
            for item in block["items"]:
                _block_words(item["blocks"], out)
        elif kind == "note":
            out.extend(_words(block["title"]))
            _block_words(block["blocks"], out)
        elif kind == "figure":
            for key in ("number", "title"):
                if block.get(key):
                    out.extend(_words(block[key]))
        elif kind == "image":
            continue
        elif kind == "table":
            for key in ("number", "title"):
                if block.get(key):
                    out.extend(_words(block[key]))
            for rows in (block["header_rows"], block["rows"], block["foot_rows"]):
                for row in rows:
                    for cell in row:
                        _block_words(cell["blocks"], out)
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
# References
# ---------------------------------------------------------------------------


def _resolve_refs(refs: list[dict], known_ids: set[str], owner: str = "") -> list[dict]:
    """Resolve collected anchors; duplicates collapse, order kept.

    In-corpus links become ``target`` ids (their raw href is layout, kept as
    provenance). External destinations are content — an FAA link changed
    while its display text stays would otherwise be invisible to change
    detection — so they are kept as a hashed ``url``. Anything else (a
    relative link outside the corpus grammar) is malformed source.
    """
    resolved: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        href = ref["href"]
        if (href, ref["text"]) in seen:
            continue
        seen.add((href, ref["text"]))
        target: str | None = None
        entry: dict = {"text": ref["text"], "target": None, "source": {"href": href}}
        if (number := _paragraph_href(href)) is not None:
            target = model.paragraph_id(number)
        elif m := _XREF_SECTION_RE.match(href):
            target = model.section_id(int(m.group("c")), int(m.group("s")))
        elif m := _XREF_APPENDIX_RE.match(href):
            target = model.appendix_id(int(m.group("n")))
        elif m := _XREF_CHAPTER_RE.match(href):
            target = model.chapter_id(int(m.group("c")))
        elif href.startswith(_EXTERNAL_SCHEMES):
            entry["url"] = href
        else:
            raise ParseError(
                f"{owner}: link {href!r} ({ref['text']!r}) is neither an AIM page nor an "
                "external URL"
            )
        if target is not None:
            if target not in known_ids:
                if REQUIRE_RESOLVED_REFERENCES:
                    raise ParseError(
                        f"{owner}: link {href!r} ({ref['text']!r}) names {target!r}, which "
                        "this edition does not contain; corpus is broken or incomplete"
                    )
            else:
                entry["target"] = target
        resolved.append(entry)
    return resolved


# ---------------------------------------------------------------------------
# Corpus assembly
# ---------------------------------------------------------------------------


def _with_hash(doc: dict) -> dict:
    doc["canonical_hash"] = model.canonical_hash(doc)
    return doc


def _doc_source(source: dict, page: str, anchor: str | None = None) -> dict:
    """The provenance block of one document: the base ``source`` (whose
    ``url`` is the edition's index page) with ``url`` pointing at the
    document's own page and anchor (plan §7.2)."""
    return {**source, "url": aim_source.page_url(source["url"], page, anchor)}


def build_aim_docs(snapshot_dir: Path, metadata: dict, source: dict) -> dict[str, dict]:
    """Parse every page of an accepted snapshot into canonical documents.

    Returns documents keyed by output stem (``chapter-04``, ``appendix-3``).
    Every gate — grammar, per-page lossless capture, chapter TOC agreement,
    unique IDs — must pass before anything is returned.
    """
    files = metadata["files"]
    figure_hashes = {
        rel.split("/", 1)[1]: entry["sha256"]
        for rel, entry in files.items()
        if rel.startswith(f"{aim_source.FIGURES_DIR}/")
    }
    index_rel = f"{aim_source.PAGES_DIR}/{aim_source.INDEX_PAGE}"
    if index_rel not in files:
        raise ParseError("archived snapshot has no index page")
    page_names = sorted(
        (
            rel.split("/", 1)[1]
            for rel in files
            if rel.startswith(f"{aim_source.PAGES_DIR}/") and rel != index_rel
        ),
        key=aim_source.page_sort_key,
    )
    # The archived page set must satisfy the same structural gate as the
    # index at fetch time (no two pages with one identity, chapter/section
    # agreement, contiguous sections, baseline chapters and appendices).
    try:
        aim_source.check_page_set(page_names)
    except aim_source.FetchError as exc:
        raise ParseError(f"archived snapshot: {exc}") from exc
    chapters: dict[int, tuple[str, ChapterPage]] = {}
    sections: dict[tuple[int, int], tuple[str, SectionPage]] = {}
    appendices: dict[int, tuple[str, AppendixPage]] = {}
    for name in page_names:
        html = (snapshot_dir / aim_source.PAGES_DIR / name).read_text(encoding="utf-8")
        kind = aim_source.classify_page(name)
        if kind[0] == "chapter":
            chapters[kind[1]] = (name, parse_chapter_page(name, html))
        elif kind[0] == "section":
            parsed = parse_section_page(name, html, figure_hashes)
            sections[(kind[1], kind[2])] = (name, parsed)
        else:
            appendices[kind[1]] = (name, parse_appendix_page(name, html, figure_hashes))

    known_ids: set[str] = set()
    for chapter in chapters:
        known_ids.add(model.chapter_id(chapter))
    for (chapter, section), (_, parsed) in sections.items():
        known_ids.add(model.section_id(chapter, section))
        for para in parsed.paragraphs:
            if para["id"] in known_ids:
                raise ParseError(f"duplicate paragraph id {para['id']}")
            known_ids.add(para["id"])
    for appendix in appendices:
        known_ids.add(model.appendix_id(appendix))

    docs: dict[str, dict] = {}
    index_html = (snapshot_dir / index_rel).read_text(encoding="utf-8")
    publication = parse_index_page(aim_source.INDEX_PAGE, index_html, figure_hashes)
    publication["explicit_references"] = _resolve_refs(
        publication.pop("_refs"), known_ids, model.PUBLICATION_ID
    )
    publication["source"] = _doc_source(source, aim_source.INDEX_PAGE)
    docs["publication"] = _with_hash(publication)
    for chapter in sorted(chapters):
        chapter_page, toc = chapters[chapter]
        own = sorted(key for key in sections if key[0] == chapter)
        toc_pages = [entry.page for entry in toc.sections]
        own_pages = [sections[key][0] for key in own]
        if toc_pages != own_pages:
            raise ParseError(
                f"chapter {chapter} contents list sections {toc_pages} but the snapshot "
                f"holds {own_pages}"
            )
        section_docs: list[dict] = []
        for (_, section), (page_name, parsed) in ((key, sections[key]) for key in own):
            toc_entry = next(e for e in toc.sections if e.page == page_name)
            if toc_entry.heading != parsed.heading:
                raise ParseError(
                    f"{page_name}: chapter contents call this section {toc_entry.heading!r} "
                    f"but the page says {parsed.heading!r}"
                )
            toc_numbers = [n for n, _ in toc_entry.entries]
            own_numbers = [p["paragraph"] for p in parsed.paragraphs]
            if toc_numbers != own_numbers:
                raise ParseError(
                    f"{page_name}: chapter contents list paragraphs {toc_numbers} but the "
                    f"page holds {own_numbers}"
                )
            for (_, toc_heading), para in zip(toc_entry.entries, parsed.paragraphs, strict=True):
                if toc_heading != para["heading"]:
                    raise ParseError(
                        f"{page_name}: paragraph {para['paragraph']} heading {para['heading']!r} "
                        f"differs from chapter contents {toc_heading!r}"
                    )
            if parsed.chapter_heading is not None and parsed.chapter_heading != toc.heading:
                raise ParseError(
                    f"{page_name}: chapter heading {parsed.chapter_heading!r} differs from "
                    f"chapter page {toc.heading!r}"
                )
            built_words: list[str] = []
            _block_words(parsed.content, built_words)
            for para in parsed.paragraphs:
                built_words.extend(_words(f"{para['paragraph']}. {para['heading']}"))
                _block_words(para["content"], built_words)
            _verify_lossless(page_name, parsed.source_words, built_words)

            paragraph_docs = []
            for para in parsed.paragraphs:
                para["explicit_references"] = _resolve_refs(
                    para.pop("_refs"), known_ids, para["id"]
                )
                para["source"] = _doc_source(source, page_name, para["paragraph"])
                paragraph_docs.append(_with_hash(para))
            section_doc = {
                "id": model.section_id(chapter, section),
                "document_type": model.DOCUMENT_TYPE_SECTION,
                "chapter": chapter,
                "section": section,
                "heading": parsed.heading,
                "toc_label": toc_entry.label,
                "content": parsed.content,
                "explicit_references": _resolve_refs(
                    parsed.refs, known_ids, model.section_id(chapter, section)
                ),
                "paragraphs": paragraph_docs,
                "source": _doc_source(source, page_name),
            }
            section_docs.append(_with_hash(section_doc))
        chapter_doc = {
            "id": model.chapter_id(chapter),
            "document_type": model.DOCUMENT_TYPE_CHAPTER,
            "chapter": chapter,
            "heading": toc.heading,
            "sections": section_docs,
            "source": _doc_source(source, chapter_page),
        }
        docs[f"chapter-{chapter:02d}"] = _with_hash(chapter_doc)

    for appendix in sorted(appendices):
        page_name, parsed = appendices[appendix]
        built_words = []
        _block_words(parsed.content, built_words)
        _verify_lossless(page_name, parsed.source_words, built_words)
        appendix_doc = {
            "id": model.appendix_id(appendix),
            "document_type": model.DOCUMENT_TYPE_APPENDIX,
            "appendix": appendix,
            "heading": parsed.heading,
            "content": parsed.content,
            "explicit_references": _resolve_refs(
                parsed.refs, known_ids, model.appendix_id(appendix)
            ),
            "source": _doc_source(source, page_name),
        }
        docs[f"appendix-{appendix}"] = _with_hash(appendix_doc)

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


def count_paragraphs(doc: dict) -> int:
    if doc.get("document_type") != model.DOCUMENT_TYPE_CHAPTER:
        return 0
    return sum(len(section["paragraphs"]) for section in doc["sections"])


def count_sections(doc: dict) -> int:
    if doc.get("document_type") != model.DOCUMENT_TYPE_CHAPTER:
        return 0
    return len(doc["sections"])
