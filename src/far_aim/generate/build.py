"""Vault build orchestration: plan, verify, sync (plan §22 Phase 3).

The build renders every note in memory first and verifies the whole plan —
link resolution, counts, filename policy, frontmatter schemas — before a
single byte is written (plan §32.13). Sync then writes only changed files,
deletes stale generated notes, and never touches curated material
(plan §32.5): a file is provably generator-owned only if its frontmatter
says ``generated: true``.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.config import Config
from far_aim.generate import BuildError, naming, notes
from far_aim.generate.frontmatter import emit_frontmatter, frontmatter_defect
from far_aim.models import cfr as cfr_model
from far_aim.parsers import ecfr as ecfr_parser

_WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]|#]+)")
_GENERATED_MARKER = "generated: true"


@dataclass
class Registry:
    """Everything the render pass needs to link and alias globally."""

    section_numbers: set[str] = field(default_factory=set)
    stems: dict[str, tuple[str, ...]] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    section_count: int = 0
    appendix_count: int = 0


def _iter_documents(part_doc: dict):
    """Sections and appendices of a part, in document order."""

    def walk(children: list[dict]):
        for child in children:
            doc_type = child.get("document_type")
            if doc_type in (cfr_model.DOCUMENT_TYPE_SECTION, cfr_model.DOCUMENT_TYPE_APPENDIX):
                yield child
            elif child.get("type") == "subpart":
                yield from walk(child["children"])
            elif child.get("type") == "subject_group":
                yield from walk(child["sections"])
            elif child.get("type") == "heading":
                continue
            else:
                kind = doc_type or child.get("type")
                raise BuildError(f"unknown part child {kind!r} in part {part_doc['part']!r}")

    yield from walk(part_doc["children"])


def _add_stem(registry: Registry, seen: dict[str, str], stem: str, parts: tuple[str, ...]) -> None:
    if not naming.STEM_RE.match(stem) or stem.endswith((" ", ".")):
        raise BuildError(f"filename stem violates naming policy: {stem!r}")
    folded = stem.casefold()
    if folded in seen:
        raise BuildError(f"filename collision: {stem!r} vs {seen[folded]!r}")
    seen[folded] = stem
    registry.stems[stem] = parts


def build_registry(docs: dict[str, dict]) -> Registry:
    """Pass 1: stems, link targets, and alias candidates for every note."""
    registry = Registry()
    seen: dict[str, str] = {}
    citation_aliases: dict[str, list[str]] = {}
    heading_candidates: dict[str, str] = {}

    _add_stem(
        registry, seen, notes.TITLE_INDEX_STEM, (notes.FAR_DIR, f"{notes.TITLE_INDEX_STEM}.md")
    )
    _add_stem(registry, seen, notes.SOURCE_STATUS_STEM, (f"{notes.SOURCE_STATUS_STEM}.md",))
    for part, doc in docs.items():
        folder = naming.part_folder_name(part)
        stem = naming.part_index_stem(part)
        _add_stem(registry, seen, stem, (notes.FAR_DIR, folder, f"{stem}.md"))
        for child in _iter_documents(doc):
            if child["document_type"] == cfr_model.DOCUMENT_TYPE_SECTION:
                registry.section_count += 1
                section = child["section"]
                stem = naming.section_stem(section)
                _add_stem(registry, seen, stem, (notes.FAR_DIR, folder, f"{stem}.md"))
                registry.section_numbers.add(section)
                cite: list[str] = []
                marker = child["head_marker"]
                if marker:
                    cite.append(f"{marker} {section}")
                elif notes.is_global_numbering(section, part):
                    cite.append(f"§ {section}")
                if notes.is_global_numbering(section, part):
                    cite.append(f"14 CFR {section}")
                citation_aliases[child["id"]] = cite
                heading_candidates[child["id"]] = notes.display_heading(child["heading"])
            else:
                registry.appendix_count += 1
                stem = naming.appendix_stem(part, child["id"])
                _add_stem(registry, seen, stem, (notes.FAR_DIR, folder, f"{stem}.md"))
                citation_aliases[child["id"]] = []
                heading_candidates[child["id"]] = notes.display_heading(child["heading"])

    _resolve_alias_collisions(registry, citation_aliases, heading_candidates)
    return registry


def _resolve_alias_collisions(
    registry: Registry,
    citation_aliases: dict[str, list[str]],
    heading_candidates: dict[str, str],
) -> None:
    """Global alias uniqueness (plan §17.4): ambiguous heading aliases drop.

    Citation-form aliases are collision-free by section-number uniqueness;
    a heading alias survives only if no other note claims the same name.
    """
    taken = {stem.casefold() for stem in registry.stems}
    for aliases in citation_aliases.values():
        taken.update(alias.casefold() for alias in aliases)
    counts: dict[str, int] = {}
    for candidate in heading_candidates.values():
        folded = candidate.casefold()
        counts[folded] = counts.get(folded, 0) + 1
    for note_id, cite in citation_aliases.items():
        aliases = list(cite)
        heading = heading_candidates[note_id]
        folded = heading.casefold()
        if counts[folded] == 1 and folded not in taken:
            aliases.append(heading)
        registry.aliases[note_id] = aliases


def _assemble(note: notes.Note) -> bytes:
    defect = frontmatter_defect(note.kind, note.frontmatter)
    if defect is not None:
        raise BuildError(f"{'/'.join(note.path_parts)}: frontmatter: {defect}")
    text = emit_frontmatter(note.frontmatter) + "\n" + note.body
    if "\r" in text:
        raise BuildError(f"{'/'.join(note.path_parts)}: carriage return in output")
    return text.encode("utf-8")


def plan_vault(
    docs: dict[str, dict], version: str, title_hash: str, sources: dict[str, object]
) -> dict[tuple[str, ...], bytes]:
    """Render the complete vault in memory and verify it (nothing written)."""
    registry = build_registry(docs)
    plan: dict[tuple[str, ...], bytes] = {}

    def add(note: notes.Note) -> None:
        if note.path_parts in plan:
            raise BuildError(f"duplicate note path {'/'.join(note.path_parts)}")
        plan[note.path_parts] = _assemble(note)

    for part in sorted(docs, key=naming.part_sort_key):
        doc = docs[part]
        add(notes.build_part_index(doc))
        for child in _iter_documents(doc):
            aliases = registry.aliases[child["id"]]
            if child["document_type"] == cfr_model.DOCUMENT_TYPE_SECTION:
                add(notes.build_section_note(child, aliases, registry.section_numbers))
            else:
                add(notes.build_appendix_note(child, aliases, registry.section_numbers))
    add(notes.build_title_index(docs, version, title_hash))
    add(notes.build_source_status(sources))

    _verify_plan(plan, registry, docs)
    return plan


def _verify_plan(
    plan: dict[tuple[str, ...], bytes], registry: Registry, docs: dict[str, dict]
) -> None:
    """Phase 3 exit-criteria gates, enforced before any write."""
    expected = len(docs) + registry.section_count + registry.appendix_count + 2
    if len(plan) != expected:
        raise BuildError(f"planned {len(plan)} notes, expected {expected}")
    parsed_sections = sum(ecfr_parser.count_sections(doc) for doc in docs.values())
    if registry.section_count != parsed_sections:
        raise BuildError(
            f"walked {registry.section_count} sections but the canonical layer "
            f"holds {parsed_sections}"
        )
    for parts, data in plan.items():
        body = data.decode("utf-8")
        for target in _WIKILINK_TARGET_RE.findall(body):
            if target not in registry.stems:
                raise BuildError(f"{'/'.join(parts)}: broken generated link [[{target}]]")


@dataclass
class SyncStats:
    written: int = 0
    unchanged: int = 0
    deleted: int = 0
    warnings: list[str] = field(default_factory=list)


def write_text_atomic(path: Path, data: bytes) -> None:
    """Atomic write with world-readable permissions (vault notes are content,
    not secrets; mkstemp alone would leave them 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def is_generated_note(path: Path) -> bool:
    """A note is generator-owned only if its frontmatter says so."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.rstrip("\n") != "---":
                return False
            for _ in range(64):
                line = fh.readline()
                if not line or line.rstrip("\n") == "---":
                    return False
                if line.rstrip("\n") == _GENERATED_MARKER:
                    return True
    except OSError:
        return False
    return False


def _backup_into(backup_root: Path, index: int, path: Path) -> Path:
    """Preserve ``path``'s current content under the backup dir (hard link
    when possible — os.replace unlinks the original name, not the inode)."""
    backup = backup_root / f"{index}-{path.name}"
    try:
        os.link(path, backup)
    except OSError:
        shutil.copy2(path, backup)
    return backup


def _prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if Path(dirpath) == root:
            continue
        # rmdir refuses non-empty directories — curated content stays.
        with contextlib.suppress(OSError):
            os.rmdir(dirpath)


def sync_vault(config: Config, plan: dict[tuple[str, ...], bytes]) -> SyncStats:
    """Write the verified plan into ``vault/``, curated notes untouched.

    Every mutation is journaled against a backup so that a mid-sync
    filesystem failure (full disk, permissions) rolls the vault back to its
    previous state instead of leaving a mixed partial tree — the last
    known-good output is preserved on failure (plan §32.13).
    """
    vault = config.vault_dir
    paths = {vault.joinpath(*parts): data for parts, data in plan.items()}

    # Refuse before writing anything if a curated file sits at a generated
    # path — regeneration must never overwrite user-authored notes.
    for path, data in sorted(paths.items()):
        if path.exists() and path.read_bytes() != data and not is_generated_note(path):
            raise BuildError(
                f"curated note at generated path {path}; move or rename it, then rebuild"
            )

    stats = SyncStats()
    owned_roots = [vault / notes.FAR_DIR]
    vault.mkdir(parents=True, exist_ok=True)
    backup_root = Path(tempfile.mkdtemp(dir=vault, prefix=".sync-backup-"))
    # (action, live path, backup path or None) — replayed in reverse on failure.
    journal: list[tuple[str, Path, Path | None]] = []
    try:
        for path, data in sorted(paths.items()):
            if path.exists():
                if path.read_bytes() == data:
                    stats.unchanged += 1
                    continue
                journal.append(("overwrite", path, _backup_into(backup_root, len(journal), path)))
            else:
                journal.append(("create", path, None))
            write_text_atomic(path, data)
            stats.written += 1

        # Stale generated notes (an upstream removal) go; anything else stays.
        candidates = [
            path
            for root in owned_roots
            if root.is_dir()
            for path in sorted(root.rglob("*.md"))
        ]
        status_path = vault / f"{notes.SOURCE_STATUS_STEM}.md"
        if status_path.exists():
            candidates.append(status_path)
        for path in candidates:
            if path in paths:
                continue
            if is_generated_note(path):
                backup = backup_root / f"{len(journal)}-{path.name}"
                os.rename(path, backup)
                journal.append(("delete", path, backup))
                stats.deleted += 1
            else:
                stats.warnings.append(f"curated note inside generated tree kept: {path}")
    except BaseException:
        for action, path, backup in reversed(journal):
            with contextlib.suppress(OSError):
                if action == "create":
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, path)
        for root in owned_roots:
            _prune_empty_dirs(root)
        shutil.rmtree(backup_root, ignore_errors=True)
        raise
    shutil.rmtree(backup_root, ignore_errors=True)

    for root in owned_roots:
        _prune_empty_dirs(root)
    return stats


def build_vault(
    config: Config, version: str, title_hash: str, sources: dict[str, object], docs: dict[str, dict]
) -> SyncStats:
    """Plan, verify, and sync the whole vault; raises BuildError on any defect."""
    plan = plan_vault(docs, version, title_hash, sources)
    return sync_vault(config, plan)
