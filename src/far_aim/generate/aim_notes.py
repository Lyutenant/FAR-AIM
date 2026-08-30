"""AIM note builders: canonical AIM documents → vault notes (plan §9, §10.2).

Layout under ``vault/AIM/``:

    AIM.md                         # publication index
    Chapter 04/AIM Chapter 4.md    # chapter index (sections → paragraphs)
    Chapter 04/AIM 4-1.md          # section note
    Chapter 04/4-1-9.md            # paragraph note (stable citation stem)
    Appendices/AIM Appendix 3.md
    assets/<figure file>           # archived figures, embedded by notes

Paragraph stems follow the plan verbatim (``4-1-9``); container notes carry
an ``AIM`` prefix so they never collide with FAR part-local section stems.
"""

from __future__ import annotations

import re

from far_aim.generate import BuildError, naming
from far_aim.generate.aim_markdown import asset_prefix_for, render_aim_blocks
from far_aim.generate.frontmatter import Value
from far_aim.generate.markdown import escape_md
from far_aim.generate.notes import Note, link_display

AIM_DIR = "AIM"
AIM_INDEX_STEM = "AIM"
APPENDICES_DIR = "Appendices"
ASSETS_DIR = "assets"

# id → (stem, display text) for every linkable AIM note, built by the registry.
Targets = dict[str, tuple[str, str]]


def edition_text(source: dict) -> str:
    """``Basic with Change 1, 2 and 3 (effective 2026-07-09)``."""
    return f"{source['edition_label']} (effective {source['effective_date']})"


def _source_callout(source: dict, url: str) -> str:
    return "\n".join(
        [
            "> [!info] Source",
            f"> FAA Aeronautical Information Manual, {edition_text(source)} — "
            f"[view on FAA]({url})",
        ]
    )


_TOC_LABEL_RE = re.compile(r"^Section (\d+)\.$")


def paragraph_display(para: dict) -> str:
    return f"AIM {para['paragraph']} — {para['heading']}"


def section_number_display(sec: dict) -> int:
    """The section number as the FAA cites it.

    Normally identical to ``section``; chapter 0's lone section is published
    as ``chap0_section_0.html`` (stable id ``aim-0-0``) but officially
    labelled "Section 1." — the preserved ``toc_label`` wins for display.
    """
    match = _TOC_LABEL_RE.match(sec.get("toc_label") or "")
    return int(match.group(1)) if match else sec["section"]


def section_display(sec: dict) -> str:
    return f"AIM Chapter {sec['chapter']}, Section {section_number_display(sec)} — {sec['heading']}"


def chapter_display(chapter: dict) -> str:
    return f"AIM Chapter {chapter['chapter']} — {chapter['heading']}"


def appendix_display(apx: dict) -> str:
    return f"AIM Appendix {apx['appendix']} — {apx['heading']}"


def _xref_chunks(refs: list[dict], targets: Targets, self_id: str) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for ref in refs:
        target = ref.get("target")
        if target is None or target == self_id or target in seen:
            continue
        entry = targets.get(target)
        if entry is None:
            continue
        seen.add(target)
        stem, display = entry
        items.append(f"- [[{stem}|{link_display(display)}]]")
    if not items:
        return []
    return ["## Explicit Cross-References", "\n".join(items)]


def _official_text_chunks(content: list[dict], path_parts: tuple[str, ...]) -> list[str]:
    chunks = render_aim_blocks(content, asset_prefix_for(path_parts))
    return ["## Official Text", *chunks] if chunks else []


def _edition_frontmatter(source: dict) -> list[tuple[str, Value]]:
    return [
        ("source", "faa"),
        ("effective_date", source["effective_date"]),
        ("change", source["change"]),
    ]


