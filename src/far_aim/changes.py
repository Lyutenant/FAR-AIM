"""Change gates (plan §38): the change ledger, mass-change thresholds (§17.5)
and the AIM change-note cross-check (§17.7).

`far-aim diff` runs between ``parse`` and ``build-vault``, when the vault on
disk still reflects the previously published versions and the in-memory
plan reflects the freshly parsed layers. The **ledger** pairs the two by
stable citation (the file name) and classifies every *document* note —
FAR sections and appendices, AIM sections, paragraphs and appendices, PCG
terms — by its frontmatter ``canonical_hash``: bytes that differ under an
equal hash are a provenance-only rewrite (an eCFR issue bump with no
amendment), a differing hash is a content change, and a removed note whose
``## Official Text`` reappears byte-identical under another citation is a
**move** (an AIM renumbering cascade, plan §14.2), never a removal.

The vault is the "before" side because CI runs on a fresh checkout with no
local canonical layers; the committed vault is the compiled output of the
accepted layers and its hashes come straight from canonical JSON
(plan §32.4). Index notes carry aggregate hashes and stay outside the
ledger; so do Home, Source Status, concept notes and figure assets.

Gates never write anything. A tripped gate makes ``diff`` fail, which
stops ``update`` before ``build-vault`` with the last known-good vault
intact (plan §32.13); each gate has an explicit CLI override except the
empty-output condition.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from far_aim.generate import aim_notes as generate_aim_notes
from far_aim.generate import notes as generate_notes
from far_aim.generate import pcg_notes as generate_pcg_notes
from far_aim.models import cfr as cfr_model
from far_aim.sources import ecfr as ecfr_source
from far_aim.sources.common import sha256_of

FAR = "FAR"
AIM = "AIM"
PCG = "PCG"
CORPORA = (FAR, AIM, PCG)

_CORPUS_DIRS = {
    FAR: generate_notes.FAR_DIR,
    AIM: generate_aim_notes.AIM_DIR,
    PCG: generate_pcg_notes.PCG_DIR,
}
_INDEX_NOTES = {
    FAR: (generate_notes.FAR_DIR, f"{generate_notes.TITLE_INDEX_STEM}.md"),
    AIM: (generate_aim_notes.AIM_DIR, f"{generate_aim_notes.AIM_INDEX_STEM}.md"),
    PCG: (generate_pcg_notes.PCG_DIR, f"{generate_pcg_notes.PCG_INDEX_STEM}.md"),
}
# Frontmatter ``type`` values of the notes the ledger covers (plan §38.2) —
# the values the notes carry, which differ from the generator's internal
# schema kind names (a PCG term note is ``type: "glossary"``).
DOCUMENT_KINDS = {
    FAR: frozenset({"regulation", "appendix"}),
    AIM: frozenset({"aim", "aim_section", "aim_appendix"}),
    PCG: frozenset({"glossary"}),
}

OFFICIAL_TEXT_HEADING = "## Official Text"
_LIST_LIMIT = 20


# ---------------------------------------------------------------------------
# Reading generated notes
# ---------------------------------------------------------------------------


def read_frontmatter(data: bytes) -> dict[str, object] | None:
    """The frontmatter of a generated note as a dict, or None if absent/malformed.

    Understands exactly the value space ``generate.frontmatter`` emits:
    JSON-quoted strings, ints, bools and lists of quoted strings.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return None
    result: dict[str, object] = {}
    current_list: list[str] | None = None
    for line in lines[1:]:
        if line == "---":
            return result
        if line.startswith("  - ") and current_list is not None:
            try:
                item = json.loads(line[4:])
            except ValueError:
                return None
            if not isinstance(item, str):
                return None
            current_list.append(item)
            continue
        current_list = None
        key, sep, raw = line.partition(":")
        if not sep or not key or key != key.strip():
            return None
        raw = raw.strip()
        if raw == "":
            current_list = []
            result[key] = current_list
        elif raw in ("true", "false"):
            result[key] = raw == "true"
        elif raw.startswith('"'):
            try:
                result[key] = json.loads(raw)
            except ValueError:
                return None
        elif re.fullmatch(r"-?\d+", raw):
            result[key] = int(raw)
        else:
            return None
    return None  # unterminated block


