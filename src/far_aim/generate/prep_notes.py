"""Exam-prep rendering (plan §39.4): the study callout on covered notes, the
ACS ``Where to study`` section, and the generated ``vault/Prep/`` notes.

Everything here renders the curator's own words from ``ppl-study.json`` and
``acs-map.json`` (``links.study``) and is labelled as such; official text
is never restated, only linked. Generated under ``vault/Prep/Private
Pilot/`` (a generator-owned root, unlike the reader's ``Study/``):

    Private Pilot Prep.md   # entry point + coverage report
    Part 61 Map.md          # subparts, ranges, the studied sections with gists
    Part 91 Map.md
    Numbers Sheet.md        # every quoted threshold, linked to its paragraph
    Where Do I Look.md      # plain-English questions → citations, by ACS Task
"""

from __future__ import annotations

from dataclasses import dataclass, field

from far_aim.generate import BuildError, acs_notes, naming
from far_aim.generate.frontmatter import Value
from far_aim.generate.markdown import escape_md
from far_aim.generate.notes import Note, link_display
from far_aim.links.study import (
    ACS_MAP_SOURCE,
    STUDY_SOURCE,
    AcsMap,
    Coverage,
    StudyEntry,
    StudyGuide,
)
from far_aim.models import acs as acs_model
from far_aim.models import cfr as cfr_model

PREP_DIR = "Prep"
PREP_FOLDER = "Private Pilot"
PREP_INDEX_STEM = "Private Pilot Prep"
NUMBERS_STEM = "Numbers Sheet"
LOOKUP_STEM = "Where Do I Look"
MAP_PARTS = ("61", "91")
STUDY_CALLOUT = "study"
"""Obsidian callout type of the study aid; styled by the committed CSS snippet."""


def part_map_stem(part: str) -> str:
    return f"Part {part} Map"


@dataclass(frozen=True)
class StudyContext:
    """Verified curated data plus the link targets the renderers need."""

    guide: StudyGuide | None
    acs_map: AcsMap | None
    coverage: tuple[Coverage, ...] = ()
    # stem → display text for every linkable generated note (FAR sections
    # and parts, AIM paragraphs, PCG terms, concepts).
    targets: dict[str, str] = field(default_factory=dict)
    element_codes: frozenset[str] = frozenset()
    # ACS task code → (area roman, area title, task title); area roman → title.
    tasks: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    areas: dict[str, str] = field(default_factory=dict)


def _cell(text: str) -> str:
    """A table cell: Markdown-escaped, pipes escaped."""
    return escape_md(text).replace("|", "\\|")


def link(stem: str, targets: dict[str, str]) -> str:
    display = targets.get(stem)
    if display is None:
        raise BuildError(f"study data links {stem!r}, which no generated note defines")
    return f"[[{stem}]]" if display == stem else f"[[{stem}|{link_display(display)}]]"


def code_link(code: str, element_codes: frozenset[str]) -> str:
    """An ACS element code as a block link to its element (plain when unknown)."""
    if code in element_codes:
        task = acs_notes.task_code_of(code)
        return f"[[{acs_notes.task_stem(task)}#^{acs_notes.element_block_id(code)}|{code}]]"
    return f"`{code}`"


def _callout(kind: str, title: str, lines: list[str]) -> str:
    out = [f"> [!{kind}] {title}"]
    for line in lines:
        out.extend(f"> {part}" if part else ">" for part in line.split("\n"))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Study callout (FAR section and AIM paragraph notes)
# ---------------------------------------------------------------------------


def study_callout(entry: StudyEntry, context: StudyContext) -> str:
    lines = [f"**Gist:** {escape_md(entry.gist)}", f"**Why it matters:** {escape_md(entry.why)}"]
    if entry.numbers:
        lines.append("**Numbers:** " + " · ".join(escape_md(n.value) for n in entry.numbers))
    for trap in entry.traps:
        lines.append(f"**Watch out:** {escape_md(trap)}")
    for mnemonic in entry.mnemonics:
        lines.append(f"**Mnemonic (training-community, not FAA):** {escape_md(mnemonic)}")
    tail = [f"**Stage:** {escape_md(entry.stage)}"]
    if entry.acs:
        tail.insert(
            0, "**ACS:** " + " · ".join(code_link(c, context.element_codes) for c in entry.acs)
        )
    lines.append(" — ".join(tail))
    return _callout(STUDY_CALLOUT, "Private Pilot study aid — curated, not official text", lines)


# ---------------------------------------------------------------------------
# ACS task notes: ## Where to study (curated)
# ---------------------------------------------------------------------------


def _entry_links(entry, targets: dict[str, str]) -> str:
    if entry.out_of_corpus:
        return f"not in the FAR/AIM: {escape_md(entry.out_of_corpus)}"
    return ", ".join(link(stem, targets) for stem in entry.stems)


def where_to_study_chunks(task: dict, acs_map: AcsMap, targets: dict[str, str]) -> list[str]:
    """One line for the Task's default entry, then one per element with its own entry."""
    items: list[str] = []
    default = acs_map.tasks.get(task["code"])
    if default is not None:
        items.append(f"- **All Knowledge and Risk elements** — {_entry_links(default, targets)}")
    for kind in ("knowledge", "risk", "skills"):
        for element in task[kind]["elements"]:
            entry = acs_map.elements.get(element["code"])
            if entry is None:
                continue
            items.append(f"- **{element['code']}** — {_entry_links(entry, targets)}")
    if not items:
        return []
    return [
        "## Where to study (curated)",
        _callout(
            "info",
            "Curated map",
            [
                f"From `{ACS_MAP_SOURCE}` (plan §39.3): where the vault covers each Knowledge and "
                "Risk element, or why it cannot. A study aid, not FAA text; sub-elements follow "
                "their parent unless listed."
            ],
        ),
        "\n".join(items),
    ]


# ---------------------------------------------------------------------------
# Prep notes
# ---------------------------------------------------------------------------


def _prep_note(stem: str, note_id: str, title: str, chunks: list[str]) -> Note:
    frontmatter: list[tuple[str, Value]] = [
        ("id", note_id),
        ("type", "prep"),
        ("generated", True),
        ("title", title),
        ("tags", ["prep", "study", "private-pilot"]),
    ]
    return Note(
        kind="prep",
        path_parts=(PREP_DIR, PREP_FOLDER, f"{stem}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def _prep_callout() -> str:
    return _callout(
        "info",
        "Curated study layer",
        [
            f"Rendered from `{STUDY_SOURCE}` and `{ACS_MAP_SOURCE}` (plan §39.4). Every line is "
            "a study aid in the curator's words; the official text is one click away and wins."
        ],
    )


def _area_of(entry: StudyEntry) -> str:
    """The ACS Area an entry files under: its first code's Area, else General."""
    for code in entry.acs:
        match = acs_model.ELEMENT_CODE_RE.match(code)
        if match is not None:
            return match.group("area")
    return ""


def _area_order(roman: str) -> int:
    return acs_model.roman_to_int(roman) if roman else 99


def _entry_link(entry: StudyEntry, context: StudyContext) -> str:
    return link(entry.stem, context.targets)


def build_prep_index(context: StudyContext, present_parts: list[str]) -> Note:
    guide = context.guide
    assert guide is not None
    chunks = [
        f"# {PREP_INDEX_STEM}",
        _prep_callout(),
        "Study material for the Private Pilot knowledge test and the oral portion of the "
        "practical test, organized the way both are: by the "
        f"[[{acs_notes.ACS_INDEX_STEM}|Airman Certification Standards]]. Paste a code from a "
        "knowledge-test report into search to reach the element; every covered regulation "
        "and AIM paragraph opens with a labelled study aid.",
        "## Notes in this folder",
        "\n".join(
            [
                *(
                    f"- [[{part_map_stem(p)}]] — the subparts of Part {p}, and where the studied "
                    "sections sit in them"
                    for p in present_parts
                ),
                f"- [[{NUMBERS_STEM}]] — every tested number, each linked to the paragraph it "
                "comes from",
                f"- [[{LOOKUP_STEM}]] — plain-English questions and the citation that answers "
                "each, by ACS Area and Task",
            ]
        ),
    ]
    if context.coverage:
        rows = [
            "| Area of Operation | K/R elements | Mapped | Not in the FAR/AIM |",
            "| --- | ---: | ---: | ---: |",
        ]
        totals = [0, 0, 0]
        for c in context.coverage:
            rows.append(
                f"| [[{acs_notes.area_stem('PA.' + c.roman)}|{c.roman}. {escape_md(c.title)}]] | "
                f"{c.elements} | {c.mapped} | {c.out_of_corpus} |"
            )
            totals[0] += c.elements
            totals[1] += c.mapped
            totals[2] += c.out_of_corpus
        rows.append(f"| **Total** | **{totals[0]}** | **{totals[1]}** | **{totals[2]}** |")
        chunks.extend(
            [
                "## Coverage",
                "Every Knowledge and Risk element of the ACS is either mapped to notes in this "
                "vault or marked as living outside the FAR/AIM (the FAA handbooks, the POH). "
                "The build fails on an element that is neither, so this table is the honest "
                "statement of what the vault can and cannot teach.",
                "\n".join(rows),
            ]
        )
    by_stage = guide.by_stage()
    stage_lines = [
        f"- **{escape_md(stage)}** — {escape_md(guide.stages[stage])} ({len(entries)} "
        f"{'entry' if len(entries) == 1 else 'entries'})"
        for stage, entries in by_stage.items()
    ]
    reviewed = sum(1 for e in guide.entries.values() if e.review == "reviewed")
    chunks.extend(
        [
            "## Study entries",
            f"{len(guide.entries)} covered sections and paragraphs, {reviewed} reviewed by a human "
            f"and {len(guide.entries) - reviewed} still unreviewed drafts. Every quoted number has "
            "been checked verbatim against the official text by the build; the wording of the "
            "gists has not.",
            "\n".join(stage_lines),
        ]
    )
    for stage, entries in by_stage.items():
        if not entries:
            continue
        items = [
            f"- {_entry_link(e, context)} — {escape_md(e.gist)}"
            for e in sorted(entries, key=lambda e: naming.natural_key(e.stem))
        ]
        chunks.extend([f"### {escape_md(stage)}", "\n".join(items)])
    return _prep_note(PREP_INDEX_STEM, "prep-private-pilot", PREP_INDEX_STEM, chunks)


def _subparts(part_doc: dict) -> list[tuple[str, str, list[dict]]]:
    """(label, heading, sections) for each subpart; sections outside any subpart
    collect under an empty label."""
    out: list[tuple[str, str, list[dict]]] = []
    loose: list[dict] = []

    def sections_of(children: list[dict]) -> list[dict]:
        found: list[dict] = []
        for child in children:
            if child.get("document_type") == cfr_model.DOCUMENT_TYPE_SECTION:
                found.append(child)
            elif child.get("type") == "subject_group":
                found.extend(sections_of(child["sections"]))
            elif child.get("type") == "subpart":
                found.extend(sections_of(child["children"]))
        return found

    for child in part_doc["children"]:
        if child.get("type") == "subpart":
            out.append(
                (
                    child.get("label") or "",
                    child.get("heading") or "",
                    sections_of(child["children"]),
                )
            )
        elif child.get("document_type") == cfr_model.DOCUMENT_TYPE_SECTION:
            loose.append(child)
        elif child.get("type") == "subject_group":
            loose.extend(sections_of(child["sections"]))
    if loose:
        out.insert(0, ("", "Sections not in a subpart", loose))
    return out


def build_part_map(part: str, part_doc: dict, context: StudyContext) -> Note:
    guide = context.guide
    assert guide is not None
    stem = part_map_stem(part)
    heading = part_doc["heading"]
    chunks = [
        f"# {stem}",
        _prep_callout(),
        f"[[{naming.part_index_stem(part)}|Part {part} — {escape_md(heading)}]] by subpart: the "
        "number pattern is how pilots navigate the FAR (Part 61 subpart E is the private pilot "
        "subpart; Part 91 subpart B is the flight rules). Studied sections carry their gist.",
    ]
    for label, sub_heading, sections in _subparts(part_doc):
        if not sections:
            continue
        first, last = sections[0]["section"], sections[-1]["section"]
        studied = [s for s in sections if s["section"] in guide.entries]
        title = f"Subpart {label} — {escape_md(sub_heading)}" if label else escape_md(sub_heading)
        rng = f"§ {first} – § {last}" if first != last else f"§ {first}"
        chunks.append(f"## {title}")
        chunks.append(
            f"{rng} ({len(sections)} sections)"
            + (f" · **{len(studied)} studied**" if studied else " · nothing studied here yet")
        )
        if studied:
            chunks.append(
                "\n".join(
                    f"- [[{s['section']}|§ {s['section']}]] — "
                    f"{escape_md(guide.entries[s['section']].gist)}"
                    for s in studied
                )
            )
    return _prep_note(stem, f"prep-part-{part}-map", stem, chunks)


def build_numbers_sheet(context: StudyContext) -> Note:
    guide = context.guide
    assert guide is not None
    chunks = [
        f"# {NUMBERS_STEM}",
        _prep_callout(),
        "Every number the study entries quote, grouped by ACS Area of Operation. The quote "
        "column is verbatim official text (checked by the build); the link opens the paragraph.",
    ]
    groups: dict[str, list[tuple[StudyEntry, object]]] = {}
    for entry in guide.entries.values():
        for number in entry.numbers:
            groups.setdefault(_area_of(entry), []).append((entry, number))
    for roman in sorted(groups, key=_area_order):
        title = (
            f"Area of Operation {roman} — {escape_md(context.areas.get(roman, ''))}"
            if roman
            else "General"
        )
        rows = ["| Number | Verbatim | Where |", "| --- | --- | --- |"]
        for entry, number in sorted(
            groups[roman], key=lambda pair: naming.natural_key(pair[0].stem)
        ):
            where = link(entry.stem, context.targets)
            if number.where:
                where += f" {escape_md(number.where)}"
            rows.append(f"| **{escape_md(number.value)}** | {_cell(number.quote)} | {where} |")
        chunks.extend([f"## {title}", "\n".join(rows)])
    return _prep_note(NUMBERS_STEM, "prep-numbers-sheet", NUMBERS_STEM, chunks)


def build_lookup(context: StudyContext) -> Note:
    guide = context.guide
    assert guide is not None
    chunks = [
        f"# {LOOKUP_STEM}",
        _prep_callout(),
        "Topic → citation: the question you would ask, the short answer, and where the rule "
        "lives. Grouped by the ACS Task the entry names; then every covered citation with its "
        "gist (citation → meaning).",
    ]
    by_task: dict[str, list[tuple[StudyEntry, object]]] = {}
    for entry in guide.entries.values():
        tasks = sorted({acs_notes.task_code_of(c) for c in entry.acs}) or [""]
        for task in tasks:
            for question in entry.questions:
                by_task.setdefault(task, []).append((entry, question))

    def task_order(code: str) -> tuple[int, str]:
        if not code:
            return (99, "")
        _, roman, letter = code.split(".")
        return (acs_model.roman_to_int(roman), letter)

    current_area = None
    for task in sorted(by_task, key=task_order):
        if task:
            roman, area_title, task_title = context.tasks[task]
            if roman != current_area:
                chunks.append(f"## Area of Operation {roman} — {escape_md(area_title)}")
                current_area = roman
            chunks.append(f"### [[{acs_notes.task_stem(task)}|{task} — {escape_md(task_title)}]]")
        else:
            chunks.append("## General")
        rows = ["| Question | Answer | Look in |", "| --- | --- | --- |"]
        for _entry, question in by_task[task]:
            cites = ", ".join(link(c, context.targets) for c in question.cite)
            rows.append(f"| {_cell(question.q)} | {_cell(question.a)} | {cites} |")
        chunks.append("\n".join(rows))
    rows = ["| Citation | Gist |", "| --- | --- |"]
    for entry in sorted(
        guide.entries.values(), key=lambda e: (e.is_aim, naming.natural_key(e.stem))
    ):
        rows.append(f"| {link(entry.stem, context.targets)} | {_cell(entry.gist)} |")
    chunks.extend(["## By citation", "\n".join(rows)])
    return _prep_note(LOOKUP_STEM, "prep-where-do-i-look", LOOKUP_STEM, chunks)


def prep_stems(present_parts: list[str]) -> list[str]:
    return [PREP_INDEX_STEM, *(part_map_stem(p) for p in present_parts), NUMBERS_STEM, LOOKUP_STEM]


def build_prep_notes(context: StudyContext, far_docs: dict[str, dict]) -> list[Note]:
    present = [p for p in MAP_PARTS if p in far_docs]
    notes = [build_prep_index(context, present)]
    notes.extend(build_part_map(p, far_docs[p], context) for p in present)
    notes.append(build_numbers_sheet(context))
    notes.append(build_lookup(context))
    return notes
