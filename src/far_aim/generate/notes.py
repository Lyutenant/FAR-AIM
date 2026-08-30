"""Note builders: canonical documents → complete vault notes (plan §10).

Each builder returns a :class:`Note` — vault-relative path, schema-ordered
frontmatter, and Markdown body. Official wording flows through the
``markdown`` renderers verbatim; everything else here (H1 composition, the
Source callout, index listings) is derived display text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from far_aim.generate import BuildError, naming
from far_aim.generate.frontmatter import Value
from far_aim.generate.markdown import ECFR_BASE_URL, escape_md, render_blocks
from far_aim.links import citations as cites

FAR_DIR = "FAR"
TITLE_INDEX_STEM = "Title 14"
SOURCE_STATUS_STEM = "Source Status"

# Sections addressable by the eCFR point-in-time section URL; ranges and
# part-local numbers fall back to the part URL.
_SECTION_URL_RE = re.compile(r"^[0-9]+[a-z]?\.[0-9]+[a-z]?$")


@dataclass(frozen=True)
class Note:
    kind: str
    path_parts: tuple[str, ...]
    frontmatter: list[tuple[str, Value]]
    body: str


def display_heading(heading: str) -> str:
    """Display form of an official heading: exactly one trailing ``.`` stripped.

    The verbatim heading (period included) remains in canonical JSON, the
    source of truth (plan §32.4); this transform is display-only.
    """
    return heading[:-1] if heading.endswith(".") else heading


def link_display(text: str) -> str:
    """Sanitize display text for use inside a wikilink alias position."""
    return text.replace("[", "").replace("]", "").replace("|", "/")


def is_global_numbering(section: str, part: str) -> bool:
    return section == part or section.startswith(f"{part}.")


def ecfr_part_url(version: str, part: str) -> str:
    return f"{ECFR_BASE_URL}/on/{version}/title-14/part-{part}"


def ecfr_doc_url(version: str, doc: dict) -> str:
    section = doc.get("section")
    if section is not None and _SECTION_URL_RE.match(section):
        return f"{ECFR_BASE_URL}/on/{version}/title-14/section-{section}"
    return ecfr_part_url(version, doc["part"])


def _source_callout(version: str, url: str, extra_lines: list[str] | None = None) -> str:
    lines = [
        "> [!info] Source",
        f"> eCFR Title 14, issue {version} — [view on eCFR]({url})",
    ]
    lines.extend(f"> {line}" for line in extra_lines or [])
    return "\n".join(lines)


def _part_value(part: str) -> int | str:
    return int(part) if part.isdigit() else part


def section_display(sec: dict) -> str:
    """``§ 91.155 — Basic VFR weather minimums`` (index display text)."""
    marker = sec["head_marker"] or ("§" if is_global_numbering(sec["section"], sec["part"]) else "")
    head = f"{marker} {sec['section']}" if marker else sec["section"]
    return f"{head} — {display_heading(sec['heading'])}"


def _labelled(label: str, text: str) -> str:
    return f"**{label}:** {escape_md(text)}"


def _source_notes_chunks(
    authority: str | None,
    lists: list[tuple[str, list[str]]],
    editorial_notes: list[dict],
) -> list[str]:
    chunks: list[str] = []
    if authority:
        chunks.append(_labelled("Section authority", authority))
    for label, entries in lists:
        if entries:
            chunks.append(f"**{label}:**")
            chunks.extend(escape_md(entry) for entry in entries)
    for note in editorial_notes:
        chunks.append(f"**{escape_md(note['heading'])}** {escape_md(note['text'])}")
    return chunks


def _xref_chunks(
    content: list[dict], known_sections: set[str], self_section: str | None
) -> list[str]:
    tokens: list[str] = []
    banned: set[str] = set()
    for text in cites.collect_text(content):
        tokens.extend(cites.extract_citations(text))
        banned.update(cites.extract_other_title_citations(text))
    # A section number this document attributes to another CFR title anywhere
    # in its text is never linked as Title 14, even from a bare § elsewhere.
    tokens = [token for token in tokens if token not in banned]
    resolved = cites.resolve(tokens, known_sections, exclude=self_section)
    if not resolved:
        return []
    items = "\n".join(f"- [[{sec}|§ {sec}]]" for sec in resolved)
    return ["## Explicit Cross-References", items]


def _official_text_chunks(content: list[dict], *, reserved: bool) -> list[str]:
    """``## Official Text`` section chunks; empty when there is nothing to say.

    ``[Reserved]`` is emitted only for documents the source itself marks
    reserved — a non-reserved document with no content (e.g. a section whose
    text exists only as a pending amendment link) must not gain fabricated
    wording (plan §32.1/§32.3); its note simply carries no Official Text
    section.
    """
    chunks = render_blocks(content)
    if chunks:
        return ["## Official Text", *chunks]
    if reserved:
        return ["## Official Text", escape_md("[Reserved]")]
    return []


def build_section_note(
    sec: dict, aliases: list[str], known_sections: set[str]
) -> Note:
    part = sec["part"]
    section = sec["section"]
    version = sec["source"]["source_version"]
    global_num = is_global_numbering(section, part)
    marker = sec["head_marker"] or ("§" if global_num else "")
    disp = display_heading(sec["heading"])
    if global_num:
        citation = f"14 CFR {marker} {section}"
    elif marker:
        citation = f"14 CFR part {part}, {marker} {section}"
    else:
        citation = f"14 CFR part {part}, {section}"

    frontmatter: list[tuple[str, Value]] = [
        ("id", sec["id"]),
        ("type", "regulation"),
        ("citation", citation),
        ("title_number", sec["title_number"]),
        ("part", _part_value(part)),
        ("section", section),
        ("source", "ecfr"),
        ("source_version", version),
        ("canonical_hash", sec["canonical_hash"]),
        ("generated", True),
        ("title", disp),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["far", "regulation"]))

    head = f"{marker} {section}" if marker else section
    chunks = [
        f"# {head} — {escape_md(disp)}",
        _source_callout(version, ecfr_doc_url(version, sec)),
        *_official_text_chunks(sec["content"], reserved=sec["reserved"]),
    ]
    source_notes = _source_notes_chunks(
        sec["section_authority"],
        [
            ("Citations", sec["citations"]),
            ("Approvals", sec["approvals"]),
            ("Amendment notes", sec["amendment_notes"]),
        ],
        sec["editorial_notes"],
    )
    if source_notes:
        chunks.extend(["## Source Notes", *source_notes])
    chunks.extend(_xref_chunks(sec["content"], known_sections, section))

    return Note(
        kind="regulation",
        path_parts=(FAR_DIR, naming.part_folder_name(part), f"{naming.section_stem(section)}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_appendix_note(
    apx: dict, aliases: list[str], known_sections: set[str]
) -> Note:
    part = apx["part"]
    version = apx["source"]["source_version"]
    label = naming.appendix_label(part, apx["id"])
    stem = naming.appendix_stem(part, apx["id"])
    disp = display_heading(apx["heading"])

    frontmatter: list[tuple[str, Value]] = [
        ("id", apx["id"]),
        ("type", "appendix"),
        ("citation", f"14 CFR Part {part}, {label}"),
        ("title_number", apx["title_number"]),
        ("part", _part_value(part)),
        ("appendix", label),
        ("source", "ecfr"),
        ("source_version", version),
        ("canonical_hash", apx["canonical_hash"]),
        ("generated", True),
        ("title", disp),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["far", "regulation"]))

    chunks = [
        f"# {escape_md(apx['heading'])}",
        _source_callout(version, ecfr_part_url(version, part)),
        *_official_text_chunks(apx["content"], reserved=apx["reserved"]),
    ]
    source_notes = _source_notes_chunks(
        apx["section_authority"],
        [("Citations", apx["citations"])],
        apx["editorial_notes"],
    )
    if source_notes:
        chunks.extend(["## Source Notes", *source_notes])
    chunks.extend(_xref_chunks(apx["content"], known_sections, None))

    return Note(
        kind="appendix",
        path_parts=(FAR_DIR, naming.part_folder_name(part), f"{stem}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def _contents_chunks(part_doc: dict) -> list[str]:
    """The ``## Contents`` listing, mirroring ``children`` in document order."""
    chunks: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if items:
            chunks.append("\n".join(items))
            items.clear()

    def emit(children: list[dict]) -> None:
        for child in children:
            doc_type = child.get("document_type")
            if doc_type == "cfr_section":
                display = link_display(section_display(child))
                items.append(f"- [[{naming.section_stem(child['section'])}|{display}]]")
            elif doc_type == "cfr_appendix":
                stem = naming.appendix_stem(child["part"], child["id"])
                display = link_display(display_heading(child["heading"]))
                items.append(f"- [[{stem}|{display}]]")
            elif child.get("type") == "subpart":
                flush()
                heading = f"### Subpart {child['label']} — {escape_md(child['heading'])}"
                chunks.append(heading)
                if child["authority"]:
                    chunks.append(_labelled("Authority", child["authority"]["text"]))
                if child["source_note"]:
                    chunks.append(_labelled("Source", child["source_note"]["text"]))
                for note in child["editorial_notes"]:
                    chunks.append(f"**{escape_md(note['heading'])}** {escape_md(note['text'])}")
                emit(child["children"])
            elif child.get("type") == "subject_group":
                flush()
                chunks.append(f"**{escape_md(child['heading'])}**")
                emit(child["sections"])
            elif child.get("type") == "heading":
                flush()
                chunks.append(f"**{escape_md(child['text'])}**")
            else:
                kind = doc_type or child.get("type")
                raise BuildError(f"unknown part child {kind!r} in part {part_doc['part']!r}")
        flush()

    emit(part_doc["children"])
    return chunks


def build_part_index(part_doc: dict) -> Note:
    part = part_doc["part"]
    version = part_doc["source"]["source_version"]
    hierarchy = [
        escape_md(heading)
        for heading in (
            part_doc["subtitle_heading"],
            part_doc["chapter_heading"],
            part_doc["subchapter_heading"],
        )
        if heading
    ]

    frontmatter: list[tuple[str, Value]] = [
        ("id", part_doc["id"]),
        ("type", "index"),
        ("citation", f"14 CFR Part {part}"),
        ("title_number", part_doc["title_number"]),
        ("part", _part_value(part)),
        ("source", "ecfr"),
        ("source_version", version),
        ("canonical_hash", part_doc["canonical_hash"]),
        ("generated", True),
        ("title", part_doc["heading"]),
        ("tags", ["far"]),
    ]

    chunks = [
        f"# Part {part} — {escape_md(part_doc['heading'])}",
        _source_callout(version, ecfr_part_url(version, part), hierarchy),
    ]
    if part_doc["authority"]:
        chunks.append(_labelled("Authority", part_doc["authority"]["text"]))
    if part_doc["source_note"]:
        chunks.append(_labelled("Source", part_doc["source_note"]["text"]))
    for note in part_doc["editorial_notes"]:
        chunks.append(f"**{escape_md(note['heading'])}** {escape_md(note['text'])}")
    chunks.extend(render_blocks(part_doc["notes"]))
    for xref in part_doc["cross_references"]:
        chunks.append(f"**{escape_md(xref['heading'])}** {escape_md(xref['text'])}")
    if part_doc["reserved"]:
        chunks.append(escape_md("[Reserved]"))
    contents = _contents_chunks(part_doc)
    if contents:
        chunks.extend(["## Contents", *contents])

    return Note(
        kind="index",
        path_parts=(FAR_DIR, naming.part_folder_name(part), f"{naming.part_index_stem(part)}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_title_index(part_docs: dict[str, dict], version: str, title_hash: str) -> Note:
    frontmatter: list[tuple[str, Value]] = [
        ("id", "cfr-14"),
        ("type", "index"),
        ("citation", "14 CFR"),
        ("title_number", 14),
        ("source", "ecfr"),
        ("source_version", version),
        ("canonical_hash", title_hash),
        ("generated", True),
        ("title", "Aeronautics and Space"),
        ("tags", ["far"]),
    ]

    chunks = [
        "# Title 14 — Aeronautics and Space",
        _source_callout(version, f"{ECFR_BASE_URL}/on/{version}/title-14"),
    ]
    last_chapter: str | None = None
    last_subchapter: tuple[str | None, str | None] = (None, None)
    items: list[str] = []

    def flush() -> None:
        if items:
            chunks.append("\n".join(items))
            items.clear()

    for part in sorted(part_docs, key=naming.part_sort_key):
        doc = part_docs[part]
        if doc["chapter"] != last_chapter:
            flush()
            chunks.append(f"## {escape_md(doc['chapter_heading'])}")
            last_chapter = doc["chapter"]
            last_subchapter = (None, None)
        subchapter = (doc["chapter"], doc["subchapter"])
        if doc["subchapter"] is not None and subchapter != last_subchapter:
            flush()
            chunks.append(f"### {escape_md(doc['subchapter_heading'])}")
        last_subchapter = subchapter
        display = link_display(f"Part {part} — {doc['heading']}")
        items.append(f"- [[{naming.part_index_stem(part)}|{display}]]")
    flush()

    return Note(
        kind="index",
        path_parts=(FAR_DIR, f"{TITLE_INDEX_STEM}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_source_status(sources: dict[str, object]) -> Note:
    """``vault/Source Status.md`` from manifest state — accepted versions only.

    The manifest's ``last_checked_at`` is deliberately not rendered: it is
    the one volatile timestamp, and generated notes carry none (plan §32.10).
    """

    def current_through(name: str) -> str:
        state = sources.get(name)
        accepted = getattr(state, "accepted_version", None)
        effective = getattr(state, "effective_date", None)
        change = getattr(state, "change", None)
        if effective and change is not None:
            edition = "Basic" if change == 0 else f"Change {change}"
            return f"{edition} — effective {effective}"
        if accepted:
            return accepted
        if effective:
            return effective
        return "not yet ingested"

    rows = [
        ("eCFR Title 14", current_through("ecfr_title_14")),
        ("AIM", current_through("aim")),
        ("Pilot/Controller Glossary", current_through("pcg")),
    ]
    table = "\n".join(
        [
            "| Source | Current Through |",
            "| --- | --- |",
            *(f"| {name} | {value} |" for name, value in rows),
        ]
    )
    return Note(
        kind="status",
        path_parts=(f"{SOURCE_STATUS_STEM}.md",),
        frontmatter=[
            ("id", "source-status"),
            ("type", "status"),
            ("generated", True),
            ("title", "Source Status"),
        ],
        body="\n\n".join(["# Source Status", table]) + "\n",
    )