def official_text(data: bytes) -> bytes | None:
    """The bytes of a note's ``## Official Text`` section (heading excluded).

    Runs from the heading to the next ``## `` heading or the end of the
    note. None when the note renders no official text.
    """
    heading = OFFICIAL_TEXT_HEADING.encode("utf-8")
    start = _line_index(data, heading)
    if start is None:
        return None
    body_start = start + len(heading)
    rest = data[body_start:]
    match = re.search(rb"(?m)^## ", rest)
    return rest[: match.start()] if match else rest


def _line_index(data: bytes, line: bytes) -> int | None:
    match = re.search(rb"(?m)^" + re.escape(line) + rb"$", data)
    return match.start() if match else None


@dataclass(frozen=True)
class NoteRecord:
    """One document note on one side of the comparison."""

    path: Path
    frontmatter: dict[str, object]
    official_text: bytes | None

    @property
    def kind(self) -> str:
        return str(self.frontmatter.get("type"))

    @property
    def canonical_hash(self) -> str | None:
        value = self.frontmatter.get("canonical_hash")
        return value if isinstance(value, str) else None

    @property
    def citation(self) -> str:
        for key in ("citation", "term"):
            value = self.frontmatter.get(key)
            if isinstance(value, str):
                return value
        return self.path.stem

    def locations(self) -> frozenset[tuple[str, str]]:
        """AIM locations this note answers to: its paragraph, its section, its appendix."""
        fm = self.frontmatter
        found: set[tuple[str, str]] = set()
        if self.kind == "aim":
            found.add(("paragraph", str(fm.get("paragraph"))))
        if self.kind in ("aim", "aim_section"):
            found.add(("section", f"{fm.get('chapter')}-{fm.get('section')}"))
        if self.kind == "aim_appendix":
            found.add(("appendix", str(fm.get("appendix"))))
        return frozenset(found)


def _record(path: Path, data: bytes, kinds: frozenset[str]) -> NoteRecord | None:
    fm = read_frontmatter(data)
    if fm is None or fm.get("generated") is not True or fm.get("type") not in kinds:
        return None
    return NoteRecord(path=path, frontmatter=fm, official_text=official_text(data))


def _edition(fm: dict[str, object] | None) -> str | None:
    """The version a corpus index note pins (issue date, or effective date + change)."""
    if fm is None:
        return None
    if isinstance(fm.get("source_version"), str):
        return str(fm["source_version"])
    effective, change = fm.get("effective_date"), fm.get("change")
    if isinstance(effective, str) and isinstance(change, int):
        return f"{effective}-change-{change}"
    return None


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    before: NoteRecord
    after: NoteRecord


@dataclass
class Ledger:
    """How one corpus's planned document notes relate to the published ones."""

    corpus: str
    published: int
    planned: int
    index_before: dict[str, object] | None
    index_after: dict[str, object] | None
    unchanged: list[NoteRecord] = field(default_factory=list)
    provenance_only: list[NoteRecord] = field(default_factory=list)
    content_changed: list[NoteRecord] = field(default_factory=list)
    added: list[NoteRecord] = field(default_factory=list)
    removed: list[NoteRecord] = field(default_factory=list)
    moved: list[Move] = field(default_factory=list)

    @property
    def edition_before(self) -> str | None:
        return _edition(self.index_before)

    @property
    def edition_after(self) -> str | None:
        return _edition(self.index_after)

    @property
    def edition_changed(self) -> bool:
        return (
            self.edition_before is not None
            and self.edition_after is not None
            and self.edition_before != self.edition_after
        )

    @property
    def any_content_change(self) -> bool:
        return bool(self.content_changed or self.added or self.removed or self.moved)

    def after_records(self) -> list[NoteRecord]:
        return [
            *self.unchanged,
            *self.provenance_only,
            *self.content_changed,
            *self.added,
            *(m.after for m in self.moved),
        ]

    def totals(self) -> str:
        return (
            f"content-changed {len(self.content_changed):,}   "
            f"provenance-only {len(self.provenance_only):,}   "
            f"added {len(self.added):,}   removed {len(self.removed):,}   "
            f"moved {len(self.moved):,}"
        )

    def summary(self) -> str:
        return ", ".join(
            f"{label} {n:,}"
            for label, n in (
                ("content-changed", len(self.content_changed)),
                ("provenance-only", len(self.provenance_only)),
                ("added", len(self.added)),
                ("removed", len(self.removed)),
                ("moved", len(self.moved)),
            )
        )


