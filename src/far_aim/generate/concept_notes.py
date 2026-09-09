"""Concept note builders: the curated concept graph → ``vault/Concepts/`` (plan §36.2).

Layout:

    Concepts/Concept Map.md            # index, study order, prerequisite diagram
    Concepts/<Concept Title>.md        # one note per concept

Concept notes are generated (``generated: true``) from the committed
``data/enrichment/concepts.json``; the *content* is curated, the *files*
are compiled output, so a rebuild may rewrite or prune them. They contain
no official text: only the curator's description and links into the
authoritative corpora, each visibly marked as a study aid.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from far_aim.generate import BuildError
from far_aim.generate.frontmatter import Value
from far_aim.generate.markdown import escape_md
from far_aim.generate.notes import Note, link_display
from far_aim.links.concepts import Concept, ConceptGraph

CONCEPTS_DIR = "Concepts"
CONCEPT_MAP_STEM = "Concept Map"
CONCEPTS_SOURCE = "data/enrichment/concepts.json"


@dataclass(frozen=True)
class ConceptTargets:
    """Link targets a concept may reference: stem → display text per corpus,
    plus every stem in the vault namespace (for ``see_also``)."""

    far: dict[str, str] = field(default_factory=dict)
    aim: dict[str, str] = field(default_factory=dict)
    pcg: dict[str, str] = field(default_factory=dict)
    all_stems: frozenset[str] = field(default_factory=frozenset)


def concept_id(concept: Concept) -> str:
    return f"concept-{concept.id}"


def concept_path(concept: Concept) -> tuple[str, ...]:
    return (CONCEPTS_DIR, f"{concept.title}.md")


def verify_targets(graph: ConceptGraph, targets: ConceptTargets) -> None:
    """Every reference must resolve in the accepted editions (plan §36.2)."""
    corpora = (("far", targets.far), ("aim", targets.aim), ("pcg", targets.pcg))
    for concept in graph.concepts:
        for key, known in corpora:
            for ref in getattr(concept, key):
                if ref not in known:
                    raise BuildError(
                        f"concept {concept.id!r} references {key.upper()} note {ref!r}, "
                        "which the accepted editions do not define; fix "
                        f"{CONCEPTS_SOURCE}"
                    )
        for ref in concept.see_also:
            if ref not in targets.all_stems:
                raise BuildError(
                    f"concept {concept.id!r} see_also names {ref!r}, which is neither a "
                    f"generated note, a concept, nor a curated note on disk; fix "
                    f"{CONCEPTS_SOURCE}"
                )


def _curated_callout(area: str, kind: str) -> str:
    return "\n".join(
        [
            f"> [!note] {kind}",
            f"> A study aid from the curated concept graph (`{CONCEPTS_SOURCE}`), "
            f"not official text. Area: {escape_md(area)}.",
        ]
    )


def _link_list(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"- [[{stem}|{link_display(display)}]]" if display != stem else f"- [[{stem}]]"
        for stem, display in pairs
    )


def _title_links(graph: ConceptGraph, ids: tuple[str, ...]) -> str:
    return _link_list([(graph.by_id[i].title, graph.by_id[i].title) for i in ids])


def build_concept_note(
    concept: Concept, graph: ConceptGraph, targets: ConceptTargets
) -> Note:
    chunks = [
        f"# {escape_md(concept.title)}",
        _curated_callout(concept.area, "Curated concept"),
    ]
    if concept.description:
        chunks.append(escape_md(concept.description))
    if concept.prerequisites:
        chunks.append("## Prerequisites")
        chunks.append(_title_links(graph, concept.prerequisites))
    dependents = graph.dependents.get(concept.id, ())
    if dependents:
        chunks.append("## Builds on this")
        chunks.append(_title_links(graph, dependents))
    for heading, refs, known in (
        ("## Regulations", concept.far, targets.far),
        ("## AIM guidance", concept.aim, targets.aim),
        ("## Glossary", concept.pcg, targets.pcg),
    ):
        if refs:
            chunks.append(heading)
            chunks.append(_link_list([(ref, known[ref]) for ref in refs]))
    see_also: list[tuple[str, str]] = [(CONCEPT_MAP_STEM, CONCEPT_MAP_STEM)]
    see_also.extend((ref, ref) for ref in concept.see_also)
    chunks.append("## See also")
    chunks.append(_link_list(see_also))

    frontmatter: list[tuple[str, Value]] = [
        ("id", concept_id(concept)),
        ("type", "concept"),
        ("area", concept.area),
        ("generated", True),
        ("title", concept.title),
        ("tags", ["concept", "study"]),
    ]
    return Note(
        kind="concept",
        path_parts=concept_path(concept),
        frontmatter=frontmatter,
        body="\n\n".join(chunks) + "\n",
    )


def _mermaid_id(concept: Concept) -> str:
    return "c_" + concept.id.replace("-", "_")


def _mermaid_label(title: str) -> str:
    return title.replace('"', "#quot;")


def build_concept_map(graph: ConceptGraph) -> Note:
    chunks = [
        f"# {CONCEPT_MAP_STEM}",
        "\n".join(
            [
                "> [!note] Curated concept graph",
                f"> Study concepts and the order they build on each other, from "
                f"`{CONCEPTS_SOURCE}`. Every concept links the regulations, AIM "
                "paragraphs and glossary terms that define it; nothing here is "
                "official text.",
            ]
        ),
        "## Start here",
        "Concepts with no prerequisites:",
        _link_list([(c.title, c.title) for c in graph.roots()]),
        "## By area",
    ]
    for area, concepts in graph.areas():
        chunks.append(f"### {escape_md(area)}")
        chunks.append(_link_list([(c.title, c.title) for c in concepts]))
    chunks.append("## Suggested order")
    chunks.append("Prerequisites always come first; otherwise alphabetical:")
    chunks.append(
        "\n".join(
            f"{position}. [[{concept.title}]]"
            for position, concept in enumerate(graph.study_order(), start=1)
        )
    )
    chunks.append("## Prerequisite diagram")
    lines = ["```mermaid", "graph TD"]
    for concept in graph.concepts:
        lines.append(f'  {_mermaid_id(concept)}["{_mermaid_label(concept.title)}"]')
    for concept in graph.concepts:
        for prerequisite in concept.prerequisites:
            lines.append(
                f"  {_mermaid_id(graph.by_id[prerequisite])} --> {_mermaid_id(concept)}"
            )
    lines.append("```")
    chunks.append("\n".join(lines))

    return Note(
        kind="concept_index",
        path_parts=(CONCEPTS_DIR, f"{CONCEPT_MAP_STEM}.md"),
        frontmatter=[
            ("id", "concept-map"),
            ("type", "concept-index"),
            ("generated", True),
            ("title", CONCEPT_MAP_STEM),
            ("tags", ["concept", "study"]),
        ],
        body="\n\n".join(chunks) + "\n",
    )
