"""Phase 3 tests: canonical JSON → Obsidian vault notes.

Golden files under ``tests/fixtures/vault/`` are the committed expected
Markdown for representative notes generated from ``part-91-slice.xml``
(verbatim official text; see the fixture rule in test_ecfr_parser.py).
Byte-comparison locks the full rendering pipeline: frontmatter emission,
block renderers, escaping, citation links, and index composition.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from far_aim.config import Config
from far_aim.generate import BuildError, naming
from far_aim.generate.build import (
    SyncStats,
    build_registry,
    plan_vault,
    sync_vault,
)
from far_aim.generate.frontmatter import emit_frontmatter, frontmatter_defect
from far_aim.generate.markdown import escape_md, render_blocks
from far_aim.models import cfr as cfr_model
from far_aim.parsers import ecfr as parser
from tests.test_ecfr_parser import SLICE_PATH, SOURCE

VAULT_FIXTURES = Path(__file__).parent / "fixtures" / "vault"


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def test_part_folder_padding():
    assert naming.part_folder_name("91") == "Part 091"
    assert naming.part_folder_name("1") == "Part 001"
    assert naming.part_folder_name("1310") == "Part 1310"
    assert naming.part_folder_name("50-59") == "Part 050-059"
    assert naming.part_folder_name("374a") == "Part 374a"
    assert naming.part_folder_name("1203a") == "Part 1203a"


def test_part_index_stem_unpadded():
    assert naming.part_index_stem("91") == "Part 91"
    assert naming.part_index_stem("50-59") == "Part 50-59"


def test_appendix_stems_all_shapes():
    assert naming.appendix_stem("91", "cfr-14-part-91-appendix-A") == "Part 91 Appendix A"
    assert naming.appendix_stem("91", "cfr-14-part-91-sfar-50-2") == "Part 91 SFAR 50-2"
    assert naming.appendix_stem("13", "cfr-14-part-13-appendixes-B-C") == "Part 13 Appendixes B-C"
    # Fallback slugs are self-describing and become the stem verbatim.
    assert (
        naming.appendix_stem("117", "cfr-14-part-117-Table-A-to-Part-117")
        == "Table-A-to-Part-117"
    )
    assert (
        naming.appendix_stem("93", "cfr-14-part-93-Appendix-A-to-Subpart-U-of-Part-93")
        == "Appendix-A-to-Subpart-U-of-Part-93"
    )


def test_appendix_label():
    assert naming.appendix_label("91", "cfr-14-part-91-appendix-A") == "Appendix A"
    assert naming.appendix_label("91", "cfr-14-part-91-sfar-50-2") == "SFAR 50-2"


def test_appendix_stem_wrong_part_rejected():
    with pytest.raises(ValueError):
        naming.appendix_stem("92", "cfr-14-part-91-appendix-A")


def test_part_sort_key():
    parts = ["121", "1203a", "13", "50-59", "1", "374a"]
    assert sorted(parts, key=naming.part_sort_key) == [
        "1",
        "13",
        "50-59",
        "121",
        "374a",
        "1203a",
    ]


def test_natural_key_orders_sections_numerically():
    sections = ["91.155", "91.20", "91.3", "91.27-91.99"]
    ordered = sorted(sections, key=naming.natural_key)
    assert ordered == ["91.3", "91.20", "91.27-91.99", "91.155"]


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def _regulation_items():
    return [
        ("id", "cfr-14-91.155"),
        ("type", "regulation"),
        ("citation", "14 CFR § 91.155"),
        ("title_number", 14),
        ("part", 91),
        ("section", "91.155"),
        ("source", "ecfr"),
        ("source_version", "2026-08-19"),
        ("canonical_hash", "sha256:abc"),
        ("generated", True),
        ("title", "Basic VFR weather minimums"),
        ("aliases", ["§ 91.155"]),
        ("tags", ["far", "regulation"]),
    ]


def test_frontmatter_emission_quoting():
    text = emit_frontmatter(_regulation_items())
    assert text.startswith("---\n") and text.endswith("---\n")
    # Strings that YAML would misread as numbers/dates must be quoted.
    assert 'section: "91.155"' in text
    assert 'source_version: "2026-08-19"' in text
    # Ints and bools stay bare.
    assert "part: 91\n" in text
    assert "generated: true\n" in text
    assert '  - "§ 91.155"' in text


def test_frontmatter_schema_accepts_valid():
    assert frontmatter_defect("regulation", _regulation_items()) is None


def test_frontmatter_schema_rejects_missing_required():
    items = [item for item in _regulation_items() if item[0] != "canonical_hash"]
    assert "canonical_hash" in frontmatter_defect("regulation", items)


def test_frontmatter_schema_rejects_unknown_key():
    items = [*_regulation_items(), ("retrieved_at", "2026-08-20T10:05:13Z")]
    assert "not allowed" in frontmatter_defect("regulation", items)


def test_frontmatter_schema_rejects_wrong_type():
    items = [(k, 91155) if k == "section" else (k, v) for k, v in _regulation_items()]
    assert "expected" in frontmatter_defect("regulation", items)


def test_frontmatter_schema_rejects_out_of_order():
    items = _regulation_items()
    items[0], items[1] = items[1], items[0]
    assert frontmatter_defect("regulation", items) == "keys out of schema order"


def test_frontmatter_schema_rejects_empty_list():
    items = [(k, []) if k == "aliases" else (k, v) for k, v in _regulation_items()]
    assert "empty list" in frontmatter_defect("regulation", items)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def test_escape_md_reserved_and_syntax():
    assert escape_md("[Reserved]") == "\\[Reserved]"
    assert escape_md("a * b _ c ` d < e") == "a \\* b \\_ c \\` d \\< e"
    assert escape_md("# not a heading") == "\\# not a heading"
    assert escape_md("- not a list") == "\\- not a list"
    assert escape_md("1. not ordered") == "1\\. not ordered"
    assert escape_md("100 feet") == "100 feet"


def test_paragraph_renders_flat_with_label_and_subject():
    blocks = [
        {
            "type": "paragraph",
            "label": "(a)",
            "designator": "a",
            "subject": "Applicability.",
            "text": "This section applies.",
            "children": [
                {
                    "type": "paragraph",
                    "label": "(1)",
                    "designator": "1",
                    "subject": None,
                    "text": "To everyone.",
                    "children": [],
                }
            ],
        }
    ]
    assert render_blocks(blocks) == [
        "**(a)** *Applicability.* This section applies.",
        "**(1)** To everyone.",
    ]


def test_paragraph_null_label():
    blocks = [{"type": "paragraph", "label": None, "designator": None, "subject": None,
               "text": "Run-in text.", "children": []}]
    assert render_blocks(blocks) == ["Run-in text."]


def test_definition_renders_term_italic():
    blocks = [{"type": "definition", "term": "Administrator", "text": "means the FAA.",
               "children": []}]
    assert render_blocks(blocks) == ["*Administrator* means the FAA."]


def test_text_styles():
    assert render_blocks([{"type": "text", "style": "flush-2", "text": "Flush text."}]) == [
        "Flush text."
    ]
    assert render_blocks([{"type": "text", "style": "table-caption", "text": "Table 1"}]) == [
        "*Table 1*"
    ]


def test_heading_levels_stay_below_official_text():
    assert render_blocks([{"type": "heading", "level": 1, "text": "A23.1 General"}]) == [
        "### A23.1 General"
    ]
    assert render_blocks([{"type": "heading", "level": 5, "text": "Deep"}]) == ["###### Deep"]
    with pytest.raises(BuildError):
        render_blocks([{"type": "heading", "level": 4, "text": "?"}])


def test_example_block():
    assert render_blocks([{"type": "example", "heading": "Example 1.", "text": "A case."}]) == [
        "*Example 1.* A case."
    ]


def test_image_renders_external_link_not_embed():
    out = render_blocks([{"type": "image", "src": "/graphics/er18fe98.004.gif"}])
    assert out == [
        "[eCFR graphic er18fe98.004.gif](https://www.ecfr.gov/graphics/er18fe98.004.gif)"
    ]
    assert not out[0].startswith("!")


def test_math_renders_links_and_text():
    out = render_blocks(
        [{"type": "math", "images": ["/graphics/er26no02.001.gif"], "text": "E = mc2"}]
    )
    assert out == [
        "[eCFR graphic er26no02.001.gif](https://www.ecfr.gov/graphics/er26no02.001.gif)",
        "E = mc2",
    ]


def test_note_renders_callout_with_quoted_body():
    out = render_blocks(
        [
            {
                "type": "note",
                "heading": "Note:",
                "blocks": [{"type": "text", "style": "plain", "text": "Body line."}],
            }
        ]
    )
    assert out == ["> [!note] Note:\n> Body line."]


def test_extract_quotes_nested_blocks():
    out = render_blocks(
        [
            {
                "type": "extract",
                "blocks": [
                    {"type": "text", "style": "plain", "text": "First."},
                    {"type": "text", "style": "plain", "text": "Second."},
                ],
            }
        ]
    )
    assert out == ["> First.\n>\n> Second."]


def test_footnote_quotes_blocks():
    out = render_blocks(
        [{"type": "footnote", "blocks": [{"type": "text", "style": "plain", "text": "Fn."}]}]
    )
    assert out == ["> Fn."]


def test_unknown_block_type_fails_loudly():
    with pytest.raises(BuildError):
        render_blocks([{"type": "mystery", "text": "?"}])


_SIMPLE_TABLE = {
    "type": "table",
    "caption": None,
    "header_rows": [[{"header": True, "text": "A"}, {"header": True, "text": "B"}]],
    "rows": [[{"text": "1"}, {"text": "with | pipe"}]],
    "foot_rows": [],
}


def test_simple_table_renders_pipes():
    assert render_blocks([dict(_SIMPLE_TABLE)]) == [
        "| A | B |\n| --- | --- |\n| 1 | with \\| pipe |"
    ]


def test_table_caption_precedes_table():
    table = dict(_SIMPLE_TABLE, caption="Weather minimums")
    out = render_blocks([table])
    assert out[0] == "*Weather minimums*"
    assert out[1].startswith("| A |")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t["rows"][0][0].update(colspan="2"),
        lambda t: t["rows"][0][0].update(rowspan="2"),
        lambda t: t["header_rows"].append([{"text": "H2a"}, {"text": "H2b"}]),
        lambda t: t["foot_rows"].append([{"text": "F"}, {"text": "F"}]),
        lambda t: t["rows"].append([{"text": "ragged"}]),
        lambda t: t["rows"][0][0].update(text="two\nlines"),
    ],
)
def test_unsafe_tables_fall_back_to_html(mutate):
    import copy

    table = copy.deepcopy(_SIMPLE_TABLE)
    mutate(table)
    (out,) = render_blocks([table])[-1:]
    assert out.startswith("<table>") and out.endswith("</table>")


def test_html_table_carries_spans_and_escapes():
    table = {
        "type": "table",
        "caption": None,
        "header_rows": [[{"header": True, "colspan": "2", "text": "A & B"}]],
        "rows": [[{"text": "<x>"}, {"text": "y"}]],
        "foot_rows": [],
    }
    (out,) = render_blocks([table])
    assert '<th colspan="2">A &amp; B</th>' in out
    assert "<td>&lt;x&gt;</td>" in out


# ---------------------------------------------------------------------------
# Registry and aliases
# ---------------------------------------------------------------------------


def _mini_section(part: str, section: str, heading: str) -> dict:
    return {
        "id": cfr_model.section_id(14, part, section),
        "document_type": "cfr_section",
        "section": section,
        "part": part,
        "subpart": None,
        "subject_group": None,
        "chapter": "I",
        "subchapter": "A",
        "title_number": 14,
        "head_marker": "§",
        "heading": heading,
        "reserved": False,
        "content": [],
        "citations": [],
        "approvals": [],
        "amendment_notes": [],
        "editorial_notes": [],
        "section_authority": None,
        "canonical_hash": "sha256:0",
        "source": dict(SOURCE),
    }


def _mini_part(part: str, sections: list[dict]) -> dict:
    return {
        "id": cfr_model.part_id(14, part),
        "document_type": "cfr_part",
        "part": part,
        "heading": f"PART {part}",
        "title_number": 14,
        "title_heading": "Title 14—Aeronautics and Space",
        "subtitle": None,
        "subtitle_heading": None,
        "chapter": "I",
        "chapter_heading": "CHAPTER I—FAA",
        "subchapter": "A",
        "subchapter_heading": "SUBCHAPTER A—TEST",
        "reserved": False,
        "authority": None,
        "source_note": None,
        "editorial_notes": [],
        "notes": [],
        "cross_references": [],
        "children": sections,
        "canonical_hash": "sha256:0",
        "source": dict(SOURCE),
    }


def test_shared_heading_alias_dropped_everywhere():
    docs = {
        "1": _mini_part("1", [_mini_section("1", "1.1", "Applicability.")]),
        "3": _mini_part("3", [_mini_section("3", "3.1", "Applicability.")]),
    }
    registry = build_registry(docs)
    assert registry.aliases["cfr-14-1.1"] == ["§ 1.1", "14 CFR 1.1"]
    assert registry.aliases["cfr-14-3.1"] == ["§ 3.1", "14 CFR 3.1"]


def test_unique_heading_alias_kept():
    docs = {"1": _mini_part("1", [_mini_section("1", "1.1", "Basic VFR weather minimums.")])}
    registry = build_registry(docs)
    assert registry.aliases["cfr-14-1.1"][-1] == "Basic VFR weather minimums"


def test_heading_alias_equal_to_stem_dropped():
    # A heading that collides with another note's filename stays out of aliases.
    docs = {"1": _mini_part("1", [_mini_section("1", "1.1", "Part 1.")])}
    registry = build_registry(docs)
    assert registry.aliases["cfr-14-1.1"] == ["§ 1.1", "14 CFR 1.1"]


def test_heading_alias_equal_to_curated_entry_dropped():
    # Curated entry stems (Home's link targets) are reserved in the alias
    # namespace even though the generator never writes those files.
    docs = {"1": _mini_part("1", [_mini_section("1", "1.1", "Collections.")])}
    registry = build_registry(docs)
    assert registry.aliases["cfr-14-1.1"] == ["§ 1.1", "14 CFR 1.1"]


def test_duplicate_stem_fails():
    part = _mini_part(
        "1",
        [_mini_section("1", "1.1", "One."), _mini_section("1", "1.1", "Two.")],
    )
    part["children"][1]["id"] = "cfr-14-1.1-dup"
    with pytest.raises(BuildError, match="collision"):
        build_registry({"1": part})


# ---------------------------------------------------------------------------
# Golden files (part-91-slice fixture → committed expected Markdown)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice_plan() -> dict[tuple[str, ...], bytes]:
    root = ET.parse(SLICE_PATH).getroot()
    docs = parser.build_part_docs(root, SOURCE)
    title_hash = cfr_model.canonical_hash(
        {number: doc["canonical_hash"] for number, doc in docs.items()}
    )
    from far_aim.manifest import SourceManifest, SourceState

    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(
        accepted_version="2026-08-19", canonical_hash=title_hash
    )
    return plan_vault(docs, "2026-08-19", title_hash, manifest.sources)


@pytest.mark.parametrize(
    "name",
    [
        "91.155.md",  # table, subjects, cross-references
        "91.175.md",  # deep nesting, RVR table
        "91.27-91.99.md",  # reserved §§ range
        "Part 91 SFAR 50-2.md",  # SFAR appendix
        "Part 91 Appendix D.md",  # lettered appendix
        "Part 91 Appendixes B-C.md",  # reserved appendix range
        "Part 91.md",  # part index
        "Title 14.md",  # title index
        "Source Status.md",  # manifest-derived status note
        "Home.md",  # vault entry point (FAR-only variant: no AIM/PCG links)
    ],
)
def test_golden_note(slice_plan, name):
    match = [data for parts, data in slice_plan.items() if parts[-1] == name]
    assert len(match) == 1, f"note {name} not planned exactly once"
    expected = (VAULT_FIXTURES / name).read_bytes()
    assert match[0] == expected


def test_slice_plan_shape(slice_plan):
    paths = set(slice_plan)
    assert ("FAR", "Title 14.md") in paths
    assert ("Source Status.md",) in paths
    assert ("Home.md",) in paths
    assert ("FAR", "Part 091", "Part 91.md") in paths
    assert ("FAR", "Part 091", "91.155.md") in paths
    # 1 part index + 11 sections + 4 appendices + title + status + home
    assert len(slice_plan) == 19


def test_cross_reference_only_links_in_corpus(slice_plan):
    # § 91.155(a) cites § 91.157 (present in the slice) and part 97 (absent).
    body = slice_plan[("FAR", "Part 091", "91.155.md")].decode()
    assert "- [[91.157|§ 91.157]]" in body
    assert "[[97" not in body


def test_no_volatile_timestamps_in_notes(slice_plan):
    for data in slice_plan.values():
        text = data.decode()
        assert "retrieved_at" not in text
        assert "2026-08-20T10:05:13Z" not in text


# ---------------------------------------------------------------------------
# Sync behavior
# ---------------------------------------------------------------------------


_GENERATED_NOTE = b"---\nid: \"x\"\ngenerated: true\n---\n\n# X\n"


def _plan_one(*parts: str, data: bytes = _GENERATED_NOTE) -> dict[tuple[str, ...], bytes]:
    return {tuple(parts): data}


def test_sync_writes_and_is_idempotent(tmp_path):
    config = Config.load(tmp_path)
    plan = _plan_one("FAR", "Part 001", "1.1.md")
    stats = sync_vault(config, plan)
    assert (stats.written, stats.unchanged, stats.deleted) == (1, 0, 0)
    note = config.vault_dir / "FAR" / "Part 001" / "1.1.md"
    assert note.read_bytes() == _GENERATED_NOTE
    assert (note.stat().st_mode & 0o777) == 0o644

    again = sync_vault(config, plan)
    assert (again.written, again.unchanged, again.deleted) == (0, 1, 0)


def test_sync_refuses_curated_note_at_generated_path(tmp_path):
    config = Config.load(tmp_path)
    target = config.vault_dir / "FAR" / "Part 001" / "1.1.md"
    target.parent.mkdir(parents=True)
    target.write_text("# My own note\n", encoding="utf-8")
    with pytest.raises(BuildError, match="curated note at generated path"):
        sync_vault(config, _plan_one("FAR", "Part 001", "1.1.md"))
    # Nothing was overwritten.
    assert target.read_text(encoding="utf-8") == "# My own note\n"


def test_sync_deletes_stale_generated_note(tmp_path):
    config = Config.load(tmp_path)
    stale = config.vault_dir / "FAR" / "Part 002" / "2.1.md"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(_GENERATED_NOTE)
    stats = sync_vault(config, _plan_one("FAR", "Part 001", "1.1.md"))
    assert stats.deleted == 1
    assert not stale.exists()
    assert not stale.parent.exists()  # emptied folder pruned


def test_sync_keeps_curated_note_in_generated_tree(tmp_path):
    config = Config.load(tmp_path)
    curated = config.vault_dir / "FAR" / "Part 001" / "My Notes.md"
    curated.parent.mkdir(parents=True)
    curated.write_text("mine\n", encoding="utf-8")
    stats = sync_vault(config, _plan_one("FAR", "Part 001", "1.1.md"))
    assert curated.exists()
    assert any("My Notes.md" in warning for warning in stats.warnings)


def test_sync_never_touches_outside_owned_tree(tmp_path):
    config = Config.load(tmp_path)
    outside = config.vault_dir / "Topics" / "Airspace.md"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(_GENERATED_NOTE)  # even if marked generated
    stats = sync_vault(config, _plan_one("FAR", "Part 001", "1.1.md"))
    assert outside.exists()
    assert stats.deleted == 0


def test_sync_stats_default():
    stats = SyncStats()
    assert (stats.written, stats.unchanged, stats.deleted, stats.warnings) == (0, 0, 0, [])


# ---------------------------------------------------------------------------
# Review regressions
# ---------------------------------------------------------------------------


def test_empty_non_reserved_document_gains_no_reserved_text():
    # A section whose text exists only as a pending amendment link must not
    # be rendered as [Reserved] — that wording is not in the source.
    from far_aim.generate.notes import build_appendix_note, build_section_note

    sec = _mini_section("1", "1.1", "Pending heading.")
    assert sec["reserved"] is False and sec["content"] == []
    note = build_section_note(sec, [], set())
    assert "[Reserved]" not in note.body
    assert "## Official Text" not in note.body

    apx = {
        "id": "cfr-14-part-1-appendix-A",
        "document_type": "cfr_appendix",
        "heading": "Appendix A to Part 1—Pending",
        "alternate_headings": [],
        "part": "1",
        "subpart": None,
        "title_number": 14,
        "reserved": False,
        "content": [],
        "citations": [],
        "section_authority": None,
        "editorial_notes": [],
        "canonical_hash": "sha256:0",
        "source": dict(SOURCE),
    }
    note = build_appendix_note(apx, [], set())
    assert "[Reserved]" not in note.body


def test_reserved_document_still_renders_reserved(slice_plan):
    body = slice_plan[("FAR", "Part 091", "91.27-91.99.md")].decode()
    assert "## Official Text\n\n\\[Reserved]" in body


def test_slice_91_220_has_no_fabricated_text(slice_plan):
    # 91.220 in the slice is amendment-link-only: empty content, not reserved.
    body = slice_plan[("FAR", "Part 091", "91.220.md")].decode()
    assert "[Reserved]" not in body
    assert "## Official Text" not in body
    assert "Amendment notes" in body


def test_other_title_attribution_bans_cross_reference():
    from far_aim.generate.notes import build_section_note

    sec = _mini_section("152", "152.111", "Nondiscrimination.")
    sec["content"] = [
        {"type": "text", "style": "paragraph",
         "text": "Discrimination is prohibited under § 21.7 of the Regulations."},
        {"type": "text", "style": "paragraph", "text": "(49 CFR 21.7)."},
    ]
    note = build_section_note(sec, [], {"21.7", "152.111"})
    assert "[[21.7" not in note.body
    assert "Explicit Cross-References" not in note.body


def test_sync_failure_rolls_back_to_previous_vault(tmp_path, monkeypatch):
    import far_aim.generate.build as build_mod

    config = Config.load(tmp_path)
    old = {
        ("FAR", "Part 001", "1.1.md"): b"---\nid: \"a\"\ngenerated: true\n---\n\n# old 1.1\n",
        ("FAR", "Part 001", "1.3.md"): b"---\nid: \"b\"\ngenerated: true\n---\n\n# old 1.3\n",
    }
    sync_vault(config, old)
    before = {p: p.read_bytes() for p in sorted(config.vault_dir.rglob("*.md"))}

    new = {
        ("FAR", "Part 001", "1.1.md"): b"---\nid: \"a\"\ngenerated: true\n---\n\n# new 1.1\n",
        ("FAR", "Part 001", "1.3.md"): b"---\nid: \"b\"\ngenerated: true\n---\n\n# new 1.3\n",
        ("FAR", "Part 002", "2.1.md"): b"---\nid: \"c\"\ngenerated: true\n---\n\n# new 2.1\n",
    }
    real_write = build_mod.write_text_atomic
    calls = {"n": 0}

    def failing_write(path, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real_write(path, data)

    monkeypatch.setattr(build_mod, "write_text_atomic", failing_write)
    with pytest.raises(OSError, match="disk full"):
        sync_vault(config, new)

    after = {p: p.read_bytes() for p in sorted(config.vault_dir.rglob("*.md"))}
    assert after == before  # last known-good preserved
    assert not (config.vault_dir / "FAR" / "Part 002").exists()
    assert not list(config.vault_dir.glob(".sync-backup-*"))


def test_sync_failure_during_stale_deletion_rolls_back(tmp_path, monkeypatch):
    import far_aim.generate.build as build_mod

    config = Config.load(tmp_path)
    plan = _plan_one("FAR", "Part 001", "1.1.md")
    sync_vault(config, plan)
    stale_a = config.vault_dir / "FAR" / "Part 001" / "9.1.md"
    stale_b = config.vault_dir / "FAR" / "Part 001" / "9.2.md"
    stale_a.write_bytes(_GENERATED_NOTE)
    stale_b.write_bytes(_GENERATED_NOTE)
    before = {p: p.read_bytes() for p in sorted(config.vault_dir.rglob("*.md"))}

    real_rename = build_mod.os.rename
    calls = {"n": 0}

    def failing_rename(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("permission denied")
        real_rename(src, dst)

    monkeypatch.setattr(build_mod.os, "rename", failing_rename)
    with pytest.raises(OSError, match="permission denied"):
        sync_vault(config, plan)
    monkeypatch.undo()

    after = {p: p.read_bytes() for p in sorted(config.vault_dir.rglob("*.md"))}
    assert after == before  # the already-deleted stale note was restored


def test_escape_md_dollar_prevents_inline_math():
    # Obsidian reads paired $ as inline-math delimiters; two currency
    # amounts in one paragraph must not render the span between them as math.
    assert (
        escape_md("a fine of $300,000 up to $20,000,000")
        == "a fine of \\$300,000 up to \\$20,000,000"
    )


# ---------------------------------------------------------------------------
# Phase 6: FAR → FAR part links
# ---------------------------------------------------------------------------


def _far_section(part: str, section: str, text: str) -> dict:
    sec = _mini_section(part, section, "Test.")
    sec["content"] = [{"type": "text", "style": "paragraph", "text": text}]
    return sec


def _xrefs(note) -> list[str]:
    tail = note.body.split("## Explicit Cross-References\n\n", 1)
    return tail[1].strip().splitlines() if len(tail) == 2 else []


def test_section_note_lists_sections_then_parts_and_skips_own_part():
    from far_aim.generate.notes import build_section_note

    sec = _far_section(
        "61",
        "61.3",
        "issued under part 61 or part 143 of this chapter, see § 91.3 and part 91 of this chapter",
    )
    note = build_section_note(sec, [], {"91.3"}, {"61", "91", "143"})
    assert _xrefs(note) == ["- [[91.3|§ 91.3]]", "- [[Part 91]]", "- [[Part 143]]"]
    # Without a known-parts registry the note is unchanged from Phase 3.
    assert _xrefs(build_section_note(sec, [], {"91.3"})) == ["- [[91.3|§ 91.3]]"]


def test_section_note_part_links_honour_other_title_ban():
    from far_aim.generate.notes import build_section_note

    sec = _far_section(
        "152",
        "152.421",
        "part 21 of the regulations of the Office of the Secretary of Transportation "
        "(49 CFR part 21) apply; see also part 15 of this chapter",
    )
    note = build_section_note(sec, [], set(), {"15", "21"})
    assert _xrefs(note) == ["- [[Part 15]]"]