def build_ledgers(vault_dir: Path, planned: dict[Path, bytes]) -> dict[str, Ledger]:
    """One ledger per corpus that has published document notes on disk.

    A corpus with nothing generated on disk (first build, or a corpus being
    added) gets no ledger and therefore no gates: there is nothing to
    protect yet.
    """
    ledgers: dict[str, Ledger] = {}
    for corpus in CORPORA:
        kinds = DOCUMENT_KINDS[corpus]
        root = vault_dir / _CORPUS_DIRS[corpus]
        on_disk: dict[Path, NoteRecord] = {}
        if root.is_dir():
            for path in sorted(root.rglob("*.md")):
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                record = _record(path, data, kinds)
                if record is not None:
                    on_disk[path] = record
        if not on_disk:
            continue
        after: dict[Path, NoteRecord] = {}
        for path, data in planned.items():
            if path.suffix != ".md" or root not in path.parents:
                continue
            record = _record(path, data, kinds)
            if record is not None:
                after[path] = record
        index_path = vault_dir.joinpath(*_INDEX_NOTES[corpus])
        index_before: dict[str, object] | None = None
        try:
            index_before = read_frontmatter(index_path.read_bytes())
        except OSError:
            index_before = None
        index_after = read_frontmatter(planned[index_path]) if index_path in planned else None
        ledger = Ledger(
            corpus=corpus,
            published=len(on_disk),
            planned=len(after),
            index_before=index_before,
            index_after=index_after,
        )
        for path in sorted(set(on_disk) | set(after)):
            before, now = on_disk.get(path), after.get(path)
            if before is None:
                ledger.added.append(now)  # type: ignore[arg-type]
            elif now is None:
                ledger.removed.append(before)
            elif planned[path] == before.path.read_bytes():
                ledger.unchanged.append(now)
            elif before.canonical_hash == now.canonical_hash:
                ledger.provenance_only.append(now)
            else:
                ledger.content_changed.append(now)
        _pair_moves(ledger)
        ledgers[corpus] = ledger
    return ledgers


def _pair_moves(ledger: Ledger) -> None:
    """Greedy move pairing in citation order: identical official text, each note once."""
    unused_added = list(ledger.added)
    still_removed: list[NoteRecord] = []
    for before in ledger.removed:
        match = None
        if before.official_text is not None:
            for candidate in unused_added:
                if candidate.official_text == before.official_text:
                    match = candidate
                    break
        if match is None:
            still_removed.append(before)
        else:
            unused_added.remove(match)
            ledger.moved.append(Move(before=before, after=match))
    ledger.removed = still_removed
    ledger.added = unused_added


# ---------------------------------------------------------------------------
# Mass-change thresholds (plan §38.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Threshold:
    """Trips when a count exceeds ``max(floor, share × published)``."""

    share: float
    floor: int

    def limit(self, published: int) -> float:
        return max(float(self.floor), self.share * published)

    def exceeded(self, count: int, published: int) -> bool:
        return count > self.limit(published)

    def describe(self, published: int) -> str:
        return (
            f"{self.limit(published):,.0f} = max(floor {self.floor}, "
            f"{self.share:.0%} of {published:,})"
        )


@dataclass(frozen=True)
class CorpusThresholds:
    removed: Threshold
    content_changed: Threshold
    added: Threshold


# Data behind the numbers: plan §38.3 (eCFR versioner history 2025-06 →
# 2026-09: median 3 amended documents per issue, p90 ≈ 44, outliers 207 and
# 610, largest removal 15; AIM Change 3: six announced paragraphs). AIM and
# PCG values are provisional until the first real edition calibrates them.
THRESHOLDS: dict[str, CorpusThresholds] = {
    FAR: CorpusThresholds(Threshold(0.01, 25), Threshold(0.05, 100), Threshold(0.05, 100)),
    AIM: CorpusThresholds(Threshold(0.05, 10), Threshold(0.10, 25), Threshold(0.10, 25)),
    PCG: CorpusThresholds(Threshold(0.02, 15), Threshold(0.10, 50), Threshold(0.10, 50)),
}


def empty_output_defect(ledger: Ledger) -> str | None:
    """A previously published corpus that plans no notes at all (never overridable)."""
    if ledger.published > 0 and ledger.planned == 0:
        return (
            f"{ledger.corpus}: {ledger.published:,} published notes but the parsed layer "
            "plans none — an empty parse is never published (plan §25.1)"
        )
    return None


def mass_change_defects(
    ledger: Ledger,
    thresholds: CorpusThresholds,
    *,
    announced: set[Path] | None = None,
) -> list[str]:
    """Threshold and provenance-only-edition defects for one corpus.

    ``announced`` — paths of content-changed or removed notes an
    announcement source explains; when given, only the *unannounced*
    changes and removals count.
    """
    defects: list[str] = []
    prefix = "unannounced " if announced is not None else ""
    counted = [r for r in ledger.content_changed if announced is None or r.path not in announced]
    removed = [r for r in ledger.removed if announced is None or r.path not in announced]
    for name, count, threshold in (
        (f"{prefix}removed", len(removed), thresholds.removed),
        (f"{prefix}content-changed", len(counted), thresholds.content_changed),
        ("added", len(ledger.added), thresholds.added),
    ):
        if threshold.exceeded(count, ledger.published):
            defects.append(
                f"{ledger.corpus}: {count:,} {name} notes, above "
                f"{threshold.describe(ledger.published)}"
            )
    if ledger.corpus != FAR and ledger.edition_changed and not ledger.any_content_change:
        defects.append(
            f"{ledger.corpus}: edition {ledger.edition_before} → {ledger.edition_after} "
            "changes no note's content — a snapshot assembled from stale pages, not a "
            "new edition"
        )
    return defects


# ---------------------------------------------------------------------------
# AIM Explanation of Changes (plan §38.4)
# ---------------------------------------------------------------------------

Location = tuple[str, str]  # ("paragraph", "5-2-9") | ("section", "4-3") | ("appendix", "3")


@dataclass(frozen=True)
class ChangeNote:
    """What chapter 0's Explanation of Changes claims, as recognized structure."""

    effective_date: str | None
    announced: tuple[str, ...]  # paragraph numbers named in entry titles
    mentioned: tuple[Location, ...]  # locations named in explanation text
    categories: tuple[str, ...]  # entry titles without a citation


_DASHES_RE = re.compile("[‐‑‒–—―]")
_EFFECTIVE_RE = re.compile(
    r"^Effective:\s*(?:(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})"
    r"|(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{2,4}))\s*$"
)
_ENTRY_RE = re.compile(r"^(?P<letter>[a-z])\.\s+(?P<title>.+)$", re.DOTALL)
_TITLE_CITATION_RE = re.compile(r"^(?P<paragraph>\d{1,2}-\d{1,2}-\d{1,3})\.\s*(?P<heading>.*)$")
_FIG_TBL_RE = re.compile(r"\b(?:FIG|TBL)\s+(?P<chapter>\d{1,2})-(?P<section>\d{1,2})-\d{1,3}\b")
_APPENDIX_RE = re.compile(r"\bAppendix\s+(?P<n>\d{1,2})\b")
_CHAPTER_SECTION_RE = re.compile(
    r"\bChapter\s+(?P<chapter>\d{1,2}),\s*Section\s+(?P<section>\d{1,2})\b"
)
_PARAGRAPH_RE = re.compile(r"(?<![\d-])(?P<paragraph>\d{1,2}-\d{1,2}-\d{1,3})(?![\d-])")
_MONTHS = {
    name: i
    for i, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"),
        start=1,
    )
}