def build_paragraph_note(para: dict, aliases: list[str], targets: Targets) -> Note:
    source = para["source"]
    frontmatter: list[tuple[str, Value]] = [
        ("id", para["id"]),
        ("type", "aim"),
        ("citation", f"AIM {para['paragraph']}"),
        ("chapter", para["chapter"]),
        ("section", para["section"]),
        ("paragraph", para["paragraph"]),
        *_edition_frontmatter(source),
        ("canonical_hash", para["canonical_hash"]),
        ("generated", True),
        ("title", para["heading"]),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["aim"]))
    path_parts = (
        AIM_DIR,
        naming.aim_chapter_folder(para["chapter"]),
        f"{naming.aim_paragraph_stem(para['paragraph'])}.md",
    )
    chunks = [
        f"# AIM {para['paragraph']} — {escape_md(para['heading'])}",
        _source_callout(source, source["url"]),
        *_official_text_chunks(para["content"], path_parts),
        *_xref_chunks(para["explicit_references"], targets, para["id"]),
    ]
    return Note(
        kind="aim",
        path_parts=path_parts,
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_section_note(sec: dict, aliases: list[str], targets: Targets) -> Note:
    source = sec["source"]
    display_number = section_number_display(sec)
    frontmatter: list[tuple[str, Value]] = [
        ("id", sec["id"]),
        ("type", "aim_section"),
        ("citation", f"AIM Chapter {sec['chapter']}, Section {display_number}"),
        ("chapter", sec["chapter"]),
        ("section", sec["section"]),
        *_edition_frontmatter(source),
        ("canonical_hash", sec["canonical_hash"]),
        ("generated", True),
        ("title", sec["heading"]),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["aim"]))
    path_parts = (
        AIM_DIR,
        naming.aim_chapter_folder(sec["chapter"]),
        f"{naming.aim_section_stem(sec['chapter'], sec['section'])}.md",
    )
    chunks = [
        f"# AIM Chapter {sec['chapter']}, Section {display_number} — {escape_md(sec['heading'])}",
        _source_callout(source, source["url"]),
        *_official_text_chunks(sec["content"], path_parts),
    ]
    if sec["paragraphs"]:
        items = [
            f"- [[{naming.aim_paragraph_stem(p['paragraph'])}|"
            f"{link_display(f'{p['paragraph']} — {p['heading']}')}]]"
            for p in sec["paragraphs"]
        ]
        chunks.extend(["## Paragraphs", "\n".join(items)])
    chunks.extend(_xref_chunks(sec["explicit_references"], targets, sec["id"]))
    return Note(
        kind="aim_section",
        path_parts=path_parts,
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_chapter_note(chapter: dict, aliases: list[str]) -> Note:
    source = chapter["source"]
    frontmatter: list[tuple[str, Value]] = [
        ("id", chapter["id"]),
        ("type", "aim_chapter"),
        ("citation", f"AIM Chapter {chapter['chapter']}"),
        ("chapter", chapter["chapter"]),
        *_edition_frontmatter(source),
        ("canonical_hash", chapter["canonical_hash"]),
        ("generated", True),
        ("title", chapter["heading"]),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["aim"]))
    lines: list[str] = []
    for sec in chapter["sections"]:
        stem = naming.aim_section_stem(sec["chapter"], sec["section"])
        display = f"Section {section_number_display(sec)} — {sec['heading']}"
        lines.append(f"- [[{stem}|{link_display(display)}]]")
        for para in sec["paragraphs"]:
            display = link_display(f"{para['paragraph']} — {para['heading']}")
            lines.append(f"  - [[{naming.aim_paragraph_stem(para['paragraph'])}|{display}]]")
    chunks = [
        f"# AIM Chapter {chapter['chapter']} — {escape_md(chapter['heading'])}",
        _source_callout(source, source["url"]),
    ]
    if lines:
        chunks.extend(["## Contents", "\n".join(lines)])
    return Note(
        kind="aim_chapter",
        path_parts=(
            AIM_DIR,
            naming.aim_chapter_folder(chapter["chapter"]),
            f"{naming.aim_chapter_stem(chapter['chapter'])}.md",
        ),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_appendix_note(apx: dict, aliases: list[str], targets: Targets) -> Note:
    source = apx["source"]
    frontmatter: list[tuple[str, Value]] = [
        ("id", apx["id"]),
        ("type", "aim_appendix"),
        ("citation", f"AIM Appendix {apx['appendix']}"),
        ("appendix", apx["appendix"]),
        *_edition_frontmatter(source),
        ("canonical_hash", apx["canonical_hash"]),
        ("generated", True),
        ("title", apx["heading"]),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["aim"]))
    path_parts = (AIM_DIR, APPENDICES_DIR, f"{naming.aim_appendix_stem(apx['appendix'])}.md")
    chunks = [
        f"# AIM Appendix {apx['appendix']} — {escape_md(apx['heading'])}",
        _source_callout(source, source["url"]),
        *_official_text_chunks(apx["content"], path_parts),
        *_xref_chunks(apx["explicit_references"], targets, apx["id"]),
    ]
    return Note(
        kind="aim_appendix",
        path_parts=path_parts,
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_aim_index(docs: dict[str, dict], title_hash: str) -> Note:
    """``vault/AIM/AIM.md``: the index page's front matter, chapters, appendices."""
    publication = next(
        (d for d in docs.values() if d["document_type"] == "aim_publication"), None
    )
    if publication is None:
        raise BuildError("AIM layer has no publication document (index page)")
    chapters = sorted(
        (d for d in docs.values() if d["document_type"] == "aim_chapter"),
        key=lambda d: d["chapter"],
    )
    appendices = sorted(
        (d for d in docs.values() if d["document_type"] == "aim_appendix"),
        key=lambda d: d["appendix"],
    )
    source = publication["source"]
    path_parts = (AIM_DIR, f"{AIM_INDEX_STEM}.md")
    frontmatter: list[tuple[str, Value]] = [
        ("id", publication["id"]),
        ("type", "index"),
        ("citation", "AIM"),
        *_edition_frontmatter(source),
        ("canonical_hash", title_hash),
        ("generated", True),
        ("title", publication["title"]),
        ("tags", ["aim"]),
    ]
    chunks = [
        f"# {escape_md(publication['title'])}",
        _source_callout(source, source["url"]),
        *_official_text_chunks(
            [*publication["description"], *publication["summary"]], path_parts
        ),
    ]
    if chapters:
        items = [
            f"- [[{naming.aim_chapter_stem(c['chapter'])}|"
            f"{link_display(f'Chapter {c['chapter']} — {c['heading']}')}]]"
            for c in chapters
        ]
        chunks.extend(["## Chapters", "\n".join(items)])
    if appendices:
        items = [
            f"- [[{naming.aim_appendix_stem(a['appendix'])}|"
            f"{link_display(f'Appendix {a['appendix']} — {a['heading']}')}]]"
            for a in appendices
        ]
        chunks.extend(["## Appendices", "\n".join(items)])
    return Note(
        kind="aim_index",
        path_parts=path_parts,
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )
