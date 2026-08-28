"""Deterministic YAML frontmatter emission and schema checking (plan §10, §17.4).

Frontmatter is hand-emitted rather than produced by a YAML library: the
zero-diff rebuild invariant (plan §32.10) needs an exact byte format that no
dependency's style choices can drift. The value space is deliberately tiny —
strings, ints, bools, and lists of strings. Every string is double-quoted via
JSON escaping (a valid YAML double-quoted scalar), which sidesteps YAML's
plain-scalar pitfalls (``91.155`` is a float, ``2026-08-19`` a date) with one
uniform rule.
"""

from __future__ import annotations

import json

Scalar = str | int | bool
Value = Scalar | list[str]

# Note kinds and their frontmatter contracts: (canonical key order, required
# keys). This is the "schema-checked frontmatter" gate of the Phase 3 exit
# criteria, enforced at build time — a defect fails the build before anything
# is written (plan §32.13).
_REGULATION_KEYS = (
    "id",
    "type",
    "citation",
    "title_number",
    "part",
    "section",
    "source",
    "source_version",
    "canonical_hash",
    "generated",
    "title",
    "aliases",
    "tags",
)
_APPENDIX_KEYS = tuple(key if key != "section" else "appendix" for key in _REGULATION_KEYS)
_INDEX_KEYS = tuple(key for key in _REGULATION_KEYS if key != "section")

SCHEMAS: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    "regulation": (_REGULATION_KEYS, frozenset(_REGULATION_KEYS) - {"aliases", "tags"}),
    "appendix": (_APPENDIX_KEYS, frozenset(_APPENDIX_KEYS) - {"aliases", "tags"}),
    "index": (_INDEX_KEYS, frozenset(_INDEX_KEYS) - {"part", "aliases", "tags"}),
    "status": (
        ("id", "type", "generated", "title"),
        frozenset({"id", "type", "generated", "title"}),
    ),
}

_KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "id": str,
    "type": str,
    "citation": str,
    "title_number": int,
    "part": (int, str),
    "section": str,
    "appendix": str,
    "source": str,
    "source_version": str,
    "canonical_hash": str,
    "generated": bool,
    "title": str,
    "aliases": list,
    "tags": list,
}


def frontmatter_defect(kind: str, items: list[tuple[str, Value]]) -> str | None:
    """First schema violation in ``items`` for note ``kind``, or None."""
    schema = SCHEMAS.get(kind)
    if schema is None:
        return f"unknown note kind {kind!r}"
    ordered, required = schema
    keys = [key for key, _ in items]
    if len(keys) != len(set(keys)):
        return "duplicate frontmatter key"
    for key in required:
        if key not in keys:
            return f"missing required key {key!r}"
    for key, value in items:
        if key not in ordered:
            return f"key {key!r} not allowed for kind {kind!r}"
        expected = _KEY_TYPES[key]
        # bool is an int subclass; keep the check exact.
        if isinstance(value, bool) and expected is not bool:
            return f"key {key!r}: expected {expected}, got bool"
        if not isinstance(value, expected):
            return f"key {key!r}: expected {expected}, got {type(value).__name__}"
        if isinstance(value, list):
            if not value:
                return f"key {key!r}: empty list (omit the key instead)"
            if not all(isinstance(item, str) for item in value):
                return f"key {key!r}: list items must be strings"
    if keys != [key for key in ordered if key in keys]:
        return "keys out of schema order"
    return None


def _scalar(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def emit_frontmatter(items: list[tuple[str, Value]]) -> str:
    """Render ``items`` (already schema-checked) as a ``---`` YAML block."""
    lines = ["---"]
    for key, value in items:
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"