def _effective_date(text: str) -> str | None:
    match = _EFFECTIVE_RE.match(text)
    if match is None:
        return None
    try:
        if match.group("month"):
            month = _MONTHS.get(match.group("month").lower())
            if month is None:
                return None
            return date(int(match.group("year")), month, int(match.group("day"))).isoformat()
        year = int(match.group("y"))
        if year < 100:
            year += 2000
        return date(year, int(match.group("m")), int(match.group("d"))).isoformat()
    except ValueError:
        return None


def _mentions(text: str) -> list[Location]:
    found: list[Location] = []
    for m in _FIG_TBL_RE.finditer(text):
        found.append(("section", f"{int(m.group('chapter'))}-{int(m.group('section'))}"))
    stripped = _FIG_TBL_RE.sub(" ", text)
    for m in _APPENDIX_RE.finditer(stripped):
        found.append(("appendix", str(int(m.group("n")))))
    for m in _CHAPTER_SECTION_RE.finditer(stripped):
        found.append(("section", f"{int(m.group('chapter'))}-{int(m.group('section'))}"))
    for m in _PARAGRAPH_RE.finditer(stripped):
        found.append(("paragraph", m.group("paragraph")))
    return found


def read_aim_change_note(section: dict) -> ChangeNote | str:
    """Recognize chapter 0's Explanation of Changes; a defect string when off-grammar.

    Recognition only: the official text is read, never rewritten (dash
    normalization applies to the copy being matched).
    """
    texts = [
        _DASHES_RE.sub("-", str(block.get("text", "")))
        for block in section.get("content", [])
        if block.get("type") == "text"
    ]
    effective: str | None = None
    announced: list[str] = []
    mentioned: list[Location] = []
    categories: list[str] = []
    entries = 0
    for text in texts:
        if effective is None and text.startswith("Effective:"):
            effective = _effective_date(text)
            if effective is None:
                return f"unparseable effective date line {text!r}"
            continue
        entry = _ENTRY_RE.match(text)
        if entry is not None:
            entries += 1
            cited = False
            for line in entry.group("title").split("\n"):
                line = line.strip()
                if not line:
                    continue
                citation = _TITLE_CITATION_RE.match(line)
                if citation is not None:
                    cited = True
                    if citation.group("paragraph") not in announced:
                        announced.append(citation.group("paragraph"))
            if not cited:
                categories.append(entry.group("title").strip())
            continue
        for location in _mentions(text):
            if location not in mentioned:
                mentioned.append(location)
    if effective is None:
        first = texts[0] if texts else ""
        return f"no 'Effective:' line found (first text block: {first!r})"
    if entries == 0:
        return "no lettered change entries ('a. …') found"
    return ChangeNote(
        effective_date=effective,
        announced=tuple(announced),
        mentioned=tuple(mentioned),
        categories=tuple(categories),
    )


def find_aim_change_note_section(aim_docs: dict[str, dict]) -> dict | None:
    """Chapter 0's section 0 document (the Explanation of Changes), if the layer has it."""
    chapter = aim_docs.get("chapter-00")
    if not isinstance(chapter, dict):
        return None
    for section in chapter.get("sections", []):
        if section.get("section") == 0:
            return section
    return None


@dataclass
class CrossCheck:
    """Outcome of comparing the AIM change note with the AIM ledger."""

    defects: list[str] = field(default_factory=list)
    report: list[str] = field(default_factory=list)
    explained: set[Path] = field(default_factory=set)  # content-changed notes the note covers


def _matches(record: NoteRecord, wanted: set[Location]) -> bool:
    return bool(record.locations() & wanted)


def cross_check_aim(note: ChangeNote, ledger: Ledger) -> CrossCheck:
    """Verdicts A1–A3, A5 and A6 of plan §38.4 (A4 is the threshold on unexplained changes)."""
    result = CrossCheck()
    edition_effective = None
    if ledger.index_after is not None and isinstance(ledger.index_after.get("effective_date"), str):
        edition_effective = str(ledger.index_after["effective_date"])
    if note.effective_date != edition_effective:
        result.defects.append(
            f"AIM: the Explanation of Changes is effective {note.effective_date} but the "
            f"accepted edition is effective {edition_effective} — the change note is stale, so "
            "the snapshot mixes editions"
        )
    after_locations: set[Location] = set()
    for record in ledger.after_records():
        after_locations |= record.locations()
    gone_locations: set[Location] = set()
    for record in ledger.removed:
        gone_locations |= record.locations()
    for move in ledger.moved:
        gone_locations |= move.before.locations()
    changed: list[NoteRecord] = [
        *ledger.content_changed,
        *ledger.added,
        *(m.after for m in ledger.moved),
    ]
    changed_locations: set[Location] = set(gone_locations)
    for record in changed:
        changed_locations |= record.locations()

    missing = [
        p
        for p in note.announced
        if ("paragraph", p) not in after_locations and ("paragraph", p) not in gone_locations
    ]
    if missing:
        result.defects.append(
            "AIM: the Explanation of Changes announces paragraph(s) the parsed layer does not "
            f"have and never had: {', '.join(missing)} (plan §32.2)"
        )
    announced_changed = [p for p in note.announced if ("paragraph", p) in changed_locations]
    if note.announced and not announced_changed:
        result.defects.append(
            "AIM: none of the announced paragraphs "
            f"({', '.join(note.announced)}) shows a canonical change — the edition's changes "
            "were not captured"
        )

    # A claimed paragraph explains its section note too (the section's
    # canonical hash covers its paragraphs), and the Explanation of Changes
    # section itself changes with every edition by definition.
    explicit: set[Location] = {("paragraph", p) for p in note.announced} | set(note.mentioned)
    claimed = explicit | {
        ("section", ref.rsplit("-", 1)[0]) for kind, ref in explicit if kind == "paragraph"
    }
    claimed.add(("section", "0-0"))
    quiet = sorted(
        f"{kind} {ref}"
        for kind, ref in explicit
        if (kind, ref) not in changed_locations
    )
    unexplained = [r for r in ledger.content_changed if not _matches(r, claimed)]
    result.explained = {r.path for r in ledger.content_changed if _matches(r, claimed)}
    result.report.append(
        f"  Explanation of Changes (effective {note.effective_date}): "
        f"{len(note.announced)} announced paragraph(s), {len(note.mentioned)} mention(s), "
        f"{len(note.categories)} categor{'y' if len(note.categories) == 1 else 'ies'}"
    )
    result.report.append(
        "  announced or mentioned but unchanged: " + (", ".join(quiet) if quiet else "none")
    )
    unexplained_added = [r for r in ledger.added if not _matches(r, claimed)]
    unexplained_moved = [m for m in ledger.moved if not _matches(m.after, claimed)]
    result.report.append(
        f"  content changes neither announced nor mentioned: {len(unexplained):,}"
        + (f" ({_listed(r.citation for r in unexplained)})" if unexplained else "")
    )
    if unexplained_added or unexplained_moved:
        result.report.append(
            f"  additions/moves neither announced nor mentioned: "
            f"{len(unexplained_added):,} added, {len(unexplained_moved):,} moved"
        )
    return result


def _listed(items) -> str:
    """Comma-joined items, capped at ``_LIST_LIMIT`` with a count of the rest."""
    listed = list(items)
    shown = ", ".join(listed[:_LIST_LIMIT])
    more = f", … {len(listed) - _LIST_LIMIT} more" if len(listed) > _LIST_LIMIT else ""
    return f"{shown}{more}"


# ---------------------------------------------------------------------------
# FAR — eCFR amendment index (plan §38.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AmendmentIndex:
    """What the eCFR says changed between two issue dates, as stable ids."""

    since: str
    until: str
    amended: frozenset[str]  # stable ids with a new version in the window
    removed: frozenset[str]  # stable ids the eCFR reports removed

    @property
    def announced(self) -> frozenset[str]:
        return self.amended | self.removed


def _entry_stable_id(entry: dict) -> str:
    """The stable id (``models.cfr``) an amendment-index entry names."""
    part, identifier = entry["part"], entry["identifier"]
    if entry["type"] == "appendix":
        return cfr_model.appendix_id(ecfr_source.TITLE_NUMBER, part, identifier)
    return cfr_model.section_id(ecfr_source.TITLE_NUMBER, part, identifier)


def read_amendment_index(path: Path) -> AmendmentIndex | str:
    """Load one archived ``versions-since-<date>.json``; a defect string if malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"amendment index {path} is unreadable: {exc}"
    entries = data.get("content_versions") if isinstance(data, dict) else None
    since, until = (data.get(k) if isinstance(data, dict) else None for k in ("since", "until"))
    if not isinstance(entries, list) or not isinstance(since, str) or not isinstance(until, str):
        return f"amendment index {path} has no since/until/content_versions"
    amended: set[str] = set()
    removed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(k), str) for k in ("identifier", "type", "part")
        ):
            return f"amendment index {path} holds a malformed entry: {entry!r}"
        stable = _entry_stable_id(entry)
        (removed if entry.get("removed") is True else amended).add(stable)
    return AmendmentIndex(
        since=since, until=until, amended=frozenset(amended), removed=frozenset(removed)
    )


MAX_INDEX_HOPS = 12


def amendment_chain(raw_dir: Path, before: str, after: str) -> AmendmentIndex | None | str:
    """The archived amendment indexes joining the published issue to the new one.

    Each accepted eCFR snapshot archives the index covering the previously
    accepted issue → itself (``metadata.json`` records the file and its
    checksum). Normally one hop suffices; several accepted-but-unpublished
    issues (a gate that tripped on consecutive days, resolved locally) are
    walked back hop by hop, the latest verdict per id winning. None when
    the chain does not reach ``before`` — the vault predates what the local
    cache can explain — and a defect string when an archived file does not
    match its recorded checksum (never silently ignored).
    """
    hops: list[AmendmentIndex] = []
    current = after
    for _ in range(MAX_INDEX_HOPS):
        record = ecfr_source.recorded_amendment_index(
            raw_dir / "ecfr" / current / "metadata.json"
        )
        if record is None:
            return None
        path = raw_dir / "ecfr" / current / record["file"]
        try:
            actual = sha256_of(path)
        except OSError:
            return None
        if actual != record["sha256"]:
            return f"archived amendment index {path} does not match its recorded checksum"
        index = read_amendment_index(path)
        if isinstance(index, str):
            return index
        hops.append(index)
        if index.since == before:
            break
        if index.since >= current:
            return None
        current = index.since
    else:
        return None
    verdict: dict[str, bool] = {}  # stable id → removed?
    for index in reversed(hops):  # chronological order; the latest hop wins
        for stable in index.amended:
            verdict[stable] = False
        for stable in index.removed:
            verdict[stable] = True
    return AmendmentIndex(
        since=before,
        until=after,
        amended=frozenset(k for k, gone in verdict.items() if not gone),
        removed=frozenset(k for k, gone in verdict.items() if gone),
    )


def _stable_id(record: NoteRecord) -> str:
    value = record.frontmatter.get("id")
    return value if isinstance(value, str) else ""


def cross_check_far(index: AmendmentIndex, ledger: Ledger) -> CrossCheck:
    """Verdicts F1, F2 and F4 of plan §38.4 (F3 is the threshold on unexplained changes)."""
    result = CrossCheck()
    after_ids = {_stable_id(r) for r in ledger.after_records()}
    gone_ids = {_stable_id(r) for r in ledger.removed} | {
        _stable_id(m.before) for m in ledger.moved
    }
    changed_ids = (
        {_stable_id(r) for r in ledger.content_changed}
        | {_stable_id(r) for r in ledger.added}
        | {_stable_id(m.after) for m in ledger.moved}
        | gone_ids
    )
    missing = sorted(s for s in index.announced if s not in after_ids and s not in gone_ids)
    if missing:
        result.defects.append(
            "FAR: the eCFR amendment index names content the parsed layer does not have "
            f"and never had: {_listed(missing)} (plan §32.2)"
        )
    if index.announced and not (index.announced & changed_ids):
        result.defects.append(
            f"FAR: none of the {len(index.announced):,} documents the eCFR amendment index "
            f"names ({index.since} → {index.until}) shows a canonical change — the full-title "
            "XML lags the amendment index"
        )
    quiet = sorted(s for s in index.announced if s not in changed_ids)
    still_present = sorted(s for s in index.removed if s in after_ids)
    result.explained = {
        r.path
        for r in (*ledger.content_changed, *ledger.removed)
        if _stable_id(r) in index.announced
    }
    unexplained = [r for r in ledger.content_changed if _stable_id(r) not in index.announced]
    unexplained_removed = [r for r in ledger.removed if _stable_id(r) not in index.announced]
    result.report.append(
        f"  eCFR amendment index ({index.since} → {index.until}): "
        f"{len(index.amended):,} amended, {len(index.removed):,} removed"
    )
    result.report.append(
        "  announced but unchanged: " + (_listed(quiet) if quiet else "none")
    )
    if still_present:
        result.report.append(f"  announced removed but still present: {_listed(still_present)}")
    result.report.append(
        f"  content changes not in the amendment index: {len(unexplained):,}"
        + (f" ({_listed(r.citation for r in unexplained)})" if unexplained else "")
    )
    if unexplained_removed:
        result.report.append(
            f"  removals not in the amendment index: {len(unexplained_removed):,} "
            f"({_listed(r.citation for r in unexplained_removed)})"
        )
    return result


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class GateReport:
    """Everything ``diff`` prints and decides about the change gates."""

    lines: list[str] = field(default_factory=list)  # ledger + cross-check report
    fatal: list[str] = field(default_factory=list)  # never overridable
    mass: list[str] = field(default_factory=list)  # --accept-mass-change
    change_note: list[str] = field(default_factory=list)  # --accept-change-note-mismatch
    summaries: dict[str, str] = field(default_factory=dict)  # corpus → totals line


def evaluate(
    ledgers: dict[str, Ledger],
    *,
    aim_docs: dict[str, dict] | None,
    far_index: AmendmentIndex | None | str = None,
    thresholds: dict[str, CorpusThresholds] | None = None,
) -> GateReport:
    """Run every gate over the ledgers (plan §38.3–§38.4).

    ``far_index`` — the eCFR amendment index joining the published FAR issue
    to the planned one (:func:`amendment_chain`): None when none is
    archived, a defect string when the archive is corrupt.
    """
    thresholds = THRESHOLDS if thresholds is None else thresholds
    report = GateReport()
    for corpus in CORPORA:
        ledger = ledgers.get(corpus)
        if ledger is None:
            continue
        before = ledger.edition_before or "unknown version"
        report.lines.append(
            f"changes ({corpus}, vs published {before}): "
            f"{ledger.published:,} notes → {ledger.planned:,} planned"
        )
        report.lines.append(f"  {ledger.totals()}")
        if ledger.moved:
            pairs = (f"{m.before.citation} → {m.after.citation}" for m in ledger.moved)
            report.lines.append(f"  moved: {_listed(pairs)}")
        report.summaries[corpus] = ledger.summary()

        fatal = empty_output_defect(ledger)
        if fatal is not None:
            report.fatal.append(fatal)
            continue

        explained: set[Path] | None = None
        if corpus == FAR and ledger.edition_changed:
            if isinstance(far_index, str):
                report.change_note.append(f"FAR: {far_index}")
            elif far_index is None:
                report.lines.append(
                    f"  eCFR amendment index: none archived for {ledger.edition_before} → "
                    f"{ledger.edition_after}; thresholds count every change"
                )
            else:
                outcome = cross_check_far(far_index, ledger)
                report.lines.extend(outcome.report)
                report.change_note.extend(outcome.defects)
                explained = outcome.explained
        if corpus == AIM and ledger.edition_changed:
            section = find_aim_change_note_section(aim_docs or {})
            if section is None:
                report.change_note.append(
                    "AIM: the parsed layer has no chapter 0 Explanation of Changes to cross-check"
                )
            else:
                note = read_aim_change_note(section)
                if isinstance(note, str):
                    report.change_note.append(
                        f"AIM: chapter 0 does not follow the Explanation of Changes grammar: {note}"
                    )
                else:
                    outcome = cross_check_aim(note, ledger)
                    report.lines.extend(outcome.report)
                    report.change_note.extend(outcome.defects)
                    explained = outcome.explained
        report.mass.extend(mass_change_defects(ledger, thresholds[corpus], announced=explained))
    return report
