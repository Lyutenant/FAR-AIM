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
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.config import Config
from far_aim.generate import BuildError, aim_notes, naming, notes, pcg_notes
from far_aim.generate.aim_markdown import collect_asset_names
from far_aim.generate.frontmatter import emit_frontmatter, frontmatter_defect
from far_aim.models import aim as aim_model
from far_aim.models import cfr as cfr_model
from far_aim.models import pcg as pcg_model
from far_aim.parsers import aim as aim_parser
from far_aim.parsers import ecfr as ecfr_parser
from far_aim.parsers import pcg as pcg_parser

_WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]|#]+)")
_GENERATED_MARKER = "generated: true"
# Binary assets carry no frontmatter, so generator ownership is recorded in
# a ledger inside the assets directory: filename → sha256 of the bytes the
# generator wrote. Only files the ledger lists (with unchanged bytes) are
# ever deleted or overwritten; anything else in the directory is curated.
ASSET_LEDGER = ".generated.json"
# The ledger proves its own ownership the way notes do with frontmatter: a
# file at the reserved path without this marker is curated and never touched.
_LEDGER_MARKER = "far_aim_asset_ledger"


@dataclass
class Registry:
    """Everything the render pass needs to link and alias globally."""

    section_numbers: set[str] = field(default_factory=set)
    part_numbers: set[str] = field(default_factory=set)
    stems: dict[str, tuple[str, ...]] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    section_count: int = 0
    appendix_count: int = 0
    # AIM: id → (stem, display) link targets, note count, asset filenames.
    aim_targets: dict[str, tuple[str, str]] = field(default_factory=dict)
    aim_note_count: int = 0
    aim_assets: set[str] = field(default_factory=set)
    # PCG: id → (stem, display) link targets and note count.
    pcg_targets: dict[str, tuple[str, str]] = field(default_factory=dict)
    pcg_note_count: int = 0


@dataclass(frozen=True)
class AimLayer:
    """The verified canonical AIM layer plus the archived figure bytes."""

    docs: dict[str, dict]
    title_hash: str
    assets: dict[str, bytes]


@dataclass(frozen=True)
class PcgLayer:
    """The verified canonical PCG layer."""

    docs: dict[str, dict]
    title_hash: str


def _iter_pcg_terms(docs: dict[str, dict]):
    """Term documents of the PCG layer, in letter/document order."""
    for key in sorted(docs):
        doc = docs[key]
        kind = doc.get("document_type")
        if kind == pcg_model.DOCUMENT_TYPE_LETTER:
            yield from doc["terms"]
        elif kind == pcg_model.DOCUMENT_TYPE_PUBLICATION:
            continue  # rendered into the PCG index note, not a note of its own
        else:
            raise BuildError(f"unknown PCG document type {kind!r} in {key!r}")


def _iter_aim_documents(docs: dict[str, dict]):
    """(kind, document) for every AIM note-bearing document, in citation order."""
    for key in sorted(docs, key=_aim_doc_key):
        doc = docs[key]
        kind = doc.get("document_type")
        if kind == aim_model.DOCUMENT_TYPE_CHAPTER:
            yield "chapter", doc
            for section in doc["sections"]:
                yield "section", section
                for para in section["paragraphs"]:
                    yield "paragraph", para
        elif kind == aim_model.DOCUMENT_TYPE_APPENDIX:
            yield "appendix", doc
        elif kind == aim_model.DOCUMENT_TYPE_PUBLICATION:
            continue  # rendered into the AIM index note, not a note of its own
        else:
            raise BuildError(f"unknown AIM document type {kind!r} in {key!r}")


def _aim_doc_key(key: str) -> tuple[int, int]:
    doc_kind, _, number = key.partition("-")
    if doc_kind == "publication":
        return (-1, 0)
    return (0 if doc_kind == "chapter" else 1, int(number))


def aim_asset_hashes(docs: dict[str, dict]) -> dict[str, str]:
    """Figure asset filename → sha256 recorded in the canonical AIM layer.

    The same file referenced with two different checksums is a defect: the
    vault can hold only one asset under that name.
    """
    hashes: dict[str, str] = {}

    def record(src: str, sha: str) -> None:
        name = src.rsplit("/", 1)[-1]
        if hashes.setdefault(name, sha) != sha:
            raise BuildError(f"AIM figure {name!r} is referenced with two different checksums")

    def walk(blocks: list[dict]) -> None:
        for block in blocks:
            kind = block["type"]
            if kind == "figure":
                record(block["image"]["source"]["src"], block["image"]["sha256"])
            elif kind == "image":
                record(block["source"]["src"], block["sha256"])
            elif kind == "list":
                for item in block["items"]:
                    walk(item["blocks"])
            elif kind == "note":
                walk(block["blocks"])
            elif kind == "table":
                for rows in (block["header_rows"], block["rows"], block["foot_rows"]):
                    for row in rows:
                        for cell in row:
                            walk(cell["blocks"])

    for kind, doc in _iter_aim_documents(docs):
        if kind != "chapter":
            walk(doc["content"])
    for blocks in _publication_blocks(docs):
        walk(blocks)
    return hashes


def _publication_blocks(docs: dict[str, dict]) -> list[list[dict]]:
    """Block lists of the ``aim_publication`` document (index front matter), if any."""
    for doc in docs.values():
        if doc.get("document_type") == aim_model.DOCUMENT_TYPE_PUBLICATION:
            return [doc["description"], doc["summary"]]
    return []


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


def _add_stem(
    registry: Registry,
    seen: dict[str, str],
    stem: str,
    parts: tuple[str, ...],
    *,
    pattern: re.Pattern[str] = naming.STEM_RE,
) -> None:
    if not pattern.match(stem) or stem.endswith((" ", ".")):
        raise BuildError(f"filename stem violates naming policy: {stem!r}")
    folded = stem.casefold()
    if folded in seen:
        raise BuildError(f"filename collision: {stem!r} vs {seen[folded]!r}")
    seen[folded] = stem
    registry.stems[stem] = parts


def build_registry(
    docs: dict[str, dict], aim: AimLayer | None = None, pcg: PcgLayer | None = None
) -> Registry:
    """Pass 1: stems, link targets, and alias candidates for every note.

    All corpora share one namespace: filename stems and aliases are checked
    for uniqueness across FAR, AIM and PCG together (plan §17.4).
    """
    registry = Registry()
    seen: dict[str, str] = {}
    citation_aliases: dict[str, list[str]] = {}
    heading_candidates: dict[str, str] = {}

    _add_stem(
        registry, seen, notes.TITLE_INDEX_STEM, (notes.FAR_DIR, f"{notes.TITLE_INDEX_STEM}.md")
    )
    _add_stem(registry, seen, notes.SOURCE_STATUS_STEM, (f"{notes.SOURCE_STATUS_STEM}.md",))
    if aim is not None:
        _register_aim(registry, seen, citation_aliases, heading_candidates, aim)
    if pcg is not None:
        _register_pcg(registry, seen, citation_aliases, heading_candidates, pcg)
    for part, doc in docs.items():
        folder = naming.part_folder_name(part)
        stem = naming.part_index_stem(part)
        _add_stem(registry, seen, stem, (notes.FAR_DIR, folder, f"{stem}.md"))
        registry.part_numbers.add(part)
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


