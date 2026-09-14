"""ACS parser: archived PDF snapshot → canonical ACS JSON (plan §39.2; Phase 10a).

The Airman Certification Standards are published only as a PDF. Text is
read with the pinned ``pypdf`` in **layout mode**, which preserves reading
order and keeps the task labels intact (plain mode re-orders a label's
first letter: ``eferences: R``). Layout mode has its own, smaller
artifacts, repaired by exactly three deterministic rules that never
change wording — runs of spaces are collapsed (justified text and table
cells), a kerning gap before a possessive apostrophe is closed
(``manufacturer ’s`` → ``manufacturer’s``), and a line-final hyphen joins
its continuation without a space (``pilot-in-`` / ``command``). Everything
else is grammar over lines:

- **Page furniture**: each page after the cover carries a page number
  (roman for the front matter, arabic for the body — both checked to be
  sequential) and, in the body, a running header naming the Area of
  Operation or Appendix the page belongs to (checked against the content
  actually parsed on that page). Nothing else is ever dropped.
- **Front matter**: the cover (document number, title, edition date,
  publisher), Foreword, Revision History (a three-column table), Major
  Enhancements (the ACS's own change note: bullets, some followed by a
  grid of element codes), Table of Contents (dot-leader entries, wrapped
  entries re-joined) and Introduction. An unrecognized front-matter
  section is a parse error, not a silent drop (§32.2).
- **Body**: ``Area of Operation N. Title`` → ``Task L. Title (classes)`` →
  ``References:`` / ``Objective:`` / ``Note:`` fields → ``Knowledge:``,
  ``Risk Management:``, ``Skills:`` sections, each a lead-in followed by
  coded elements ``PA.I.A.K1 text`` (sub-elements ``PA.I.B.K1a a. text``
  nest under their parent). Codes must agree with their area, task and
  section and number contiguously; ``[Archived]`` placeholders are kept.
- **Appendices**: generic prose blocks — headings, paragraphs, ``Note:``
  callouts, bullet lists, and *preformatted* blocks for the PDF's tables
  (lines with column gaps, kept verbatim rather than guessed at).

Gates before anything is returned: lossless capture (every word of every
surviving line lands in exactly one canonical field), contents
cross-check (the Table of Contents names exactly the areas, tasks and
appendix sections parsed), change-note cross-check (every code the Major
Enhancements list as added exists; every code listed as removed is absent
or an ``[Archived]`` placeholder, and vice versa) and unique ids.
``REQUIRE_COMPLETE`` relaxes the two cross-checks to the areas present
(test fixtures are page subsets of the real document).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdf
from pypdf import PdfReader

from far_aim.links import citations as cites
from far_aim.models import acs as model
from far_aim.sources import acs as acs_source

EXTRACTOR = f"pypdf/{pypdf.__version__}"
"""Recorded in every document's ``source`` block (provenance, excluded from
``canonical_hash``): a library upgrade that changes extracted text shows up
as a reviewed content change (plan §39.2)."""

REQUIRE_COMPLETE = True
"""Cross-check the Table of Contents and Major Enhancements against the whole
document. Tests parse page subsets and set this False, which restricts both
checks to the areas and appendices actually present."""

TERMINAL_PUNCTUATION = (".", ":", ";", "!", "?", ".)", ".”", '."')
PARAGRAPH_SHORT_LINE = 0.85
"""Prose is justified: every wrapped line runs to the block's full width, so a
line shorter than this share of the widest line ends a paragraph."""
PREFORMATTED_GAP = 6
"""A line with an internal run of this many spaces is table layout, not prose."""
PREFORMATTED_INDENT = 6
"""A block whose every line is indented this far is set off from the prose."""

_SPACE_RUN_RE = re.compile(r"[ \t\xa0  ]+")
_APOSTROPHE_GAP_RE = re.compile(r"(?<=\w) ’(?=s\b)")
_PAGE_NUMBER_RE = re.compile(r"^(?:(?P<arabic>\d+)|(?P<roman>[ivxl]+))$")
_RUNNING_HEADER_RE = re.compile(r"^(?:Area of Operation [IVX]+\.\s.+|Appendix \d+: .+)$")
_AREA_RE = re.compile(r"^Area of Operation (?P<roman>[IVX]+)\.\s+(?P<title>.+)$")
_TASK_RE = re.compile(r"^Task (?P<letter>[A-Z])\.\s+(?P<title>.+)$")
_APPENDIX_RE = re.compile(r"^Appendix (?P<number>\d+): (?P<title>.+)$")
_LABEL_RE = re.compile(r"^(?P<label>References|Objective|Note|Knowledge|Skills):\s*(?P<rest>.*)$")
_RISK_LABEL_RE = re.compile(r"^Risk Management:\s*(?P<rest>.*)$")
_ELEMENT_RE = re.compile(r"^(?P<code>[A-Z]{2,3}\.[IVX]+\.[A-Z]\.[KRS]\d+[a-z]?)\s+(?P<text>.*)$")
_SUB_MARKER_RE = re.compile(r"^(?P<marker>[a-z])\.\s+")
_CLASSES_RE = re.compile(
    r"\s*\((?P<classes>(?:ASEL|ASES|AMEL|AMES)(?:,\s*(?:ASEL|ASES|AMEL|AMES))*)\)$"
)
_MAJOR_ENHANCEMENTS_RE = re.compile(r"^Major Enhancements to (?P<number>FAA-S-ACS-\d+[A-Z]?)$")
_TOC_ENTRY_RE = re.compile(r"^(?P<title>.*?)\s*\.{2,}\s*(?P<page>\d+)$")
_TOC_GROUP_RE = re.compile(r"^(?:Introduction|Area of Operation [IVX]+\.\s.+|Appendix \d+: .+)$")
_REVISION_ROW_RE = re.compile(
    r"^(?P<number>FAA-S-[A-Z0-9-]+)\s{2,}(?P<description>.+?)\s{2,}"
    r"(?P<date>[A-Z][a-z]+(?: \d{1,2},)? \d{4})$"
)
_DOCUMENT_NUMBER_RE = re.compile(r"^FAA-S-ACS-\d+[A-Z]?$")
_EDITION_DATE_RE = re.compile(r"^(?P<month>[A-Z][a-z]+) (?P<year>\d{4})$")
_CODE_TOKEN_RE = re.compile(r"^[A-Z]{2,3}\.[IVX]+\.[A-Z]\.[KRS]\d+[a-z]?$")
_GAP_RE = re.compile(rf" {{{PREFORMATTED_GAP},}}")
_DOT_LEADER_RE = re.compile(r"\.{2,}")
_HANDBOOK_RE = re.compile(r"^FAA-[HPS]-\d{4}-\d+[A-Z]?$")
_AC_RE = re.compile(r"^AC \d+-\d+[A-Z]?$")
_CFR_PARTS_RE = re.compile(r"^14 CFR parts?\b")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
FRONT_MATTER_SECTIONS = ("Foreword", "Revision History", "Table of Contents", "Introduction")


class ParseError(ValueError):
    """The snapshot does not follow the ACS grammar; nothing is written."""


# ---------------------------------------------------------------------------
# Extraction and normalization
# ---------------------------------------------------------------------------


def normalize_line(raw: str) -> str:
    """Whitespace collapsed and the possessive kerning gap closed; wording untouched."""
    line = _SPACE_RUN_RE.sub(" ", raw).strip()
    return _APOSTROPHE_GAP_RE.sub("’", line)


def words(text: str) -> list[str]:
    """Whitespace tokens with dot leaders removed (the lossless-capture unit)."""
    return _DOT_LEADER_RE.sub(" ", text).split()


def line_words(texts: list[str]) -> list[str]:
    """Words of consecutive lines with the line-final-hyphen join applied, so
    the source tally counts ``pilot-in-command`` the way the documents do."""
    out: list[str] = []
    carry: str | None = None
    for text in texts:
        tokens = words(text)
        if not tokens:
            continue
        if carry is not None:
            tokens[0] = carry + tokens[0]
            carry = None
        if text.endswith("-"):
            carry = tokens.pop()
        out.extend(tokens)
    if carry is not None:
        out.append(carry)
    return out


def join_lines(lines: list[str]) -> str:
    """Wrapped lines re-joined: a line-final hyphen continues its word."""
    out = ""
    for line in lines:
        if not out:
            out = line
        elif out.endswith("-"):
            out += line
        else:
            out += " " + line
    return out


@dataclass
class Line:
    raw: str  # rstripped extraction output (column layout preserved)
    text: str  # normalized
    page: int  # 1-based PDF page


@dataclass
class Page:
    number: int
    lines: list[Line]
    page_label: str | None = None
    running_header: str | None = None


def extract_pages(reader: PdfReader) -> list[Page]:
    pages: list[Page] = []
    for index in range(len(reader.pages)):
        text = acs_source.page_text(reader, index)
        lines = [
            Line(raw=raw.rstrip(), text=normalize_line(raw), page=index + 1)
            for raw in text.split("\n")
        ]
        pages.append(Page(number=index + 1, lines=lines))
    return pages


def page_stream(pages: list[Page]) -> list[Line]:
    """The pages' lines as one stream, with a blank line at each page break
    unless the text plainly continues: a line-final hyphen, or an unfinished
    sentence picked up by a lowercase word on the next page."""
    stream: list[Line] = []
    for page in pages:
        if stream and page.lines:
            previous, following = stream[-1].text, page.lines[0].text
            continues = previous.endswith("-") or (
                not _ends_sentence(previous) and following[:1].islower()
            )
            if not continues:
                stream.append(Line(raw="", text="", page=page.number))
        stream.extend(page.lines)
    return stream


def strip_furniture(page: Page) -> None:
    """Drop the page number and running header, recording both for verification."""
    lines = page.lines
    while lines and not lines[-1].text:
        lines.pop()
    if lines and page.number > 1 and _PAGE_NUMBER_RE.match(lines[-1].text):
        page.page_label = lines.pop().text
    while lines and not lines[-1].text:
        lines.pop()
    while lines and not lines[0].text:
        lines.pop(0)
    if lines and page.number > 1 and _RUNNING_HEADER_RE.match(lines[0].text):
        page.running_header = lines.pop(0).text
    while lines and not lines[0].text:
        lines.pop(0)


def verify_page_labels(pages: list[Page]) -> None:
    """Cover unnumbered; then roman i, ii, … then arabic 1, 2, … each contiguous
    (merely increasing when parsing a page subset)."""
    last_roman = 0
    last_arabic = 0
    phase = "roman"
    for page in pages:
        if page.number == 1:
            if page.page_label is not None:
                raise ParseError(
                    f"page 1: the cover page carries a page number {page.page_label!r}"
                )
            continue
        label = page.page_label
        if label is None:
            raise ParseError(f"page {page.number}: no page number found at the bottom of the page")
        if phase == "roman" and label.isdigit():
            phase = "arabic"
        if phase == "roman":
            try:
                value = model.roman_to_int(label.upper())
            except ValueError as exc:
                raise ParseError(
                    f"page {page.number}: bad front-matter page number {label!r}"
                ) from exc
            expected, last_roman = last_roman + 1, value
        else:
            value = int(label)
            expected, last_arabic = last_arabic + 1, value
        if value != expected and (REQUIRE_COMPLETE or value < expected):
            raise ParseError(f"page {page.number}: page number {label!r}, expected {expected}")


# ---------------------------------------------------------------------------
# Generic prose blocks (front matter, appendices)
# ---------------------------------------------------------------------------


def _ends_sentence(text: str) -> bool:
    return text.endswith(TERMINAL_PUNCTUATION)


def _split_paragraphs(lines: list[Line]) -> list[str]:
    """Wrapped prose → paragraphs (see ``PARAGRAPH_SHORT_LINE``)."""
    widest = max(len(line.raw.strip()) for line in lines)
    paragraphs: list[str] = []
    current: list[str] = []
    for index, line in enumerate(lines):
        current.append(line.text)
        last = index == len(lines) - 1
        short = len(line.raw.strip()) < PARAGRAPH_SHORT_LINE * widest
        if last or (short and _ends_sentence(line.text)):
            paragraphs.append(join_lines(current))
            current = []
    return paragraphs


def _dedent(raw_lines: list[str]) -> list[str]:
    indent = min((_indent(raw) for raw in raw_lines if raw.strip()), default=0)
    return [raw[indent:].rstrip() if raw.strip() else "" for raw in raw_lines]


def parse_blocks(lines: list[Line], what: str) -> list[dict]:
    """Blank-line-separated runs → heading / text / note / list / preformatted blocks.

    Consecutive preformatted runs (a table whose rows layout mode separates
    with blank lines) merge into one block so the table stays whole.
    """
    runs: list[list[Line]] = []
    run: list[Line] = []
    for line in lines:
        if line.text:
            run.append(line)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    blocks: list[dict] = []
    table: list[str] = []

    def flush_table() -> None:
        if table:
            blocks.append({"type": "preformatted", "lines": _dedent(table)})
            table.clear()

    for run in runs:
        if _is_preformatted(run):
            if table:
                table.append("")
            table.extend(line.raw for line in run)
            continue
        flush_table()
        blocks.extend(_run_blocks(run, what))
    flush_table()
    return blocks


def _is_preformatted(run: list[Line]) -> bool:
    """Table layout (column gaps) or a block set off from the prose by indentation
    (a definition list, a task listing): kept line by line, never flowed."""
    if any(_GAP_RE.search(line.raw.strip()) for line in run):
        return True
    if any(line.text.startswith("•") for line in run):
        return False
    if _LABEL_RE.match(run[0].text) is not None:
        return False
    return all(_indent(line.raw) >= PREFORMATTED_INDENT for line in run)


def _indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _run_blocks(run: list[Line], what: str) -> list[dict]:
    first = run[0].text
    bullet_at = next((i for i, line in enumerate(run) if line.text.startswith("•")), None)
    if bullet_at is not None:
        blocks = _run_blocks(run[:bullet_at], what) if bullet_at else []
        items: list[list[str]] = []
        for line in run[bullet_at:]:
            if line.text.startswith("•"):
                items.append([line.text[1:].strip()])
            else:
                items[-1].append(line.text)
        blocks.append({"type": "list", "items": [{"text": join_lines(item)} for item in items]})
        return blocks
    note = _LABEL_RE.match(first)
    if note is not None and note.group("label") == "Note":
        body = [note.group("rest")] + [line.text for line in run[1:]]
        return [{"type": "note", "label": "Note", "text": join_lines([b for b in body if b])}]
    if not any(_ends_sentence(line.text) for line in run) and sum(len(x.text) for x in run) < 160:
        return [{"type": "heading", "text": join_lines([line.text for line in run])}]
    return [{"type": "text", "text": paragraph} for paragraph in _split_paragraphs(run)]


def block_words(blocks: list[dict], out: list[str]) -> None:
    for block in blocks:
        kind = block["type"]
        if kind in ("heading", "text"):
            out.extend(words(block["text"]))
        elif kind == "note":
            out.extend(words(f"{block['label']}:"))
            out.extend(words(block["text"]))
        elif kind == "list":
            for item in block["items"]:
                out.append("•")
                out.extend(words(item["text"]))
        elif kind == "preformatted":
            out.extend(line_words([normalize_line(raw) for raw in block["lines"]]))
        else:
            raise ParseError(f"unknown block type {kind!r} during verification")


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------


def parse_cover(lines: list[Line]) -> dict:
    texts = [line.text for line in lines if line.text]
    if not texts or _DOCUMENT_NUMBER_RE.match(texts[0]) is None:
        raise ParseError(f"cover page: expected the document number first, found {texts[:1]!r}")
    number = texts[0]
    title_lines: list[str] = []
    edition_date: str | None = None
    publisher: list[str] = []
    for text in texts[1:]:
        if edition_date is None:
            if _EDITION_DATE_RE.match(text) and text.split()[0] in _MONTHS:
                edition_date = text
            else:
                title_lines.append(text)
        else:
            publisher.append(text)
    if edition_date is None or not title_lines:
        raise ParseError("cover page: no edition date (Month YYYY) after the title")
    month = _MONTHS.index(edition_date.split()[0]) + 1
    return {
        "document_number": number,
        "title": " ".join(title_lines),
        "title_lines": title_lines,
        "edition_date": edition_date,
        "edition_month": f"{edition_date.split()[1]}-{month:02d}",
        "publisher": publisher,
    }


def _cover_words(cover: dict, out: list[str]) -> None:
    out.extend(words(cover["document_number"]))
    for line in cover["title_lines"]:
        out.extend(words(line))
    out.extend(words(cover["edition_date"]))
    for line in cover["publisher"]:
        out.extend(words(line))


def parse_revision_history(lines: list[Line]) -> dict:
    texts = [line for line in lines if line.text]
    if not texts:
        raise ParseError("Revision History: empty")
    header = texts[0]
    columns = [c for c in re.split(r"\s{2,}", header.raw.strip()) if c]
    if columns != ["Document #", "Description", "Date"]:
        raise ParseError(f"Revision History: unexpected columns {columns!r}")
    rows: list[dict] = []
    for line in texts[1:]:
        match = _REVISION_ROW_RE.match(line.raw.strip())
        if match is None:
            raise ParseError(f"Revision History: page {line.page}: unparseable row {line.text!r}")
        rows.append(
            {
                "document_number": match.group("number"),
                "description": normalize_line(match.group("description")),
                "date": match.group("date"),
            }
        )
    return {"columns": columns, "rows": rows}


def _revision_words(history: dict, out: list[str]) -> None:
    for column in history["columns"]:
        out.extend(words(column))
    for row in history["rows"]:
        out.extend(words(row["document_number"]))
        out.extend(words(row["description"]))
        out.extend(words(row["date"]))


def parse_changes(lines: list[Line], document_number: str) -> dict:
    """The Major Enhancements page: bullets, each with any code grid that follows it."""
    items: list[dict] = []
    for line in lines:
        if not line.text:
            continue
        tokens = line.text.split()
        if line.text.startswith("•"):
            items.append({"text": [line.text[1:].strip()], "codes": []})
        elif not items:
            raise ParseError(f"Major Enhancements: page {line.page}: text before the first bullet")
        elif all(_CODE_TOKEN_RE.match(token) for token in tokens):
            items[-1]["codes"].extend(tokens)
        elif items[-1]["codes"]:
            raise ParseError(
                f"Major Enhancements: page {line.page}: prose after a code grid: {line.text!r}"
            )
        else:
            items[-1]["text"].append(line.text)
    bullets = [{"text": join_lines(item["text"]), "codes": item["codes"]} for item in items]
    added: list[str] = []
    removed: list[str] = []
    for bullet in bullets:
        text = bullet["text"]
        if text.startswith("The following ACS codes have been added"):
            added.extend(bullet["codes"])
        elif text.startswith("The following ACS codes have been removed"):
            removed.extend(bullet["codes"])
        elif bullet["codes"]:
            raise ParseError(f"Major Enhancements: codes under an unrecognized bullet {text!r}")
    for name, codes in (("added", added), ("removed", removed)):
        if len(set(codes)) != len(codes):
            raise ParseError(f"Major Enhancements: duplicate {name} codes")
    return {
        "document_number": document_number,
        "bullets": bullets,
        "added": added,
        "removed": removed,
    }


def _changes_words(changes: dict, out: list[str]) -> None:
    out.extend(words(f"Major Enhancements to {changes['document_number']}"))
    for bullet in changes["bullets"]:
        out.append("•")
        out.extend(words(bullet["text"]))
        out.extend(bullet["codes"])


def parse_contents(lines: list[Line]) -> list[dict]:
    """Table of Contents → ``group`` and ``entry`` records (wrapped entries re-joined)."""
    entries: list[dict] = []
    pending: list[str] = []
    for line in lines:
        if not line.text:
            continue
        match = _TOC_ENTRY_RE.match(line.text)
        if match is not None:
            title = (
                join_lines([*pending, match.group("title")]) if pending else match.group("title")
            )
            pending = []
            entries.append(
                {"kind": "entry", "title": title.strip(), "page": int(match.group("page"))}
            )
        elif not pending and _TOC_GROUP_RE.match(line.text):
            entries.append({"kind": "group", "title": line.text})
        else:
            pending.append(line.text)
    if pending:
        raise ParseError(f"Table of Contents: dangling entry text {join_lines(pending)!r}")
    return entries


def _contents_words(entries: list[dict], out: list[str]) -> None:
    for entry in entries:
        out.extend(words(entry["title"]))
        if entry["kind"] == "entry":
            out.append(str(entry["page"]))


def split_front_matter(lines: list[Line]) -> tuple[dict[str, list[Line]], int]:
    """Front matter after the cover keyed by section heading, plus the body start.

    Sections follow in the document's fixed order. The Table of Contents
    lists ``Introduction`` and every ``Area of Operation`` as group lines,
    so the Introduction heading is the ``Introduction`` line whose next
    text line is *not* a dot-leader entry, and the body begins at the
    first Area of Operation heading after it.
    """
    sections: dict[str, list[Line]] = {}
    current: str | None = None
    texts = [line.text for line in lines]

    def next_text(index: int) -> str:
        for text in texts[index + 1 :]:
            if text:
                return text
        return ""

    for index, line in enumerate(lines):
        text = line.text
        key: str | None = None
        if text in FRONT_MATTER_SECTIONS or _MAJOR_ENHANCEMENTS_RE.match(text):
            key = "Major Enhancements" if _MAJOR_ENHANCEMENTS_RE.match(text) else text
            if (
                current == "Table of Contents"
                and key == "Introduction"
                and _TOC_ENTRY_RE.match(next_text(index))
            ):
                key = None  # the contents' own group line
        if key is not None:
            if key in sections:
                raise ParseError(f"page {line.page}: front-matter section {key!r} appears twice")
            current = key
            sections[key] = [line]
            continue
        if current == "Introduction" and _AREA_RE.match(text):
            missing = [
                s for s in (*FRONT_MATTER_SECTIONS, "Major Enhancements") if s not in sections
            ]
            if missing:
                raise ParseError(f"front matter is missing section(s) {missing}")
            return sections, index
        if current is None:
            if text:
                raise ParseError(
                    f"page {line.page}: front-matter text outside any section: {text!r}"
                )
            continue
        sections[current].append(line)
    raise ParseError("no Area of Operation follows the Introduction")


# ---------------------------------------------------------------------------
# Body: areas, tasks, elements
# ---------------------------------------------------------------------------


@dataclass
class _Element:
    code: str
    kind: str
    number: int
    sub: str
    lines: list[str]
    page: int


@dataclass
class _Section:
    kind: str  # knowledge | risk | skills
    label: str  # the label line(s) verbatim ("Risk Management:")
    lead_in: str
    elements: list[_Element] = field(default_factory=list)


@dataclass
class _Task:
    letter: str
    heading_lines: list[str]
    page: int
    references: list[str] = field(default_factory=list)
    objective: list[str] = field(default_factory=list)
    notes: list[list[str]] = field(default_factory=list)
    sections: list[_Section] = field(default_factory=list)
    target: list[str] | None = None  # where continuation lines go


@dataclass
class _Area:
    roman: str
    heading: str
    page: int
    tasks: list[_Task] = field(default_factory=list)


@dataclass
class _Appendix:
    number: int
    heading: str
    page: int
    lines: list[Line] = field(default_factory=list)


class _BodyParser:
    def __init__(self, acs_prefix: str | None = None) -> None:
        self.areas: list[_Area] = []
        self.appendices: list[_Appendix] = []
        self.prefix = acs_prefix
        self._pending_risk: Line | None = None

    # -- containers ---------------------------------------------------------

    @property
    def area(self) -> _Area | None:
        return self.areas[-1] if self.areas else None

    @property
    def task(self) -> _Task | None:
        area = self.area
        return area.tasks[-1] if area and area.tasks else None

    @property
    def appendix(self) -> _Appendix | None:
        return self.appendices[-1] if self.appendices else None

    def current_heading(self) -> str | None:
        if self.appendices:
            return self.appendices[-1].heading
        if self.areas:
            return self.areas[-1].heading
        return None

    def feed(self, line: Line) -> None:
        text = line.text
        if self.appendices:
            match = _APPENDIX_RE.match(text)
            if match is not None:
                self._start_appendix(match, line)
            else:
                self.appendices[-1].lines.append(line)
            return
        if not text:
            return
        match = _APPENDIX_RE.match(text)
        if match is not None:
            self._close_task(line)
            self._start_appendix(match, line)
            return
        match = _AREA_RE.match(text)
        if match is not None:
            self._close_task(line)
            self.areas.append(_Area(roman=match.group("roman"), heading=text, page=line.page))
            return
        if self.area is None:
            raise ParseError(
                f"page {line.page}: body text before the first Area of Operation: {text!r}"
            )
        match = _TASK_RE.match(text)
        if match is not None:
            self._close_task(line)
            self.area.tasks.append(
                _Task(letter=match.group("letter"), heading_lines=[text], page=line.page)
            )
            return
        task = self.task
        if task is None:
            raise ParseError(f"page {line.page}: text before the first Task of the area: {text!r}")
        if self._pending_risk is not None:
            match = re.match(r"^Management:\s*(?P<rest>.*)$", text)
            if match is None:
                raise ParseError(
                    f"page {line.page}: 'Risk' not followed by 'Management:' ({text!r})"
                )
            self._pending_risk = None
            self._start_section(task, "risk", "Risk Management:", match.group("rest"), line)
            return
        if text == "Risk":
            self._pending_risk = line
            return
        match = _RISK_LABEL_RE.match(text)
        if match is not None:
            self._start_section(task, "risk", "Risk Management:", match.group("rest"), line)
            return
        match = _LABEL_RE.match(text)
        if match is not None:
            label, rest = match.group("label"), match.group("rest")
            if label == "References":
                if task.references or task.sections:
                    raise ParseError(
                        f"page {line.page}: duplicate or misplaced References: in Task "
                        f"{task.letter}"
                    )
                task.references = [rest]
                task.target = task.references
            elif label == "Objective":
                if task.objective or task.sections:
                    raise ParseError(
                        f"page {line.page}: duplicate or misplaced Objective: in Task {task.letter}"
                    )
                task.objective = [rest]
                task.target = task.objective
            elif label == "Note":
                if task.sections:
                    raise ParseError(
                        f"page {line.page}: Note: after the element sections of Task {task.letter}"
                    )
                task.notes.append([rest])
                task.target = task.notes[-1]
            elif label == "Knowledge":
                self._start_section(task, "knowledge", "Knowledge:", rest, line)
            else:
                self._start_section(task, "skills", "Skills:", rest, line)
            return
        match = _ELEMENT_RE.match(text)
        if match is not None:
            self._add_element(task, match.group("code"), match.group("text"), line)
            return
        if task.target is None:
            if task.references or task.sections:
                raise ParseError(
                    f"page {line.page}: unrecognized line in Task {task.letter}: {text!r}"
                )
            task.heading_lines.append(text)  # a wrapped task title
            return
        task.target.append(text)

    def _start_appendix(self, match: re.Match, line: Line) -> None:
        self.appendices.append(
            _Appendix(number=int(match.group("number")), heading=line.text, page=line.page)
        )

    def _close_task(self, line: Line) -> None:
        task = self.task
        if task is None:
            return
        if not task.references or not task.objective:
            raise ParseError(
                f"page {line.page}: Task {task.letter} of Area {self.area.roman} ended without "  # type: ignore[union-attr]
                "References: and Objective:"
            )
        kinds = [section.kind for section in task.sections]
        if kinds != ["knowledge", "risk", "skills"]:
            raise ParseError(
                f"page {line.page}: Task {task.letter} of Area {self.area.roman} has sections "  # type: ignore[union-attr]
                f"{kinds}, expected Knowledge, Risk Management, Skills"
            )

    def _start_section(self, task: _Task, kind: str, label: str, lead_in: str, line: Line) -> None:
        if any(section.kind == kind for section in task.sections):
            raise ParseError(f"page {line.page}: duplicate {label} section in Task {task.letter}")
        task.sections.append(_Section(kind=kind, label=label, lead_in=lead_in))
        task.target = None

    def _add_element(self, task: _Task, code: str, text: str, line: Line) -> None:
        if not task.sections:
            raise ParseError(
                f"page {line.page}: element {code} before any section of Task {task.letter}"
            )
        section = task.sections[-1]
        parsed = model.ELEMENT_CODE_RE.match(code)
        assert parsed is not None
        prefix = parsed.group("acs")
        if self.prefix is None:
            self.prefix = prefix
        area = self.area
        assert area is not None
        if (
            prefix != self.prefix
            or parsed.group("area") != area.roman
            or parsed.group("task") != task.letter
        ):
            raise ParseError(
                f"page {line.page}: element {code} does not belong to Area {area.roman} "
                f"Task {task.letter}"
            )
        kind = model.ELEMENT_KINDS[parsed.group("kind")]
        if kind != section.kind:
            raise ParseError(
                f"page {line.page}: {kind} element {code} inside the {section.kind} section"
            )
        number, sub = int(parsed.group("number")), parsed.group("sub")
        previous = section.elements[-1] if section.elements else None
        if sub:
            parent = next((e for e in reversed(section.elements) if not e.sub), None)
            if parent is None or parent.number != number:
                raise ParseError(f"page {line.page}: sub-element {code} has no parent element")
            expected = "a" if previous is parent else chr(ord(previous.sub) + 1)  # type: ignore[union-attr]
            if previous is not parent and (
                previous is None or not previous.sub or previous.number != number
            ):
                raise ParseError(f"page {line.page}: sub-element {code} out of sequence")
            if sub != expected:
                raise ParseError(f"page {line.page}: sub-element {code}, expected …{expected}")
        else:
            expected_number = (previous.number if previous else 0) + 1
            if number != expected_number:
                raise ParseError(
                    f"page {line.page}: element {code} out of sequence (expected number "
                    f"{expected_number})"
                )
        section.elements.append(
            _Element(code=code, kind=kind, number=number, sub=sub, lines=[text], page=line.page)
        )
        task.target = section.elements[-1].lines


# ---------------------------------------------------------------------------
# Canonical documents
# ---------------------------------------------------------------------------


def _task_title(heading_lines: list[str], letter: str) -> tuple[str, list[str]]:
    heading = join_lines(heading_lines)
    match = _TASK_RE.match(heading)
    assert match is not None and match.group("letter") == letter
    title = match.group("title")
    classes: list[str] = []
    found = _CLASSES_RE.search(title)
    if found is not None:
        classes = [c.strip() for c in found.group("classes").split(",")]
    return title, classes


def parse_references(text: str) -> list[dict]:
    """``14 CFR parts 61, 68, 91; AC 68-1; FAA-H-8083-25`` → typed reference tokens.

    CFR parts resolve through the shared citation recognizer; everything
    else is kept as typed text (handbooks, advisory circulars, the AIM,
    charts, the POH/AFM) until those become sources.
    """
    references: list[dict] = []
    for group in (g.strip() for g in text.split(";")):
        if not group:
            continue
        if _CFR_PARTS_RE.match(group):
            parts = cites.extract_part_citations(group)
            if not parts:
                raise ParseError(f"References: unparseable CFR citation {group!r}")
            references.append({"kind": "cfr_parts", "text": group, "parts": parts})
            continue
        tokens = [t.strip() for t in group.split(",") if t.strip()]
        if tokens and all(_HANDBOOK_RE.match(t) for t in tokens):
            references.extend({"kind": "handbook", "text": t} for t in tokens)
        elif group == "AIM":
            references.append({"kind": "aim", "text": group})
        elif _AC_RE.match(group):
            references.append({"kind": "advisory_circular", "text": group})
        else:
            references.append({"kind": "other", "text": group})
    return references


def _element_doc(element: _Element, parent_code: str | None) -> dict:
    text = join_lines(element.lines)
    doc = {
        "code": element.code,
        "kind": element.kind,
        "number": element.number,
        "sub": element.sub or None,
        "text": text,
    }
    if element.sub:
        marker = _SUB_MARKER_RE.match(text)
        if marker is None or marker.group("marker") != element.sub:
            raise ParseError(
                f"sub-element {element.code} text does not start with '{element.sub}.'"
            )
        doc["parent"] = parent_code
    return doc


def _task_doc(area: _Area, task: _Task, prefix: str, source: dict, pdf_url: str) -> dict:
    title, classes = _task_title(task.heading_lines, task.letter)
    code = f"{prefix}.{area.roman}.{task.letter}"
    references_text = join_lines(task.references)
    sections: dict[str, dict] = {}
    for section in task.sections:
        elements: list[dict] = []
        parent: str | None = None
        for element in section.elements:
            if not element.sub:
                parent = element.code
            elements.append(_element_doc(element, parent))
        sections[section.kind] = {
            "label": section.label,
            "lead_in": section.lead_in,
            "elements": elements,
        }
    return {
        "id": model.task_id(prefix, area.roman, task.letter),
        "document_type": model.DOCUMENT_TYPE_TASK,
        "code": code,
        "area": area.roman,
        "letter": task.letter,
        "heading": join_lines(task.heading_lines),
        "title": title,
        "classes": classes,
        "references_text": references_text,
        "references": parse_references(references_text),
        "objective": join_lines(task.objective),
        "notes": [join_lines(note) for note in task.notes],
        "knowledge": sections["knowledge"],
        "risk": sections["risk"],
        "skills": sections["skills"],
        "page": task.page,
        "source": {**source, "url": acs_source.page_url(pdf_url, task.page)},
    }


def _task_words(task: dict, out: list[str]) -> None:
    out.extend(words(task["heading"]))
    out.extend(words("References:"))
    out.extend(words(task["references_text"]))
    out.extend(words("Objective:"))
    out.extend(words(task["objective"]))
    for note in task["notes"]:
        out.extend(words("Note:"))
        out.extend(words(note))
    for kind in ("knowledge", "risk", "skills"):
        section = task[kind]
        out.extend(words(section["label"]))
        out.extend(words(section["lead_in"]))
        for element in section["elements"]:
            out.append(element["code"])
            out.extend(words(element["text"]))


def _with_hash(doc: dict) -> dict:
    doc["canonical_hash"] = model.canonical_hash(doc)
    return doc


def _verify_lossless(source_words: list[str], built_words: list[str]) -> None:
    def counts(items: list[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in items:
            out[item] = out.get(item, 0) + 1
        return out

    source, built = counts(source_words), counts(built_words)
    if source == built:
        return
    missing = {w: n - built.get(w, 0) for w, n in source.items() if n > built.get(w, 0)}
    extra = {w: n - source.get(w, 0) for w, n in built.items() if n > source.get(w, 0)}
    raise ParseError(
        "lossless-capture check failed; "
        f"missing from documents: {dict(list(missing.items())[:8])}, "
        f"not in source: {dict(list(extra.items())[:8])}"
    )


def _verify_contents(entries: list[dict], areas: list[dict], appendices: list[dict]) -> None:
    """The Table of Contents must name exactly the areas, tasks and appendix sections parsed."""
    listed_areas: dict[str, list[str]] = {}
    listed_appendices: dict[str, list[str]] = {}
    current: list[str] | None = None
    for entry in entries:
        if entry["kind"] == "group":
            title = entry["title"]
            if title == "Introduction":
                current = []
            elif _AREA_RE.match(title):
                current = listed_areas.setdefault(title, [])
            else:
                current = listed_appendices.setdefault(title, [])
        elif current is not None:
            current.append(entry["title"])
    parsed_areas = {a["heading"]: [t["heading"] for t in a["tasks"]] for a in areas}
    parsed_appendices = {
        a["heading"]: [b["text"] for b in a["content"] if b["type"] == "heading"]
        for a in appendices
    }
    for label, listed, parsed in (
        ("Area of Operation", listed_areas, parsed_areas),
        ("Appendix", listed_appendices, parsed_appendices),
    ):
        if REQUIRE_COMPLETE and set(listed) != set(parsed):
            raise ParseError(
                f"Table of Contents lists {label}s {sorted(listed)} but the document holds "
                f"{sorted(parsed)}"
            )
        for heading, items in parsed.items():
            expected = listed.get(heading)
            if expected is None:
                raise ParseError(f"{label} {heading!r} is not listed in the Table of Contents")
            if label == "Area of Operation":
                if items != expected:
                    raise ParseError(
                        f"Table of Contents lists tasks {expected} for {heading!r} but the "
                        f"document holds {items}"
                    )
            else:
                missing = [e for e in expected if e not in items]
                if missing:
                    raise ParseError(
                        f"Table of Contents lists section(s) {missing} for {heading!r} that "
                        "the appendix does not head"
                    )


def _verify_changes(changes: dict, areas: list[dict]) -> None:
    """The Major Enhancements page is the ACS's own change note (plan §38.4)."""
    present: dict[str, str] = {}
    for area in areas:
        for task in area["tasks"]:
            for kind in ("knowledge", "risk", "skills"):
                for element in task[kind]["elements"]:
                    present[element["code"]] = element["text"]
    parsed_areas = {a["roman"] for a in areas}

    def in_scope(code: str) -> bool:
        match = model.ELEMENT_CODE_RE.match(code)
        return REQUIRE_COMPLETE or (match is not None and match.group("area") in parsed_areas)

    missing = [c for c in changes["added"] if in_scope(c) and c not in present]
    if missing:
        raise ParseError(
            f"Major Enhancements list added codes the document does not contain: {missing}"
        )
    live = [
        c
        for c in changes["removed"]
        if in_scope(c) and c in present and "[Archived]" not in present[c]
    ]
    if live:
        raise ParseError(f"Major Enhancements list removed codes that are still live: {live}")
    unannounced = sorted(
        code
        for code, text in present.items()
        if "[Archived]" in text and code not in changes["removed"]
    )
    if unannounced:
        raise ParseError(f"[Archived] placeholders not listed under removed codes: {unannounced}")


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


