"""Tier 3 curated concept graph (plan §12.3, §36.2).

``data/enrichment/concepts.json`` is human-authored: study concepts, the
concepts each one presupposes, and the authoritative notes that define it.
This module parses and shape-checks the file and derives what a curator
never writes by hand — reverse edges ("builds on this"), the roots, and a
deterministic study order. Reference resolution against the generated
vault (does ``91.155`` exist in the accepted edition?) is the generator's
job, because only it knows the stem namespace.

Fail-closed like the glossary gate: a malformed file, an unknown key, a
duplicate id or title, a dangling prerequisite or a cycle is a build error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.generate import BuildError, naming

SCHEMA_VERSION = 1
CONCEPT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_FIELDS = frozenset(
    {"id", "title", "area", "description", "prerequisites", "far", "aim", "pcg", "see_also"}
)
_REQUIRED = ("id", "title", "area")
_LIST_FIELDS = ("prerequisites", "far", "aim", "pcg", "see_also")


@dataclass(frozen=True)
class Concept:
    id: str
    title: str
    area: str
    description: str = ""
    prerequisites: tuple[str, ...] = ()
    far: tuple[str, ...] = ()
    aim: tuple[str, ...] = ()
    pcg: tuple[str, ...] = ()
    see_also: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConceptGraph:
    """Concepts in file order plus the derived reverse edges."""

    concepts: tuple[Concept, ...]
    by_id: dict[str, Concept] = field(default_factory=dict)
    dependents: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ConceptGraph:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BuildError(f"cannot read concept graph {path}: {exc}") from exc
        return cls.from_raw(raw, where=str(path))

    @classmethod
    def from_raw(cls, raw: object, *, where: str = "concept graph") -> ConceptGraph:
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
            raise BuildError(f"{where}: unsupported schema (expected {SCHEMA_VERSION})")
        unknown = sorted(set(raw) - {"schema", "_comment", "concepts"})
        if unknown:
            raise BuildError(f"{where}: unknown key(s) {', '.join(unknown)}")
        records = raw.get("concepts")
        if not isinstance(records, list) or not records:
            raise BuildError(f"{where}: concepts must be a non-empty list")
        concepts = tuple(_parse_concept(record, where) for record in records)
        return cls.from_concepts(concepts, where=where)

    @classmethod
    def from_concepts(cls, concepts: tuple[Concept, ...], *, where: str) -> ConceptGraph:
        by_id: dict[str, Concept] = {}
        titles: dict[str, str] = {}
        for concept in concepts:
            if concept.id in by_id:
                raise BuildError(f"{where}: duplicate concept id {concept.id!r}")
            by_id[concept.id] = concept
            folded = concept.title.casefold()
            if folded in titles:
                raise BuildError(
                    f"{where}: concept titles {titles[folded]!r} and {concept.title!r} "
                    "collide case-insensitively"
                )
            titles[folded] = concept.title
        dependents: dict[str, list[str]] = {concept.id: [] for concept in concepts}
        for concept in concepts:
            for prerequisite in concept.prerequisites:
                if prerequisite not in by_id:
                    raise BuildError(
                        f"{where}: concept {concept.id!r} names unknown prerequisite "
                        f"{prerequisite!r}"
                    )
                if prerequisite == concept.id:
                    raise BuildError(f"{where}: concept {concept.id!r} is its own prerequisite")
                dependents[prerequisite].append(concept.id)
        graph = cls(
            concepts=concepts,
            by_id=by_id,
            dependents={k: tuple(v) for k, v in dependents.items()},
        )
        graph._check_acyclic(where)
        return graph

    def _check_acyclic(self, where: str) -> None:
        remaining = {c.id: set(c.prerequisites) for c in self.concepts}
        while remaining:
            free = [cid for cid, deps in remaining.items() if not deps]
            if not free:
                cycle = ", ".join(sorted(remaining))
                raise BuildError(f"{where}: prerequisite cycle among {cycle}")
            for cid in free:
                del remaining[cid]
            for deps in remaining.values():
                deps.difference_update(free)

    def roots(self) -> list[Concept]:
        """Concepts with no prerequisites, in file order."""
        return [c for c in self.concepts if not c.prerequisites]

    def areas(self) -> list[tuple[str, list[Concept]]]:
        """(area, concepts) grouped in order of first appearance."""
        grouped: dict[str, list[Concept]] = {}
        for concept in self.concepts:
            grouped.setdefault(concept.area, []).append(concept)
        return list(grouped.items())

    def study_order(self) -> list[Concept]:
        """A topological order: prerequisites first, ties by title, then id."""
        remaining = {c.id: set(c.prerequisites) for c in self.concepts}
        order: list[Concept] = []
        while remaining:
            free = sorted(
                (cid for cid, deps in remaining.items() if not deps),
                key=lambda cid: (self.by_id[cid].title.casefold(), cid),
            )
            if not free:  # pragma: no cover — from_concepts already rejects cycles
                raise BuildError("prerequisite cycle")
            head = free[0]
            order.append(self.by_id[head])
            del remaining[head]
            for deps in remaining.values():
                deps.discard(head)
        return order


def _parse_concept(record: object, where: str) -> Concept:
    if not isinstance(record, dict):
        raise BuildError(f"{where}: every concept must be an object")
    unknown = sorted(set(record) - _FIELDS)
    label = record.get("id") if isinstance(record.get("id"), str) else "<no id>"
    if unknown:
        raise BuildError(f"{where}: concept {label!r} has unknown key(s) {', '.join(unknown)}")
    for key in _REQUIRED:
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise BuildError(f"{where}: concept {label!r} needs a non-empty string {key!r}")
    cid = record["id"]
    if not CONCEPT_ID_RE.match(cid):
        raise BuildError(f"{where}: concept id {cid!r} is not a slug (a-z, 0-9, hyphens)")
    title = record["title"]
    if not naming.STEM_RE.match(title) or title.endswith((" ", ".")) or title != title.strip():
        raise BuildError(f"{where}: concept {cid!r} title {title!r} violates the naming policy")
    description = record.get("description", "")
    if not isinstance(description, str) or description != description.strip():
        raise BuildError(f"{where}: concept {cid!r} description must be a trimmed string")
    lists: dict[str, tuple[str, ...]] = {}
    for key in _LIST_FIELDS:
        values = record.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(v, str) and v.strip() and v == v.strip() for v in values
        ):
            raise BuildError(
                f"{where}: concept {cid!r} {key!r} must be a list of trimmed, non-empty strings"
            )
        if len(set(values)) != len(values):
            raise BuildError(f"{where}: concept {cid!r} {key!r} has duplicates")
        lists[key] = tuple(values)
    return Concept(
        id=cid,
        title=title,
        area=record["area"].strip(),
        description=description,
        prerequisites=lists["prerequisites"],
        far=lists["far"],
        aim=lists["aim"],
        pcg=lists["pcg"],
        see_also=lists["see_also"],
    )
