"""Note builders: canonical documents → complete vault notes (plan §10).

Each builder returns a :class:`Note` — vault-relative path, schema-ordered
frontmatter, and Markdown body. Official wording flows through the
``markdown`` renderers verbatim; everything else here (H1 composition, the
Source callout, index listings) is derived display text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from far_aim.generate import BuildError, enrich, naming
from far_aim.generate.frontmatter import Value
from far_aim.generate.markdown import ECFR_BASE_URL, escape_md, render_blocks
from far_aim.links import citations as cites

FAR_DIR = "FAR"
TITLE_INDEX_STEM = "Title 14"
SOURCE_STATUS_STEM = "Source Status"
HOME_STEM = "Home"

# Curated entry notes (Phase 7): committed, user-editable notes the generator
# links to but never writes. Their stems are registered as link targets so the
# generated Home note may point at them; everything under their folders is
# curated territory the sync never touches (it only owns FAR/AIM/PCG and the
# root status/home notes). Renaming these files breaks Home's links — Obsidian
# shows them unresolved — but never the build of the file itself.
CURATED_ENTRIES = (
    ("Collections", ("Collections", "Collections.md")),
    ("Topics", ("Topics", "Topics.md")),
    ("Study", ("Study", "Study.md")),
)

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


def _pending_amendment_chunks(note: dict | None) -> list[str]:
    """The eCFR's "Link to an amendment published at …" pointers attached to
    an authority/source/editorial note (present only when the source carries
    one — see ``parsers.ecfr._parse_hed_pspace``)."""
    entries = (note or {}).get("amendment_notes") or []
    if not entries:
        return []
    return ["**Amendment notes:**", *(escape_md(entry) for entry in entries)]


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
        chunks.extend(_pending_amendment_chunks(note))
    return chunks


def _xref_chunks(
    content: list[dict],
    known_sections: set[str],
    self_section: str | None,
    known_parts: set[str] | None = None,
    self_part: str | None = None,
) -> list[str]:
    """Sections the text cites (natural order), then parts (part order).

    A number this document attributes to another CFR title anywhere in its
    text is never linked as Title 14, even from a bare ``§``/``part``
    elsewhere. Parts count only in their CFR-qualified forms (``part 121 of
    this chapter``, ``14 CFR part 121``): Title 14 text also says ``Part 1``
    of an ICAO Annex or an IEC standard, or heads an appendix's own parts.
    The document's own section and part are never listed.
    """
    tokens: list[str] = []
    banned: set[str] = set()
    parts: list[str] = []
    banned_parts: set[str] = set()
    for text in cites.collect_text(content):
        tokens.extend(cites.extract_citations(text))
        banned.update(cites.extract_other_title_citations(text))
        if known_parts:
            parts.extend(cites.extract_part_citations(text, qualified_only=True))
            banned_parts.update(cites.extract_other_title_parts(text))
    tokens = [token for token in tokens if token not in banned]
    parts = [part for part in parts if part not in banned_parts and part != self_part]
    resolved = cites.resolve(tokens, known_sections, exclude=self_section)
    items = [f"- [[{sec}|§ {sec}]]" for sec in resolved]
    for part in cites.resolve_parts(parts, known_parts or set()):
        items.append(f"- [[{naming.part_index_stem(part)}]]")
    if not items:
        return []
    return ["## Explicit Cross-References", "\n".join(items)]


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
    sec: dict,
    aliases: list[str],
    known_sections: set[str],
    known_parts: set[str] | None = None,
    *,
    related: enrich.RelatedIndex | None = None,
    related_targets: enrich.Targets | None = None,
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
    xrefs = _xref_chunks(sec["content"], known_sections, section, known_parts, sec["part"])
    chunks.extend(xrefs)
    # Tier 4 last, visibly separate, and never repeating an explicit reference.
    chunks.extend(
        enrich.related_chunks(
            sec["id"], related, related_targets or {}, exclude=enrich.linked_stems(xrefs)
        )
    )

    return Note(
        kind="regulation",
        path_parts=(FAR_DIR, naming.part_folder_name(part), f"{naming.section_stem(section)}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_appendix_note(
    apx: dict, aliases: list[str], known_sections: set[str], known_parts: set[str] | None = None
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
    chunks.extend(_xref_chunks(apx["content"], known_sections, None, known_parts, apx["part"]))

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
                    chunks.extend(_pending_amendment_chunks(child["authority"]))
                if child["source_note"]:
                    chunks.append(_labelled("Source", child["source_note"]["text"]))
                    chunks.extend(_pending_amendment_chunks(child["source_note"]))
                for note in child["editorial_notes"]:
                    chunks.append(f"**{escape_md(note['heading'])}** {escape_md(note['text'])}")
                    chunks.extend(_pending_amendment_chunks(note))
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
        chunks.extend(_pending_amendment_chunks(part_doc["authority"]))
    if part_doc["source_note"]:
        chunks.append(_labelled("Source", part_doc["source_note"]["text"]))
        chunks.extend(_pending_amendment_chunks(part_doc["source_note"]))
    for note in part_doc["editorial_notes"]:
        chunks.append(f"**{escape_md(note['heading'])}** {escape_md(note['text'])}")
        chunks.extend(_pending_amendment_chunks(note))
    chunks.extend(render_blocks(part_doc["notes"]))
    for xref in part_doc["cross_references"]:
        chunks.append(f"**{escape_md(xref['heading'])}** {escape_md(xref['text'])}")
        chunks.extend(_pending_amendment_chunks(xref))
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


def build_home(*, has_aim: bool, has_pcg: bool, has_concepts: bool = False) -> Note:
    """``vault/Home.md`` — the vault's entry point (plan §22 Phase 7).

    Static navigation only: edition details live in ``Source Status`` so this
    note never changes on a source update. Corpus links appear only for the
    layers actually built (a FAR-only build must not emit broken links); the
    curated entry links always verify because their stems are registered
    unconditionally (:data:`CURATED_ENTRIES`).
    """
    sources = [f"- [[{TITLE_INDEX_STEM}|Title 14, Code of Federal Regulations (the FARs)]]"]
    if has_aim:
        sources.append("- [[AIM|Aeronautical Information Manual]]")
    if has_pcg:
        sources.append("- [[PCG|Pilot/Controller Glossary]]")
    sources.append(f"- [[{SOURCE_STATUS_STEM}]] — the editions this vault is built from")
    study = [
        "- [[Collections]] — study collections for a certificate or rating",
        "- [[Topics]] — notes that gather everything on one concept across sources",
        "- [[Study]] — freeform working notes",
    ]
    finding = [
        "- **Search** any citation (`91.155`, `AIM 4-1-9`) or heading — "
        "citation forms and headings are note aliases.",
        "- **Backlinks** on any note list every note that cites it — open a "
        "glossary term or a section to see everything referring to it.",
        "- Section notes end with their explicit cross-references; AIM notes "
        "also list the glossary terms they use.",
    ]
    if has_concepts:
        finding.append(
            "- A trailing **Related (derived)** section, where present, holds "
            "similarity-based suggestions — a study aid, not a cross-reference."
        )

    chunks = [
        "# FAR/AIM Knowledge Vault",
        "Reference notes generated from official U.S. aviation sources, plus a"
        " curated study layer. Official wording is never altered; every"
        " generated note records the source edition it came from.",
        "## Sources",
        "\n".join(sources),
        "## Study layer",
        *(
            [
                "The [[Concept Map]] lists study concepts, what each builds on, and a"
                " suggested order; it is generated from the curated concept graph."
            ]
            if has_concepts
            else []
        ),
        "Curated notes live outside the generated trees and are never touched"
        " by a rebuild:",
        "\n".join(study),
        "## Finding things",
        "\n".join(finding),
    ]
    return Note(
        kind="home",
        path_parts=(f"{HOME_STEM}.md",),
        frontmatter=[
            ("id", "home"),
            ("type", "home"),
            ("generated", True),
            ("title", "FAR/AIM Knowledge Vault"),
        ],
        body="\n\n".join(chunks) + "\n",
    )
