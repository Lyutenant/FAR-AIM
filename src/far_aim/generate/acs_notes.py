"""ACS note builders: canonical ACS documents → vault notes (plan §39.1–§39.2).

Layout under ``vault/ACS/Private Pilot Airplane/``:

    ACS Private Pilot Airplane.md   # publication index: edition, foreword,
                                    #   revision history, change note, areas
    PA.I.md … PA.XII.md             # one index per Area of Operation
    PA.I.A.md … PA.XII.B.md         # one note per Task, elements verbatim
    ACS Appendix 1.md …             # appendices as prose/preformatted blocks

Notes are named by **code only** (plan §9): ``PA.I.A`` is the stem and the
citation; the Task title is the heading and the ``title`` property but
never an alias, because titles such as "Pilot Qualifications" or "Night
Operations" already name curated collection and concept notes (§39.1).
Every element line begins with its code verbatim and carries the block id
``^pa-i-a-k1`` (Obsidian block ids allow only letters, digits and dashes),
so a search for ``PA.I.A.K1`` lands on the element and a block link can
target it. Task ``References`` render verbatim, then as links for the CFR
parts (and the AIM) the vault holds; handbooks and advisory circulars stay
text until they are sources.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from far_aim.generate import BuildError, naming
from far_aim.generate.frontmatter import Value
from far_aim.generate.hierarchy import TEXT_CSS_CLASS, list_item
from far_aim.generate.markdown import escape_md
from far_aim.generate.notes import Note, link_display
from far_aim.links import citations as cites
from far_aim.links.glossary import GlossaryIndex
from far_aim.models import acs as acs_model

ACS_DIR = "ACS"
ACS_FOLDER = "Private Pilot Airplane"
ACS_INDEX_STEM = "ACS Private Pilot Airplane"
ACS_CITATION = "ACS Private Pilot Airplane"
APPENDIX_STEM_PREFIX = "ACS Appendix"

# id → (stem, display text) for every linkable ACS note.
Targets = dict[str, tuple[str, str]]


def area_stem(code: str) -> str:
    """``PA.I`` verbatim."""
    return code


def task_stem(code: str) -> str:
    """``PA.I.A`` verbatim."""
    return code


def appendix_stem(number: int) -> str:
    return f"{APPENDIX_STEM_PREFIX} {number}"


def element_block_id(code: str) -> str:
    """``PA.I.A.K1`` → ``pa-i-a-k1``."""
    return code.lower().replace(".", "-")


def task_code_of(element_code: str) -> str:
    """``PA.I.B.K1a`` → ``PA.I.B``."""
    match = acs_model.ELEMENT_CODE_RE.match(element_code)
    if match is None:
        raise BuildError(f"not an ACS element code: {element_code!r}")
    return f"{match.group('acs')}.{match.group('area')}.{match.group('task')}"


def area_display(area: dict) -> str:
    return f"Area of Operation {area['roman']} — {area['title']}"


def task_display(task: dict) -> str:
    return f"Task {task['letter']}. {task['title']}"


def appendix_display(appendix: dict) -> str:
    return appendix["heading"]


@dataclass(frozen=True)
class FarTargets:
    """The FAR part numbers whose index notes exist (references link to them)."""

    parts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GlossaryLinks:
    """The compiled PCG matcher and the PCG note targets it links to."""

    index: GlossaryIndex
    targets: Targets


@dataclass(frozen=True)
class AcsContext:
    """Everything a task note links to beyond its own document."""

    publication: dict
    far: FarTargets = FarTargets()
    aim_index_stem: str | None = None
    glossary: GlossaryLinks | None = None
    # Phase 10b (plan §39.3): the curated element map and the stem → display
    # targets its links resolve against; rendered by ``prep_notes``.
    study_map: object | None = None
    study_targets: dict[str, str] | None = None


def edition_text(source: dict, publication: dict) -> str:
    """``Private Pilot for Airplane Category (FAA-S-ACS-6C), November 2023, effective
    2024-05-31``."""
    return (
        f"{source['edition_label']}, {publication['edition_date']}, "
        f"effective {source['effective_date']}"
    )


def _source_callout(source: dict, publication: dict) -> str:
    return "\n".join(
        [
            "> [!info] Source",
            f"> FAA Airman Certification Standards, {edition_text(source, publication)} — "
            f"[view on FAA]({source['url']})",
        ]
    )


def _frontmatter_common(source: dict, canonical_hash: str) -> list[tuple[str, Value]]:
    return [
        ("source", "faa"),
        ("source_version", source["source_version"]),
        ("effective_date", source["effective_date"]),
        ("canonical_hash", canonical_hash),
        ("generated", True),
    ]


def _note_callout(text: str) -> str:
    lines = ["> [!note] Note"]
    lines.extend(f"> {line}" if line else ">" for line in escape_md(text).split("\n"))
    return "\n".join(lines)


def _reference_links(task: dict, context: AcsContext) -> list[str]:
    links: list[str] = []
    for reference in task["references"]:
        kind = reference["kind"]
        if kind == "cfr_parts":
            for part in cites.resolve_parts(reference["parts"], set(context.far.parts)):
                links.append(f"[[{naming.part_index_stem(part)}]]")
        elif kind == "aim" and context.aim_index_stem:
            links.append(f"[[{context.aim_index_stem}]]")
    seen: set[str] = set()
    return [link for link in links if not (link in seen or seen.add(link))]


def _glossary_chunks(texts: Iterable[str], glossary: GlossaryLinks | None) -> list[str]:
    """``## Glossary Terms``: PCG terms the task's text uses (plan §12.2, Tier 2)."""
    if glossary is None:
        return []
    found = glossary.index.find(texts)
    entries = sorted(
        (glossary.targets[term_id] for term_id in found if term_id in glossary.targets),
        key=lambda entry: (entry[1].casefold(), entry[0]),
    )
    if not entries:
        return []
    items = "\n".join(f"- [[{stem}|{link_display(display)}]]" for stem, display in entries)
    return ["## Glossary Terms", items]