def build_acs_docs(snapshot_dir: Path, metadata: dict, source: dict) -> dict[str, dict]:
    """Parse the accepted snapshot into canonical documents keyed by output stem
    (``publication``, ``area-01`` … ``appendix-3``). Every gate must pass first."""
    pdf_path = snapshot_dir / metadata["file"]
    try:
        reader = acs_source.open_pdf(pdf_path.read_bytes(), "archived ACS PDF")
    except acs_source.FetchError as exc:
        raise ParseError(str(exc)) from exc
    pages = extract_pages(reader)
    for page in pages:
        strip_furniture(page)
    verify_page_labels(pages)
    source = {**source, "extractor": EXTRACTOR}
    pdf_url = source["url"]

    # Cover, then the front matter up to the first real Area of Operation.
    cover = parse_cover(pages[0].lines)
    if cover["document_number"] != metadata["document_number"]:
        raise ParseError(
            f"cover names {cover['document_number']}, the snapshot was accepted as "
            f"{metadata['document_number']}"
        )
    stream = page_stream(pages[1:])
    sections, body_start = split_front_matter(stream)

    foreword = parse_blocks(sections["Foreword"][1:], "Foreword")
    history = parse_revision_history(sections["Revision History"][1:])
    changes = parse_changes(sections["Major Enhancements"][1:], cover["document_number"])
    contents = parse_contents(sections["Table of Contents"][1:])
    introduction = parse_blocks(sections["Introduction"][1:], "Introduction")
    if history["rows"] and history["rows"][-1]["document_number"] != cover["document_number"]:
        raise ParseError(
            f"Revision History ends at {history['rows'][-1]['document_number']}, the cover says "
            f"{cover['document_number']}"
        )
    if changes["document_number"] != cover["document_number"]:
        raise ParseError("Major Enhancements name a different document than the cover")

    body = _BodyParser()
    for line in stream[body_start:]:
        body.feed(line)
    body._close_task(stream[-1])
    if not body.areas:
        raise ParseError("no Area of Operation parsed")
    prefix = body.prefix
    if prefix is None:
        raise ParseError("no element codes parsed; cannot determine the ACS code prefix")
    # Every page's running header must name the container its content joined.
    headings_by_page: dict[int, str] = {}
    for area in body.areas:
        headings_by_page[area.page] = area.heading
    for appendix in body.appendices:
        headings_by_page[appendix.page] = appendix.heading
    current: str | None = None
    for page in pages:
        current = headings_by_page.get(page.number, current)
        if page.running_header is not None and page.running_header != current:
            raise ParseError(
                f"page {page.number}: running header {page.running_header!r} but the page's "
                f"content belongs to {current!r}"
            )

    docs: dict[str, dict] = {}
    area_docs: list[dict] = []
    for area in body.areas:
        match = _AREA_RE.match(area.heading)
        assert match is not None
        tasks = [_with_hash(_task_doc(area, task, prefix, source, pdf_url)) for task in area.tasks]
        if not tasks:
            raise ParseError(f"Area of Operation {area.roman} has no tasks")
        doc = {
            "id": model.area_id(prefix, area.roman),
            "document_type": model.DOCUMENT_TYPE_AREA,
            "code": f"{prefix}.{area.roman}",
            "roman": area.roman,
            "number": model.roman_to_int(area.roman),
            "heading": area.heading,
            "title": match.group("title"),
            "tasks": tasks,
            "page": area.page,
            "source": {**source, "url": acs_source.page_url(pdf_url, area.page)},
        }
        area_docs.append(_with_hash(doc))
        docs[f"area-{doc['number']:02d}"] = doc
    numbers = [a["number"] for a in area_docs]
    if numbers != list(range(1, len(numbers) + 1)) and REQUIRE_COMPLETE:
        raise ParseError(f"Areas of Operation are not sequential: {numbers}")

    appendix_docs: list[dict] = []
    for appendix in body.appendices:
        match = _APPENDIX_RE.match(appendix.heading)
        assert match is not None
        doc = {
            "id": model.appendix_id(prefix, appendix.number),
            "document_type": model.DOCUMENT_TYPE_APPENDIX,
            "number": appendix.number,
            "heading": appendix.heading,
            "title": match.group("title"),
            "content": parse_blocks(appendix.lines, appendix.heading),
            "page": appendix.page,
            "source": {**source, "url": acs_source.page_url(pdf_url, appendix.page)},
        }
        appendix_docs.append(_with_hash(doc))
        docs[f"appendix-{appendix.number}"] = doc

    publication = {
        "id": model.publication_id(prefix),
        "document_type": model.DOCUMENT_TYPE_PUBLICATION,
        "acs_prefix": prefix,
        "document_number": cover["document_number"],
        "title": cover["title"],
        "edition_date": cover["edition_date"],
        "edition_month": cover["edition_month"],
        "cover": cover,
        "foreword": foreword,
        "revision_history": history,
        "changes": changes,
        "contents": contents,
        "introduction": introduction,
        "areas": [{"code": a["code"], "heading": a["heading"], "id": a["id"]} for a in area_docs],
        "appendices": [{"heading": a["heading"], "id": a["id"]} for a in appendix_docs],
        "source": {**source, "url": pdf_url},
    }
    docs["publication"] = _with_hash(publication)

    _verify_contents(contents, area_docs, appendix_docs)
    _verify_changes(changes, area_docs)
    source_words = line_words([line.text for page in pages for line in page.lines])
    built: list[str] = []
    _cover_words(cover, built)
    for name, blocks in (("Foreword", foreword), ("Introduction", introduction)):
        built.extend(words(name))
        block_words(blocks, built)
    built.extend(words("Revision History"))
    _revision_words(history, built)
    _changes_words(changes, built)
    built.extend(words("Table of Contents"))
    _contents_words(contents, built)
    for area in area_docs:
        built.extend(words(area["heading"]))
        for task in area["tasks"]:
            _task_words(task, built)
    for appendix in appendix_docs:
        built.extend(words(appendix["heading"]))
        block_words(appendix["content"], built)
    _verify_lossless(source_words, built)
    _verify_unique_ids(docs)
    return docs


def count_tasks(doc: dict) -> int:
    if doc.get("document_type") != model.DOCUMENT_TYPE_AREA:
        return 0
    return len(doc["tasks"])


def count_elements(doc: dict) -> int:
    if doc.get("document_type") != model.DOCUMENT_TYPE_AREA:
        return 0
    return sum(
        len(task[kind]["elements"])
        for task in doc["tasks"]
        for kind in ("knowledge", "risk", "skills")
    )


def iter_tasks(docs: dict[str, dict]):
    """Task documents of the layer in area/task order."""
    for key in sorted(docs):
        doc = docs[key]
        if doc.get("document_type") == model.DOCUMENT_TYPE_AREA:
            yield from doc["tasks"]