def _register_aim(
    registry: Registry,
    seen: dict[str, str],
    citation_aliases: dict[str, list[str]],
    heading_candidates: dict[str, str],
    aim: AimLayer,
) -> None:
    index_stem = aim_notes.AIM_INDEX_STEM
    _add_stem(registry, seen, index_stem, (aim_notes.AIM_DIR, f"{index_stem}.md"))
    registry.aim_note_count += 1
    for kind, doc in _iter_aim_documents(aim.docs):
        registry.aim_note_count += 1
        if kind == "chapter":
            stem = naming.aim_chapter_stem(doc["chapter"])
            folder = naming.aim_chapter_folder(doc["chapter"])
            _add_stem(registry, seen, stem, (aim_notes.AIM_DIR, folder, f"{stem}.md"))
            registry.aim_targets[doc["id"]] = (stem, aim_notes.chapter_display(doc))
            citation_aliases[doc["id"]] = []
        elif kind == "section":
            stem = naming.aim_section_stem(doc["chapter"], doc["section"])
            folder = naming.aim_chapter_folder(doc["chapter"])
            _add_stem(registry, seen, stem, (aim_notes.AIM_DIR, folder, f"{stem}.md"))
            registry.aim_targets[doc["id"]] = (stem, aim_notes.section_display(doc))
            citation_aliases[doc["id"]] = []
            collect_asset_names(doc["content"], registry.aim_assets)
        elif kind == "paragraph":
            stem = naming.aim_paragraph_stem(doc["paragraph"])
            folder = naming.aim_chapter_folder(doc["chapter"])
            _add_stem(registry, seen, stem, (aim_notes.AIM_DIR, folder, f"{stem}.md"))
            registry.aim_targets[doc["id"]] = (stem, aim_notes.paragraph_display(doc))
            citation_aliases[doc["id"]] = [f"AIM {doc['paragraph']}"]
            collect_asset_names(doc["content"], registry.aim_assets)
        else:
            stem = naming.aim_appendix_stem(doc["appendix"])
            _add_stem(
                registry, seen, stem, (aim_notes.AIM_DIR, aim_notes.APPENDICES_DIR, f"{stem}.md")
            )
            registry.aim_targets[doc["id"]] = (stem, aim_notes.appendix_display(doc))
            citation_aliases[doc["id"]] = []
            collect_asset_names(doc["content"], registry.aim_assets)
        heading_candidates[doc["id"]] = doc["heading"]
    for blocks in _publication_blocks(aim.docs):
        collect_asset_names(blocks, registry.aim_assets)
    missing = sorted(registry.aim_assets - set(aim.assets))
    if missing:
        raise BuildError(f"AIM figures missing from the archived snapshot: {missing[:5]}")
    # Asset filenames are link targets too (``![[file]]`` embeds).
    for name in sorted(registry.aim_assets):
        _add_stem(
            registry,
            seen,
            name,
            (aim_notes.AIM_DIR, aim_notes.ASSETS_DIR, name),
            pattern=naming.ASSET_NAME_RE,
        )


def _register_pcg(
    registry: Registry,
    seen: dict[str, str],
    citation_aliases: dict[str, list[str]],
    heading_candidates: dict[str, str],
    pcg: PcgLayer,
) -> None:
    index_stem = pcg_notes.PCG_INDEX_STEM
    _add_stem(registry, seen, index_stem, (pcg_notes.PCG_DIR, f"{index_stem}.md"))
    registry.pcg_note_count += 1
    for term_doc in _iter_pcg_terms(pcg.docs):
        registry.pcg_note_count += 1
        term = term_doc["term"]
        stem = naming.pcg_term_stem(term)
        folder = naming.pcg_letter_folder(term_doc["letter"].lower())
        _add_stem(
            registry,
            seen,
            stem,
            (pcg_notes.PCG_DIR, folder, f"{stem}.md"),
            pattern=naming.PCG_STEM_RE,
        )
        registry.pcg_targets[term_doc["id"]] = (stem, pcg_notes.term_display(term_doc))
        citation_aliases[term_doc["id"]] = []
        # The verbatim term is an alias candidate when sanitization changed
        # it — filtered through the global collision rule like heading
        # aliases (plan §17.4): a reserved term's verbatim form can equal
        # another note's stem (the term ``AIM`` vs the AIM index note), and
        # an ambiguous alias must be dropped, not emitted.
        if term != stem:
            heading_candidates[term_doc["id"]] = term


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
        heading = heading_candidates.get(note_id)
        if heading is not None:
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
    docs: dict[str, dict],
    version: str,
    title_hash: str,
    sources: dict[str, object],
    aim: AimLayer | None = None,
    pcg: PcgLayer | None = None,
) -> dict[tuple[str, ...], bytes]:
    """Render the complete vault in memory and verify it (nothing written).

    The plan maps vault-relative path parts to bytes: Markdown notes plus,
    when an AIM layer is given, the archived figure assets it embeds.
    """
    registry = build_registry(docs, aim, pcg)
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
                add(
                    notes.build_section_note(
                        child, aliases, registry.section_numbers, registry.part_numbers
                    )
                )
            else:
                add(
                    notes.build_appendix_note(
                        child, aliases, registry.section_numbers, registry.part_numbers
                    )
                )
    add(notes.build_title_index(docs, version, title_hash))
    add(notes.build_source_status(sources))

    if aim is not None:
        targets = registry.aim_targets
        far = aim_notes.FarTargets(
            sections=frozenset(registry.section_numbers),
            parts=frozenset(registry.part_numbers),
        )
        for kind, doc in _iter_aim_documents(aim.docs):
            aliases = registry.aliases[doc["id"]]
            if kind == "chapter":
                add(aim_notes.build_chapter_note(doc, aliases))
            elif kind == "section":
                add(aim_notes.build_section_note(doc, aliases, targets, far))
            elif kind == "paragraph":
                add(aim_notes.build_paragraph_note(doc, aliases, targets, far))
            else:
                add(aim_notes.build_appendix_note(doc, aliases, targets, far))
        add(aim_notes.build_aim_index(aim.docs, aim.title_hash))
        generated = {name: aim.assets[name] for name in sorted(registry.aim_assets)}
        for name, data in generated.items():
            plan[(aim_notes.AIM_DIR, aim_notes.ASSETS_DIR, name)] = data
        plan[(aim_notes.AIM_DIR, aim_notes.ASSETS_DIR, ASSET_LEDGER)] = asset_ledger_bytes(
            generated
        )

    if pcg is not None:
        for term_doc in _iter_pcg_terms(pcg.docs):
            aliases = registry.aliases[term_doc["id"]]
            add(pcg_notes.build_term_note(term_doc, aliases, registry.pcg_targets))
        add(pcg_notes.build_pcg_index(pcg.docs, pcg.title_hash))

    _verify_plan(plan, registry, docs, aim, pcg)
    return plan


def is_note_path(parts: tuple[str, ...]) -> bool:
    return parts[-1].endswith(".md")


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def asset_ledger_bytes(assets: dict[str, bytes]) -> bytes:
    """Deterministic ledger content for the generated assets."""
    ledger = {name: _sha256(data) for name, data in sorted(assets.items())}
    payload = {_LEDGER_MARKER: 1, "generated": ledger}
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_ledger(path: Path) -> dict | None:
    """The parsed generator-owned ledger at ``path``, or None (absent, unreadable,
    or a file without the ownership marker — i.e. curated)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get(_LEDGER_MARKER) != 1:
        return None
    return data


def is_generated_ledger(path: Path) -> bool:
    """True only for a ledger the generator provably wrote."""
    return _load_ledger(path) is not None


def read_asset_ledger(path: Path) -> dict[str, str]:
    """Filename → sha256 recorded by an earlier build; empty unless the file
    is a generator-owned ledger."""
    data = _load_ledger(path)
    generated = data.get("generated") if data is not None else None
    if not isinstance(generated, dict):
        return {}
    return {k: v for k, v in generated.items() if isinstance(k, str) and isinstance(v, str)}


def _file_sha256(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes())
    except OSError:
        return None


def _verify_plan(
    plan: dict[tuple[str, ...], bytes],
    registry: Registry,
    docs: dict[str, dict],
    aim: AimLayer | None,
    pcg: PcgLayer | None = None,
) -> None:
    """Phase 3/4/5 exit-criteria gates, enforced before any write."""
    expected = len(docs) + registry.section_count + registry.appendix_count + 2
    if aim is not None:
        expected += registry.aim_note_count + len(registry.aim_assets) + 1  # + ledger
    if pcg is not None:
        expected += registry.pcg_note_count
    if len(plan) != expected:
        raise BuildError(f"planned {len(plan)} files, expected {expected}")
    parsed_sections = sum(ecfr_parser.count_sections(doc) for doc in docs.values())
    if registry.section_count != parsed_sections:
        raise BuildError(
            f"walked {registry.section_count} sections but the canonical layer "
            f"holds {parsed_sections}"
        )
    if aim is not None:
        parsed = sum(aim_parser.count_paragraphs(doc) for doc in aim.docs.values())
        walked = sum(1 for kind, _ in _iter_aim_documents(aim.docs) if kind == "paragraph")
        if walked != parsed:
            raise BuildError(
                f"walked {walked} AIM paragraphs but the canonical layer holds {parsed}"
            )
    if pcg is not None:
        parsed = sum(pcg_parser.count_terms(doc) for doc in pcg.docs.values())
        walked = registry.pcg_note_count - 1  # minus the PCG index note
        if walked != parsed:
            raise BuildError(
                f"walked {walked} PCG terms but the canonical layer holds {parsed}"
            )
    for parts, data in plan.items():
        if not is_note_path(parts):
            continue
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

    assets_root = vault / aim_notes.AIM_DIR / aim_notes.ASSETS_DIR
    ledger_path = assets_root / ASSET_LEDGER
    # What the previous build wrote into the assets directory: the only files
    # there the generator may overwrite or delete (plan §32.5).
    previous_ledger = read_asset_ledger(ledger_path)

    def owned_asset(path: Path) -> bool:
        recorded = previous_ledger.get(path.name)
        return recorded is not None and _file_sha256(path) == recorded

    # Refuse before writing anything if a curated file sits at a generated
    # path — regeneration must never overwrite user-authored material. Notes
    # prove ownership by frontmatter; assets by the ledger (bytes unchanged
    # since the generator wrote them). An asset that merely happens to hold
    # the same bytes is *not* adopted: recording it in the ledger would let a
    # later edition overwrite or prune a file the user placed there.
    for path, data in sorted(paths.items()):
        if not path.exists():
            continue
        if path.suffix == ".md":
            if path.read_bytes() != data and not is_generated_note(path):
                raise BuildError(
                    f"curated note at generated path {path}; move or rename it, then rebuild"
                )
        elif path == ledger_path:
            if path.read_bytes() != data and not is_generated_ledger(path):
                raise BuildError(
                    f"curated file at the reserved asset-ledger path {path}; move or rename "
                    "it, then rebuild"
                )
        elif path.parent == assets_root and not owned_asset(path):
            raise BuildError(
                f"curated or modified file at generated asset path {path}; move or rename "
                "it, then rebuild"
            )

    stats = SyncStats()
    owned_roots = [vault / notes.FAR_DIR, vault / aim_notes.AIM_DIR, vault / pcg_notes.PCG_DIR]
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
        if assets_root.is_dir():
            candidates.extend(
                p for p in sorted(assets_root.iterdir()) if p.is_file() and p.suffix != ".md"
            )
        for path in candidates:
            if path in paths:
                continue
            if path.parent == assets_root and path.suffix != ".md":
                if path == ledger_path:
                    if not is_generated_ledger(path):
                        stats.warnings.append(
                            f"curated file at the reserved asset-ledger path kept: {path}"
                        )
                        continue
                    generated_stale = True
                elif owned_asset(path):
                    generated_stale = True
                elif path.name in previous_ledger:
                    stats.warnings.append(f"modified generated asset kept: {path}")
                    continue
                else:
                    stats.warnings.append(f"curated file inside generated assets kept: {path}")
                    continue
            else:
                generated_stale = is_generated_note(path)
            if generated_stale:
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
    config: Config,
    version: str,
    title_hash: str,
    sources: dict[str, object],
    docs: dict[str, dict],
    aim: AimLayer | None = None,
    pcg: PcgLayer | None = None,
) -> SyncStats:
    """Plan, verify, and sync the whole vault; raises BuildError on any defect."""
    plan = plan_vault(docs, version, title_hash, sources, aim, pcg)
    return sync_vault(config, plan)