def _element_items(elements: list[dict]) -> list[str]:
    """Elements as nested list items, sub-elements under their parent."""
    chunks: list[str] = []
    children: list[str] = []
    head: str | None = None

    def flush() -> None:
        if head is not None:
            chunks.append(list_item(head, list(children)))
        children.clear()

    for element in elements:
        block_id = element_block_id(element['code'])
        line = f"**{element['code']}** {escape_md(element['text'])} ^{block_id}"
        if element.get("sub"):
            if head is None:
                raise BuildError(f"sub-element {element['code']} without a parent element")
            children.append(list_item(line, []))
        else:
            flush()
            head = line
    flush()
    return chunks


def _section_chunks(heading: str, section: dict) -> list[str]:
    chunks = [f"## {heading}", escape_md(section["lead_in"])]
    chunks.extend(_element_items(section["elements"]))
    return chunks


def task_texts(task: dict) -> list[str]:
    """The task's own wording, for glossary matching."""
    texts = [task["title"], task["objective"], *task["notes"]]
    for kind in ("knowledge", "risk", "skills"):
        texts.extend(element["text"] for element in task[kind]["elements"])
    return texts


def build_task_note(task: dict, area: dict, aliases: list[str], context: AcsContext) -> Note:
    source = task["source"]
    frontmatter: list[tuple[str, Value]] = [
        ("id", task["id"]),
        ("type", "acs_task"),
        ("citation", task["code"]),
        ("area", area["roman"]),
        ("task", task["letter"]),
        *_frontmatter_common(source, task["canonical_hash"]),
        ("title", task["title"]),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["acs"]))
    frontmatter.append(("cssclasses", [TEXT_CSS_CLASS]))

    classes = ", ".join(task["classes"]) if task["classes"] else "all classes"
    chunks = [
        f"# {task['code']} — {escape_md(task['title'])}",
        _source_callout(source, context.publication),
        f"**Area of Operation:** [[{area_stem(area['code'])}|{link_display(area_display(area))}]]"
        f" · **Applies to:** {escape_md(classes)}",
        "## References",
        escape_md(task["references_text"]),
    ]
    links = _reference_links(task, context)
    if links:
        chunks.append("\n".join(f"- {link}" for link in links))
    chunks.extend(["## Objective", escape_md(task["objective"])])
    chunks.extend(_note_callout(note) for note in task["notes"])
    chunks.extend(_section_chunks("Knowledge", task["knowledge"]))
    chunks.extend(_section_chunks("Risk Management", task["risk"]))
    chunks.extend(_section_chunks("Skills", task["skills"]))
    if context.study_map is not None:
        from far_aim.generate import prep_notes

        chunks.extend(
            prep_notes.where_to_study_chunks(
                task, context.study_map, context.study_targets or {}  # type: ignore[arg-type]
            )
        )
    chunks.extend(_glossary_chunks(task_texts(task), context.glossary))
    return Note(
        kind="acs_task",
        path_parts=(ACS_DIR, ACS_FOLDER, f"{task_stem(task['code'])}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_area_note(area: dict, aliases: list[str], publication: dict) -> Note:
    source = area["source"]
    frontmatter: list[tuple[str, Value]] = [
        ("id", area["id"]),
        ("type", "acs_area"),
        ("citation", area["code"]),
        ("area", area["roman"]),
        *_frontmatter_common(source, area["canonical_hash"]),
        ("title", area["title"]),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["acs"]))
    items = "\n".join(
        f"- [[{task_stem(task['code'])}|{link_display(task_display(task))}]]"
        for task in area["tasks"]
    )
    chunks = [
        f"# {escape_md(area_display(area))}",
        _source_callout(source, publication),
        f"Part of [[{ACS_INDEX_STEM}|{link_display(publication['title'])}]].",
        "## Tasks",
        items,
    ]
    return Note(
        kind="acs_area",
        path_parts=(ACS_DIR, ACS_FOLDER, f"{area_stem(area['code'])}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def render_blocks(blocks: list[dict]) -> list[str]:
    """Appendix / front-matter blocks → Markdown chunks; unknown types fail (§32.2)."""
    chunks: list[str] = []
    for block in blocks:
        kind = block["type"]
        if kind == "heading":
            chunks.append(f"### {escape_md(block['text'])}")
        elif kind == "text":
            chunks.append(escape_md(block["text"]))
        elif kind == "note":
            chunks.append(_note_callout(block["text"]))
        elif kind == "list":
            chunks.extend(list_item(escape_md(item["text"]), []) for item in block["items"])
        elif kind == "preformatted":
            if any("```" in line for line in block["lines"]):
                raise BuildError("preformatted block contains a code fence")
            chunks.append("\n".join(["```text", *block["lines"], "```"]))
        else:
            raise BuildError(f"no renderer for ACS block type {kind!r}")
    return chunks


def build_appendix_note(appendix: dict, aliases: list[str], publication: dict) -> Note:
    source = appendix["source"]
    frontmatter: list[tuple[str, Value]] = [
        ("id", appendix["id"]),
        ("type", "acs_appendix"),
        ("citation", appendix_stem(appendix["number"])),
        ("appendix", appendix["number"]),
        *_frontmatter_common(source, appendix["canonical_hash"]),
        ("title", appendix["title"]),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["acs"]))
    frontmatter.append(("cssclasses", [TEXT_CSS_CLASS]))
    chunks = [
        f"# {escape_md(appendix['heading'])}",
        _source_callout(source, publication),
        f"Part of [[{ACS_INDEX_STEM}|{link_display(publication['title'])}]].",
        "## Official Text",
        *render_blocks(appendix["content"]),
    ]
    return Note(
        kind="acs_appendix",
        path_parts=(ACS_DIR, ACS_FOLDER, f"{appendix_stem(appendix['number'])}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def _code_link(code: str, codes: frozenset[str]) -> str:
    """A change-note code as a block link to its element when the layer has it."""
    if code in codes:
        return f"[[{task_stem(task_code_of(code))}#^{element_block_id(code)}|{code}]]"
    return f"`{code}`"


def build_acs_index(
    docs: dict[str, dict], title_hash: str, element_codes: frozenset[str]
) -> Note:
    """``ACS Private Pilot Airplane.md``: edition, foreword, revision history,
    the FAA's own change note, the areas with their tasks, and the appendices."""
    publication = next(
        (d for d in docs.values() if d["document_type"] == acs_model.DOCUMENT_TYPE_PUBLICATION),
        None,
    )
    if publication is None:
        raise BuildError("ACS layer has no publication document")
    source = publication["source"]
    frontmatter: list[tuple[str, Value]] = [
        ("id", publication["id"]),
        ("type", "acs_index"),
        ("citation", ACS_CITATION),
        *_frontmatter_common(source, title_hash),
        ("title", publication["title"]),
        ("tags", ["acs"]),
    ]
    areas = sorted(
        (d for d in docs.values() if d["document_type"] == acs_model.DOCUMENT_TYPE_AREA),
        key=lambda d: d["number"],
    )
    appendices = sorted(
        (d for d in docs.values() if d["document_type"] == acs_model.DOCUMENT_TYPE_APPENDIX),
        key=lambda d: d["number"],
    )
    history = publication["revision_history"]
    table = "\n".join(
        [
            "| " + " | ".join(escape_md(c) for c in history["columns"]) + " |",
            "| " + " | ".join("---" for _ in history["columns"]) + " |",
            *(
                f"| {escape_md(r['document_number'])} | {escape_md(r['description'])} | "
                f"{escape_md(r['date'])} |"
                for r in history["rows"]
            ),
        ]
    )
    change_items: list[str] = []
    for bullet in publication["changes"]["bullets"]:
        children = []
        if bullet["codes"]:
            children.append(", ".join(_code_link(code, element_codes) for code in bullet["codes"]))
        change_items.append(list_item(escape_md(bullet["text"]), children))
    area_items: list[str] = []
    for area in areas:
        tasks = [
            list_item(f"[[{task_stem(t['code'])}|{link_display(task_display(t))}]]", [])
            for t in area["tasks"]
        ]
        area_items.append(
            list_item(f"[[{area_stem(area['code'])}|{link_display(area_display(area))}]]", tasks)
        )
    appendix_items = "\n".join(
        f"- [[{appendix_stem(a['number'])}|{link_display(appendix_display(a))}]]"
        for a in appendices
    )
    chunks = [
        f"# {escape_md(publication['title'])}",
        _source_callout(source, publication),
        f"**Document:** {escape_md(publication['document_number'])} · "
        f"**Edition:** {escape_md(publication['edition_date'])} · "
        f"**Effective:** {escape_md(source['effective_date'])} · "
        f"**Published by:** {escape_md(' — '.join(publication['cover']['publisher']))}",
        "## Foreword",
        *render_blocks(publication["foreword"]),
        "## Revision History",
        table,
        f"## Major Enhancements to {escape_md(publication['document_number'])}",
        *change_items,
        "## Introduction",
        *render_blocks(publication["introduction"]),
        "## Areas of Operation",
        *area_items,
    ]
    if appendix_items:
        chunks.extend(["## Appendices", appendix_items])
    return Note(
        kind="acs_index",
        path_parts=(ACS_DIR, ACS_FOLDER, f"{ACS_INDEX_STEM}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )
