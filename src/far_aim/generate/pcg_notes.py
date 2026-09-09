"""PCG note builders: canonical PCG documents → vault notes (plan §9, §10.3).

Layout under ``vault/PCG/``:

    PCG.md                       # publication index (purpose, letters, terms)
    A/CONTROLLED AIRSPACE.md     # one note per term, named by the term itself
    A/ACC (ICAO).md              # sanitized stem; the verbatim term is the title

The term is the citation (plan §9): the stem is the term text itself,
deterministically sanitized to a portable, wikilink-safe filename
(``naming.pcg_term_stem``); the verbatim term remains the note title, H1
and — when sanitization changed anything — an alias.

Official wording is reproduced exactly: the definition renders the full
entry text verbatim (term, separator and all). Cross-references render
under ``## See Also`` (glossary terms, wikilinked when the target is in
the corpus) and ``## References`` (external documents — the AIM, CFR
parts, FAA orders — linked when the source carried a URL). Hyperlinks
embedded inside a definition render under ``## References`` too: their
wording stays verbatim in the Official Text (the renderers never rewrite
official wording into links, and Markdown escaping defeats bare-URL
autolinking), so the References entry is what keeps the authoritative
destination clickable. Notes/sub-lists mirror the AIM renderer's
decisions (marker-led nested list items via ``generate.hierarchy``,
callout boxes titled with the verbatim label).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from far_aim.generate import BuildError, naming
from far_aim.generate.aim_markdown import text_md
from far_aim.generate.frontmatter import Value
from far_aim.generate.hierarchy import TEXT_CSS_CLASS, list_item
from far_aim.generate.markdown import escape_md
from far_aim.generate.notes import Note, link_display
from far_aim.links import citations as cites

PCG_DIR = "PCG"
PCG_INDEX_STEM = "PCG"

# id → (stem, display text) for every linkable PCG term, built by the registry.
Targets = dict[str, tuple[str, str]]


def edition_text(source: dict) -> str:
    return f"{source['edition_label']} (effective {source['effective_date']})"


def _source_callout(source: dict, url: str) -> str:
    return "\n".join(
        [
            "> [!info] Source",
            f"> FAA Pilot/Controller Glossary, {edition_text(source)} — "
            f"[view on FAA]({url})",
        ]
    )


def term_display(term_doc: dict) -> str:
    """Display text for links to a term note.

    Square brackets would be stripped by wikilink sanitization; parenthesize
    them instead so ``NAVIGATION SPECIFICATION [ICAO]`` reads as
    ``NAVIGATION SPECIFICATION (ICAO)`` rather than losing its tag.
    """
    return term_doc["term"].replace("[", "(").replace("]", ")")


def _alpha(index: int) -> str:
    letters = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("a") + rem) + letters
    return letters


def _list_marker(style: str, index: int) -> str:
    if style == "a":
        return f"{_alpha(index)}."
    if style == "1":
        return f"{index + 1}."
    raise BuildError(f"unsupported PCG list style {style!r}")


def _note_callout(block: dict) -> str:
    label = block.get("label") or "Note"
    kind = "cite" if label.upper().startswith("REFERENCE") else "note"
    lines = [f"> [!{kind}] {escape_md(label.replace(chr(10), ' '))}"]
    body = text_md(block["text"])
    if body:
        lines.extend(">" if not line else f"> {line}" for line in body.split("\n"))
    return "\n".join(lines)


def render_pcg_blocks(blocks: list[dict]) -> list[str]:
    """Render non-reference PCG blocks to Markdown chunks."""
    chunks: list[str] = []
    for block in blocks:
        kind = block["type"]
        if kind in ("entry", "text"):
            if block["text"]:
                chunks.append(text_md(block["text"]))
        elif kind == "list":
            for index, item in enumerate(block["items"]):
                marker = f"**{_list_marker(block['style'], index)}**"
                text = text_md(item["text"])
                chunks.append(list_item(f"{marker} {text}" if text else marker, []))
        elif kind == "note":
            chunks.append(_note_callout(block))
        elif kind == "reference":
            continue  # rendered in the See Also / References sections
        else:
            raise BuildError(f"no renderer for PCG block type {kind!r}")
    return chunks


@dataclass(frozen=True)
class ReferTargets:
    """Where a ``Refer to`` row may link outside the glossary (plan §12.1).

    ``far`` carries the FAR section and part numbers whose notes exist;
    ``aim_index_stem`` is the AIM publication note's stem when the AIM
    corpus is built (the PCG's bare ``Refer to AIM`` rows point there).
    """

    far: object  # aim_notes.FarTargets; typed loosely to avoid a cycle
    aim_index_stem: str | None = None


_AIM_REFER_RE = re.compile(r"^\s*AIM\.?\s*$")


def _refer_links(text: str, refer: ReferTargets | None) -> list[str]:
    """Wikilinks for the FAR/AIM documents a ``Refer to`` row names."""
    if refer is None:
        return []
    links: list[str] = []
    if refer.aim_index_stem and _AIM_REFER_RE.match(text):
        return [f"[[{refer.aim_index_stem}]]"]
    far = refer.far
    banned_sections = cites.extract_other_title_citations(text)
    banned_parts = cites.extract_other_title_parts(text)
    sections = [t for t in cites.extract_citations(text) if t not in banned_sections]
    parts = [t for t in cites.extract_part_citations(text) if t not in banned_parts]
    for sec in cites.resolve(sections, set(far.sections)):
        links.append(f"[[{naming.section_stem(sec)}|§ {sec}]]")
    for part in cites.resolve_parts(parts, set(far.parts)):
        links.append(f"[[{naming.part_index_stem(part)}]]")
    return links


def _reference_sections(
    term_doc: dict, targets: Targets, refer: ReferTargets | None = None
) -> list[str]:
    see_items: list[str] = []
    refer_items: list[str] = []
    seen: set[tuple[str, str | None]] = set()
    for block in term_doc["content"]:
        if block.get("type") != "reference":
            continue
        key = (block["text"], block.get("target") or block.get("url"))
        if key in seen:
            continue
        seen.add(key)
        target = block.get("target")
        entry = targets.get(target) if target else None
        # A "Refer to" row naming the AIM or a FAR document means that
        # publication, even when the parser also matched a glossary entry of
        # the same name ("Refer to AIM" → the manual, not the PCG's AIM term).
        outside = _refer_links(block["text"], refer) if block["kind"] == "refer" else []
        if len(outside) == 1 and outside[0].startswith("[[") and "|" not in outside[0]:
            item = f"- {outside[0]}" if _AIM_REFER_RE.match(block["text"]) else (
                f"- [[{outside[0][2:-2]}|{link_display(block['text'])}]]"
            )
        elif outside:
            item = f"- {escape_md(block['text'])} — " + ", ".join(outside)
        elif entry is not None and target != term_doc["id"]:
            stem, display = entry
            item = f"- [[{stem}|{link_display(display)}]]"
        elif block.get("url"):
            # Embedded in-definition links carry no separate display text
            # (the URL itself is the visible wording); fall back to the URL.
            display = block["text"] or block["url"]
            item = f"- [{escape_md(display)}]({block['url']})"
        else:
            item = f"- {escape_md(block['text'])}"
        (see_items if block["kind"] == "see" else refer_items).append(item)
    chunks: list[str] = []
    if see_items:
        chunks.extend(["## See Also", "\n".join(see_items)])
    if refer_items:
        chunks.extend(["## References", "\n".join(refer_items)])
    return chunks


def _edition_frontmatter(source: dict) -> list[tuple[str, Value]]:
    return [
        ("source", "faa"),
        ("effective_date", source["effective_date"]),
        ("change", source["change"]),
    ]


def build_term_note(
    term_doc: dict, aliases: list[str], targets: Targets, refer: ReferTargets | None = None
) -> Note:
    source = term_doc["source"]
    term = term_doc["term"]
    stem = naming.pcg_term_stem(term)
    frontmatter: list[tuple[str, Value]] = [
        ("id", term_doc["id"]),
        ("type", "glossary"),
        ("term", term),
        ("letter", term_doc["letter"]),
        *_edition_frontmatter(source),
        ("canonical_hash", term_doc["canonical_hash"]),
        ("generated", True),
        ("title", term),
    ]
    if aliases:
        frontmatter.append(("aliases", aliases))
    frontmatter.append(("tags", ["pcg"]))
    frontmatter.append(("cssclasses", [TEXT_CSS_CLASS]))
    body_chunks = render_pcg_blocks(term_doc["content"])
    chunks = [
        f"# {escape_md(term)}",
        _source_callout(source, source["url"]),
    ]
    if body_chunks:
        chunks.extend(["## Official Text", *body_chunks])
    chunks.extend(_reference_sections(term_doc, targets, refer))
    return Note(
        kind="pcg",
        path_parts=(PCG_DIR, naming.pcg_letter_folder(term_doc["letter"].lower()), f"{stem}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def build_pcg_index(docs: dict[str, dict], title_hash: str) -> Note:
    """``vault/PCG/PCG.md``: the index front matter plus every term by letter."""
    publication = next(
        (d for d in docs.values() if d["document_type"] == "pcg_publication"), None
    )
    if publication is None:
        raise BuildError("PCG layer has no publication document (index page)")
    source = publication["source"]
    frontmatter: list[tuple[str, Value]] = [
        ("id", publication["id"]),
        ("type", "index"),
        ("citation", "PCG"),
        *_edition_frontmatter(source),
        ("canonical_hash", title_hash),
        ("generated", True),
        ("title", publication["title"]),
        ("tags", ["pcg"]),
        ("cssclasses", [TEXT_CSS_CLASS]),
    ]
    chunks = [
        f"# {escape_md(publication['title'])}",
        _source_callout(source, source["url"]),
        *render_pcg_blocks([*publication["purpose"], *publication["summary"]]),
    ]
    letters = sorted(
        (d for d in docs.values() if d["document_type"] == "pcg_letter"),
        key=lambda d: d["letter"],
    )
    for letter_doc in letters:
        items = []
        for term_doc in letter_doc["terms"]:
            stem = naming.pcg_term_stem(term_doc["term"])
            items.append(f"- [[{stem}|{link_display(term_display(term_doc))}]]")
        if items:
            chunks.extend([f"## {escape_md(letter_doc['letter'])}", "\n".join(items)])
    return Note(
        kind="pcg_index",
        path_parts=(PCG_DIR, f"{PCG_INDEX_STEM}.md"),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )
