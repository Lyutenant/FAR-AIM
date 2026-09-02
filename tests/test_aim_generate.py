"""Phase 4 tests: canonical AIM JSON → Obsidian vault notes and assets.

Golden files under ``tests/fixtures/vault/`` are the committed expected
Markdown for representative AIM notes generated from the fixture corpus
(verbatim official text; see ``test_aim_source.py``), planned together with
the FAR part-91 slice so cross-corpus naming and alias rules are exercised.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import pytest

from far_aim.config import Config
from far_aim.generate import BuildError, aim_notes, naming
from far_aim.generate.aim_markdown import asset_prefix_for, list_marker, render_aim_blocks
from far_aim.generate.build import (
    ASSET_LEDGER,
    AimLayer,
    aim_asset_hashes,
    asset_ledger_bytes,
    build_registry,
    plan_vault,
    read_asset_ledger,
    sync_vault,
)
from far_aim.manifest import SourceManifest, SourceState
from far_aim.models import cfr as cfr_model
from far_aim.parsers import aim as aim_parser
from far_aim.parsers import ecfr as ecfr_parser
from far_aim.sources import aim as aim_source
from tests.test_aim_parser import SOURCE as AIM_SOURCE
from tests.test_aim_source import (
    FIXTURE_APPENDICES,
    FIXTURE_CHAPTERS,
    VERSION,
    fetch_fixture,
)
from tests.test_ecfr_parser import SLICE_PATH
from tests.test_ecfr_parser import SOURCE as ECFR_SOURCE

VAULT_FIXTURES = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture(scope="module")
def aim_layer(tmp_path_factory) -> AimLayer:
    _, result = fetch_fixture(tmp_path_factory.mktemp("aim-layer"))
    metadata = aim_source.load_metadata(result.snapshot_dir)
    assert metadata is not None
    with (
        mock.patch.multiple(
            aim_source, REQUIRED_CHAPTERS=FIXTURE_CHAPTERS, REQUIRED_APPENDICES=FIXTURE_APPENDICES
        ),
        mock.patch.object(aim_parser, "REQUIRE_RESOLVED_REFERENCES", False),
    ):
        docs = aim_parser.build_aim_docs(result.snapshot_dir, metadata, AIM_SOURCE)
    title_hash = cfr_model.canonical_hash({k: d["canonical_hash"] for k, d in docs.items()})
    figures = result.snapshot_dir / aim_source.FIGURES_DIR
    assets = {name: (figures / name).read_bytes() for name in aim_asset_hashes(docs)}
    return AimLayer(docs=docs, title_hash=title_hash, assets=assets)


@pytest.fixture(scope="module")
def far_docs() -> dict[str, dict]:
    return ecfr_parser.build_part_docs(ET.parse(SLICE_PATH).getroot(), ECFR_SOURCE)


def _sources(far_docs: dict[str, dict], aim: AimLayer) -> dict[str, SourceState]:
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(
        accepted_version="2026-08-19",
        canonical_hash=cfr_model.canonical_hash(
            {number: doc["canonical_hash"] for number, doc in far_docs.items()}
        ),
    )
    manifest.sources["aim"] = SourceState(
        accepted_version=VERSION,
        effective_date="2026-07-09",
        change=3,
        canonical_hash=aim.title_hash,
    )
    return manifest.sources


@pytest.fixture(scope="module")
def combined_plan(far_docs, aim_layer) -> dict[tuple[str, ...], bytes]:
    title_hash = _sources(far_docs, aim_layer)["ecfr_title_14"].canonical_hash
    return plan_vault(far_docs, "2026-08-19", title_hash, _sources(far_docs, aim_layer), aim_layer)


# ---------------------------------------------------------------------------
# Naming and markers
# ---------------------------------------------------------------------------


def test_aim_naming_rules():
    assert naming.aim_chapter_folder(4) == "Chapter 04"
    assert naming.aim_chapter_folder(11) == "Chapter 11"
    assert naming.aim_chapter_stem(4) == "AIM Chapter 4"
    assert naming.aim_section_stem(4, 1) == "AIM 4-1"
    assert naming.aim_paragraph_stem("4-1-9") == "4-1-9"
    assert naming.aim_appendix_stem(3) == "AIM Appendix 3"


def test_list_markers_follow_faa_convention():
    assert [list_marker(1, i) for i in range(3)] == ["a.", "b.", "c."]
    assert [list_marker(2, i) for i in range(3)] == ["1.", "2.", "3."]
    assert [list_marker(3, i) for i in range(2)] == ["(a)", "(b)"]
    assert [list_marker(4, i) for i in range(2)] == ["(1)", "(2)"]
    assert [list_marker(5, i) for i in range(2)] == ["[a]", "[b]"]
    assert [list_marker(6, i) for i in range(2)] == ["[1]", "[2]"]
    assert list_marker(1, 25) == "z." and list_marker(1, 26) == "aa."
    with pytest.raises(BuildError):
        list_marker(7, 0)


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def test_render_flat_list_with_nested_levels_and_note():
    blocks = [
        {
            "type": "list",
            "level": 1,
            "items": [
                {
                    "blocks": [
                        _text("First item"),
                        {
                            "type": "list",
                            "level": 2,
                            "html_type": "a",
                            "items": [
                                {"blocks": [_text("Sub 1")]},
                                {
                                    "blocks": [
                                        _text("Sub 2"),
                                        {
                                            "type": "note",
                                            "kind": "note",
                                            "title": "NOTE-",
                                            "blocks": [_text("Careful.")],
                                        },
                                    ]
                                },
                            ],
                        },
                    ]
                },
                {
                    "blocks": [
                        {"type": "note", "kind": "example", "title": "EXAMPLE-", "blocks": []}
                    ]
                },
            ],
        }
    ]
    assert render_aim_blocks(blocks) == [
        "**a.** First item",
        "**1.** Sub 1",
        "**2.** Sub 2",
        "> [!note] NOTE-\n> Careful.",
        "**b.**",
        "> [!example] EXAMPLE-",
    ]


def test_render_callout_kinds_and_multiline_phraseology():
    blocks = [
        {
            "type": "note",
            "kind": "reference",
            "title": "REFERENCE-",
            "blocks": [_text("AIM 5-4-3")],
        },
        {
            "type": "note",
            "kind": "phraseology",
            "title": "PHRASEOLOGY-",
            "blocks": [_text("LINE ONE.\nLINE TWO.")],
        },
    ]
    assert render_aim_blocks(blocks) == [
        "> [!cite] REFERENCE-\n> AIM 5-4-3",
        "> [!quote] PHRASEOLOGY-\n> LINE ONE.\\\n> LINE TWO.",
    ]


def test_source_breaks_render_as_hard_breaks():
    blocks = [
        _text("FAA Order JO 7210.3, Para 4-2-2, Pilot Education.\nFAA Order 1600.69."),
        {"type": "list", "level": 1, "items": [{"blocks": [_text("first\nsecond")]}]},
    ]
    assert render_aim_blocks(blocks) == [
        "FAA Order JO 7210.3, Para 4-2-2, Pilot Education.\\\nFAA Order 1600.69.",
        "**a.** first\\\nsecond",
    ]


def test_render_figure_and_image_embeds():
    figure = {
        "type": "figure",
        "number": "FIG 4-1-15",
        "title": "Induced Error [x] | y",
        "image": {"alt": "alt", "sha256": "sha256:0", "source": {"src": "images/fig79.svg"}},
    }
    image = {
        "type": "image",
        "alt": "Form 1",
        "sha256": "sha256:0",
        "source": {"src": "images/form.png"},
    }
    assert render_aim_blocks([figure, image]) == [
        "**FIG 4-1-15** *Induced Error \\[x] | y*",
        "![[fig79.svg|Induced Error x / y]]",
        "![[form.png|Form 1]]",
    ]


def _cell(*blocks: dict, **attrs) -> dict:
    return {"blocks": list(blocks), **attrs}


def test_render_pipe_table_with_image_cells():
    table = {
        "type": "table",
        "number": "TBL 1",
        "title": "Programs",
        "header_rows": [
            [
                _cell(_text("Type")),
                _cell(
                    {"type": "image", "alt": "H", "sha256": "x", "source": {"src": "images/h.png"}}
                ),
            ]
        ],
        "rows": [[_cell(_text("ASOS")), _cell(_text("X | Y"))], [_cell(), _cell(_text("2"))]],
        "foot_rows": [],
    }
    assert render_aim_blocks([table]) == [
        "**TBL 1** *Programs*",
        "| Type | ![[h.png\\|H]] |\n| --- | --- |\n| ASOS | X \\| Y |\n|  | 2 |",
    ]


def test_render_html_table_for_spans_and_nested_blocks():
    table = {
        "type": "table",
        "number": None,
        "title": None,
        "header_rows": [[_cell(_text("A"), colspan=2)]],
        "rows": [
            [
                _cell(_text("one"), _text("two")),
                _cell(
                    {
                        "type": "list",
                        "level": 1,
                        "items": [{"blocks": [_text("x")]}, {"blocks": [_text("y")]}],
                    },
                    {"type": "note", "kind": "note", "title": "NOTE-", "blocks": [_text("n")]},
                ),
            ]
        ],
        "foot_rows": [],
    }
    assert render_aim_blocks([table]) == [
        "<table>\n<thead>\n<tr><th colspan=\"2\"><p>A</p></th></tr>\n</thead>\n<tbody>\n"
        "<tr><td><p>one</p><p>two</p></td>"
        "<td><p><b>a.</b> x</p><p><b>b.</b> y</p><p><b>NOTE-</b></p><p>n</p></td></tr>\n"
        "</tbody>\n</table>"
    ]


def test_markers_follow_stylesheet_not_html_type(aim_layer):
    """AIM 4-1-20 itself cites the 5th item of a type="i" level-three list as
    "(e) above" — the enumeration the stylesheet renders, not roman numerals."""
    para = next(
        p
        for s in aim_layer.docs["chapter-04"]["sections"]
        for p in s["paragraphs"]
        if p["paragraph"] == "4-1-20"
    )

    def find_lists(blocks):
        for block in blocks:
            if block["type"] == "list":
                yield block
                for item in block["items"]:
                    yield from find_lists(item["blocks"])
            elif block["type"] == "note":
                yield from find_lists(block["blocks"])

    target = next(
        lst
        for lst in find_lists(para["content"])
        if lst["level"] == 3
        and lst.get("html_type") == "i"
        and any(
            b["type"] == "text" and b["text"].startswith("For ADS-B Out: Class E airspace")
            for item in lst["items"]
            for b in item["blocks"]
        )
    )
    rendered = "\n\n".join(render_aim_blocks([target]))
    assert "**(e)** For ADS-B Out: Class E airspace" in rendered
    assert "described in (e) above" in rendered
    assert "**v.**" not in rendered and "**(v)**" not in rendered


def test_html_table_images_are_note_relative():
    table = {
        "type": "table",
        "number": "TBL 7-1-10",
        "title": None,
        "header_rows": [],
        "rows": [
            [
                _cell(
                    {
                        "type": "image",
                        "alt": "ASOS",
                        "sha256": "x",
                        "source": {"src": "images/h.png"},
                    }
                ),
                _cell(_text("one"), _text("two")),
            ]
        ],
        "foot_rows": [],
    }
    prefix = asset_prefix_for(("AIM", "Chapter 07", "7-1-10.md"))
    (caption, html) = render_aim_blocks([table], prefix)
    assert caption == "**TBL 7-1-10**"
    assert '<img src="../assets/h.png" alt="ASOS">' in html


def test_html_table_image_attributes_are_attribute_safe():
    table = {
        "type": "table",
        "number": None,
        "title": None,
        "header_rows": [],
        "rows": [
            [
                _cell(
                    {
                        "type": "figure",
                        "number": "FIG 1",
                        "title": 'Say "ROGER" & <wait>',
                        "image": {"alt": "x", "sha256": "x", "source": {"src": "images/q.png"}},
                    }
                ),
                _cell(_text("a"), _text("b")),
            ]
        ],
        "foot_rows": [],
    }
    (html,) = render_aim_blocks([table], "../assets")
    assert 'alt="Say &quot;ROGER&quot; &amp; &lt;wait&gt;"' in html
    assert 'onerror' not in html and html.count("<img ") == 1
    # The figure's visible caption is kept ahead of the image.
    # (quotes need escaping only inside attributes, not in element text)
    assert '<p><b>FIG 1</b> <i>Say "ROGER" &amp; &lt;wait&gt;</i></p><p><img ' in html


def test_figure_caption_survives_in_pipe_table_cell():
    figure = {
        "type": "figure",
        "number": "FIG 7-1",
        "title": "Sky | Cover",
        "image": {"alt": "x", "sha256": "x", "source": {"src": "images/sky.png"}},
    }
    table = {
        "type": "table",
        "number": None,
        "title": None,
        "header_rows": [[_cell(_text("Symbol")), _cell(_text("Meaning"))]],
        "rows": [[_cell(figure), _cell(_text("clear"))]],
        "foot_rows": [],
    }
    (pipe,) = render_aim_blocks([table])
    assert "| **FIG 7-1** *Sky \\| Cover* ![[sky.png\\|Sky / Cover]] | clear |" in pipe
    assert asset_prefix_for(("AIM", "AIM.md")) == "assets"
    assert asset_prefix_for(("AIM", "Appendices", "AIM Appendix 4.md")) == "../assets"
    with pytest.raises(BuildError):
        asset_prefix_for(("FAR", "Part 091", "91.155.md"))


def test_render_unknown_block_fails():
    with pytest.raises(BuildError, match="no renderer"):
        render_aim_blocks([{"type": "mystery"}])


# ---------------------------------------------------------------------------
# Plan: registry, golden notes, shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "4-1-2.md",  # reference box, unresolved cross-reference
        "4-1-8.md",  # note inside a list item
        "4-1-15.md",  # figures, example boxes, deep lists, self-referencing anchor
        "AIM 4-1.md",  # section note
        "AIM Chapter 4.md",  # chapter contents
        "AIM 0-0.md",  # section-level content, no paragraphs
        "AIM Appendix 1.md",  # bare image embeds
        "AIM Appendix 3.md",  # pipe table
        "AIM.md",  # publication index
    ],
)
def test_golden_aim_note(combined_plan, name):
    match = [data for parts, data in combined_plan.items() if parts[-1] == name]
    assert len(match) == 1, f"note {name} not planned exactly once"
    assert match[0] == (VAULT_FIXTURES / name).read_bytes()


def test_combined_plan_shape(combined_plan, aim_layer):
    paths = set(combined_plan)
    assert ("AIM", "AIM.md") in paths
    assert ("AIM", "Chapter 04", "AIM Chapter 4.md") in paths
    assert ("AIM", "Chapter 04", "AIM 4-1.md") in paths
    assert ("AIM", "Chapter 04", "4-1-9.md") in paths
    assert ("AIM", "Chapter 00", "AIM 0-0.md") in paths
    assert ("AIM", "Appendices", "AIM Appendix 3.md") in paths
    for name, data in aim_layer.assets.items():
        assert combined_plan[("AIM", "assets", name)] == data
    # FAR slice with home note (19) + AIM: index, 2 chapters, 2 sections,
    # 7 paragraphs, 2 appendices (14) + 5 assets + the asset ledger.
    assert len(combined_plan) == 19 + 14 + 5 + 1
    assert combined_plan[("AIM", "assets", ASSET_LEDGER)] == asset_ledger_bytes(aim_layer.assets)


def test_aim_aliases_and_targets(far_docs, aim_layer):
    registry = build_registry(far_docs, aim_layer)
    assert registry.aliases["aim-4-1-9"] == [
        "AIM 4-1-9",
        "Traffic Advisory Practices at Airports Without Operating Control Towers",
    ]
    assert registry.aim_targets["aim-4-1-9"] == (
        "4-1-9",
        "AIM 4-1-9 — Traffic Advisory Practices at Airports Without Operating Control Towers",
    )
    assert registry.aim_targets["aim-4-1"] == (
        "AIM 4-1",
        "AIM Chapter 4, Section 1 — Services Available to Pilots",
    )
    assert registry.stems["aim0401_fig79_recovered.svg"] == (
        "AIM", "assets", "aim0401_fig79_recovered.svg",
    )
    assert registry.aim_note_count == 14


def test_heading_alias_collision_across_corpora_drops_both(far_docs, aim_layer):
    import copy

    far = copy.deepcopy(far_docs)
    aim = copy.deepcopy(aim_layer.docs)
    # Give a FAR section the same heading as an AIM paragraph.
    section = _first_section(far["91"]["children"])
    section["heading"] = "Control Towers."
    registry = build_registry(far, AimLayer(docs=aim, title_hash="x", assets=aim_layer.assets))
    assert "Control Towers" not in registry.aliases[section["id"]]
    assert registry.aliases["aim-4-1-2"] == ["AIM 4-1-2"]


def _first_section(children: list[dict]) -> dict:
    for child in children:
        if child.get("document_type") == "cfr_section":
            return child
        for key in ("children", "sections"):
            if isinstance(child.get(key), list):
                found = _first_section(child[key])
                if found is not None:
                    return found
    return None


def test_index_page_image_becomes_a_vault_asset(far_docs, aim_layer):
    import copy

    docs = copy.deepcopy(aim_layer.docs)
    docs["publication"]["description"].insert(
        0,
        {
            "type": "image",
            "alt": "Cover",
            "sha256": "sha256:" + "c" * 64,
            "source": {"src": "images/cover.png"},
        },
    )
    assert aim_asset_hashes(docs)["cover.png"] == "sha256:" + "c" * 64
    # Missing from the archive: refused up front rather than a broken embed.
    with pytest.raises(BuildError, match="figures missing from the archived snapshot"):
        build_registry(far_docs, AimLayer(docs=docs, title_hash="x", assets=aim_layer.assets))
    assets = {**aim_layer.assets, "cover.png": b"\x89PNG cover"}
    layer = AimLayer(docs=docs, title_hash="x", assets=assets)
    registry = build_registry(far_docs, layer)
    assert "cover.png" in registry.aim_assets
    sources = _sources(far_docs, layer)
    far_hash = sources["ecfr_title_14"].canonical_hash
    plan = plan_vault(far_docs, "2026-08-19", far_hash, sources, layer)
    assert plan[("AIM", "assets", "cover.png")] == b"\x89PNG cover"
    assert "![[cover.png|Cover]]" in plan[("AIM", "AIM.md")].decode()


def test_chapter_zero_section_cites_its_official_label(combined_plan):
    """Stable id/path stay 0-0, but the citation uses the FAA's "Section 1."."""
    note = combined_plan[("AIM", "Chapter 00", "AIM 0-0.md")].decode()
    assert 'citation: "AIM Chapter 0, Section 1"' in note
    assert "# AIM Chapter 0, Section 1 — Explanation of Changes" in note
    assert 'id: "aim-0-0"' in note and "section: 0" in note
    chapter = combined_plan[("AIM", "Chapter 00", "AIM Chapter 0.md")].decode()
    assert "[[AIM 0-0|Section 1 — Explanation of Changes]]" in chapter
    assert "Section 0" not in chapter


def test_aim_index_renders_publication_front_matter(combined_plan):
    body = combined_plan[("AIM", "AIM.md")].decode()
    assert "# Aeronautical Information Manual" in body
    assert "official guide to basic flight information and ATC procedures." in body
    assert "FEDERAL AVIATION ADMINISTRATION" in body
    index_url = "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/index.html"
    assert f"[view on FAA]({index_url})" in body


def test_no_volatile_timestamps_in_aim_notes(combined_plan):
    for parts, data in combined_plan.items():
        if parts[0] == "AIM" and parts[-1].endswith(".md"):
            text = data.decode()
            assert "retrieved_at" not in text
            assert "2026-08-29T" not in text


def test_missing_asset_fails_build(far_docs, aim_layer):
    assets = dict(aim_layer.assets)
    del assets["aim0401_fig79_recovered.svg"]
    with pytest.raises(BuildError, match="figures missing from the archived snapshot"):
        build_registry(far_docs, AimLayer(docs=aim_layer.docs, title_hash="x", assets=assets))


def test_edition_text():
    assert aim_notes.edition_text(AIM_SOURCE) == (
        "Basic with Change 1, 2 and 3 (effective 2026-07-09)"
    )


# ---------------------------------------------------------------------------
# Sync: assets and curated material
# ---------------------------------------------------------------------------


def test_sync_writes_assets_and_prunes_only_ledgered_ones(tmp_path, combined_plan):
    config = Config.load(tmp_path)
    stats = sync_vault(config, combined_plan)
    assert stats.written == len(combined_plan)
    assets = config.vault_dir / "AIM" / "assets"
    assert (assets / "aim0401_fig79_recovered.svg").exists()
    ledger = read_asset_ledger(assets / ASSET_LEDGER)
    assert set(ledger) == {p.name for p in assets.iterdir() if p.name != ASSET_LEDGER}

    # A generated asset that a later edition no longer references is pruned...
    smaller = {k: v for k, v in combined_plan.items() if k[-1] != "aimapd1_floating1.png"}
    smaller[("AIM", "assets", ASSET_LEDGER)] = asset_ledger_bytes(
        {
            k[-1]: v
            for k, v in smaller.items()
            if k[:2] == ("AIM", "assets") and k[-1] != ASSET_LEDGER
        }
    )
    # ...while curated files in the assets directory — and a generated asset
    # the user modified — are kept with a warning.
    curated_asset = assets / "my-diagram.png"
    curated_asset.write_bytes(b"mine")
    curated_note = config.vault_dir / "AIM" / "Chapter 04" / "My notes on 4-1-9.md"
    curated_note.write_text("# mine\n", encoding="utf-8")
    modified = assets / "aimapd1_floating0.png"
    modified.write_bytes(b"edited by hand")
    smaller = {k: v for k, v in smaller.items() if k[-1] != "aimapd1_floating0.png"}
    stats = sync_vault(config, smaller)
    assert stats.deleted == 1
    assert not (assets / "aimapd1_floating1.png").exists()
    assert curated_asset.read_bytes() == b"mine"
    assert modified.read_bytes() == b"edited by hand"
    assert curated_note.exists()
    assert any("curated file inside generated assets kept" in w for w in stats.warnings)
    assert any("modified generated asset kept" in w for w in stats.warnings)
    assert any("curated note inside generated tree kept" in w for w in stats.warnings)


def test_sync_refuses_curated_file_at_generated_asset_path(tmp_path, combined_plan):
    config = Config.load(tmp_path)
    path = config.vault_dir / "AIM" / "assets" / "aim0401_fig79_recovered.svg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"<svg>mine</svg>")
    with pytest.raises(BuildError, match="curated or modified file at generated asset path"):
        sync_vault(config, combined_plan)
    assert path.read_bytes() == b"<svg>mine</svg>"


def test_sync_does_not_adopt_identical_curated_asset(tmp_path, combined_plan, aim_layer):
    """Same bytes, no ledger entry: the file is the user's, not the generator's."""
    config = Config.load(tmp_path)
    path = config.vault_dir / "AIM" / "assets" / "aim0401_fig79_recovered.svg"
    path.parent.mkdir(parents=True)
    path.write_bytes(aim_layer.assets["aim0401_fig79_recovered.svg"])
    with pytest.raises(BuildError, match="curated or modified file at generated asset path"):
        sync_vault(config, combined_plan)
    assert not (path.parent / ASSET_LEDGER).exists()
    # Once moved away, the build proceeds and owns its own copy.
    path.rename(path.with_name("my-copy.svg"))
    sync_vault(config, combined_plan)
    assert "aim0401_fig79_recovered.svg" in read_asset_ledger(path.parent / ASSET_LEDGER)
    assert "my-copy.svg" not in read_asset_ledger(path.parent / ASSET_LEDGER)


def test_curated_file_at_ledger_path_is_never_touched(tmp_path, combined_plan, far_docs):
    config = Config.load(tmp_path)
    ledger = config.vault_dir / "AIM" / "assets" / ASSET_LEDGER
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"generated": {"mine.png": "sha256:0"}}\n', encoding="utf-8")
    assert read_asset_ledger(ledger) == {}  # no ownership marker: curated
    with pytest.raises(BuildError, match="reserved asset-ledger path"):
        sync_vault(config, combined_plan)
    assert '"mine.png"' in ledger.read_text(encoding="utf-8")
    # A FAR-only plan (AIM layer absent) must not prune it either.
    far_only = {k: v for k, v in combined_plan.items() if k[0] != "AIM"}
    stats = sync_vault(config, far_only)
    assert ledger.exists() and stats.deleted == 0
    assert any("reserved asset-ledger path kept" in w for w in stats.warnings)


def test_sync_refuses_to_overwrite_generated_asset_edited_in_place(tmp_path, combined_plan):
    config = Config.load(tmp_path)
    sync_vault(config, combined_plan)
    path = config.vault_dir / "AIM" / "assets" / "aim0401_fig79_recovered.svg"
    path.write_bytes(b"<svg>edited</svg>")
    with pytest.raises(BuildError, match="curated or modified file at generated asset path"):
        sync_vault(config, combined_plan)
    assert path.read_bytes() == b"<svg>edited</svg>"


def test_sync_refuses_curated_note_at_generated_aim_path(tmp_path, combined_plan):
    config = Config.load(tmp_path)
    path = config.vault_dir / "AIM" / "Chapter 04" / "4-1-9.md"
    path.parent.mkdir(parents=True)
    path.write_text("# my own 4-1-9\n", encoding="utf-8")
    with pytest.raises(BuildError, match="curated note at generated path"):
        sync_vault(config, combined_plan)
    assert path.read_text(encoding="utf-8") == "# my own 4-1-9\n"


# ---------------------------------------------------------------------------
# Phase 6: AIM → FAR links
# ---------------------------------------------------------------------------


def test_collect_aim_text_covers_block_kinds():
    blocks = [
        _text("14 CFR section 1.1"),
        {"type": "heading", "text": "§ 2.2"},
        {
            "type": "list",
            "level": 1,
            "items": [{"blocks": [_text("§ 3.3")]}, {"blocks": [_text("§ 4.4")]}],
        },
        {"type": "note", "kind": "note", "title": "NOTE- § 5.5", "blocks": [_text("§ 6.6")]},
        {"type": "figure", "number": "FIG 1", "title": "§ 7.7", "image": {"alt": "§ 99.99"}},
        {"type": "image", "alt": "§ 99.98", "source": {"src": "x.png"}},
        {
            "type": "table",
            "number": "TBL 1",
            "title": "§ 8.8",
            "header_rows": [[{"blocks": [_text("§ 9.9")]}]],
            "rows": [[{"blocks": [_text("§ 10.10")]}]],
            "foot_rows": [],
        },
    ]
    from far_aim.generate.aim_markdown import collect_text
    from far_aim.links import citations

    tokens = [t for text in collect_text(blocks) for t in citations.extract_citations(text)]
    # Image alt text is layout, never content.
    assert tokens == ["1.1", "2.2", "3.3", "4.4", "5.5", "6.6", "7.7", "8.8", "9.9", "10.10"]


def _aim_paragraph(aim: AimLayer, number: str) -> dict:
    for doc in aim.docs.values():
        for section in doc.get("sections") or []:
            for para in section["paragraphs"]:
                if para["paragraph"] == number:
                    return para
    raise AssertionError(number)


def test_far_links_from_fixture_paragraph(aim_layer):
    # AIM 4-1-20 cites "14 CFR section 91.217", "14 CFR sections 91.215,
    # 91.225, and 99.13" and "14 CFR § 91.225 ... 14 CFR § 91.215".
    para = _aim_paragraph(aim_layer, "4-1-20")
    far = aim_notes.FarTargets(
        sections=frozenset({"91.215", "91.217", "91.225", "99.13", "91.155"}),
        parts=frozenset({"91", "99"}),
    )
    assert aim_notes.far_links(para["content"], far) == [
        "- [[91.215|14 CFR § 91.215]]",
        "- [[91.217|14 CFR § 91.217]]",
        "- [[91.225|14 CFR § 91.225]]",
        "- [[99.13|14 CFR § 99.13]]",
    ]
    # Only in-corpus targets link; nothing links without a FAR corpus.
    assert aim_notes.far_links(para["content"], aim_notes.FarTargets(frozenset({"91.217"}))) == [
        "- [[91.217|14 CFR § 91.217]]"
    ]
    assert aim_notes.far_links(para["content"], aim_notes.FarTargets()) == []


def test_far_part_links_follow_sections():
    content = [_text("Operations under 14 CFR part 91 must comply with 14 CFR section 91.155.")]
    far = aim_notes.FarTargets(sections=frozenset({"91.155"}), parts=frozenset({"91"}))
    assert aim_notes.far_links(content, far) == [
        "- [[91.155|14 CFR § 91.155]]",
        "- [[Part 91|14 CFR Part 91]]",
    ]
    # An other-title attribution anywhere in the note bans the number.
    content.append(_text("Security areas are defined in 49 CFR part 91."))
    assert aim_notes.far_links(content, far) == ["- [[91.155|14 CFR § 91.155]]"]


def test_paragraph_note_lists_aim_anchors_then_far_links(aim_layer, far_docs):
    import copy

    para = copy.deepcopy(_aim_paragraph(aim_layer, "4-1-20"))
    # The fixture's own anchors all point outside the fixture; give it one
    # in-corpus AIM anchor so the ordering rule is exercised.
    para["explicit_references"].insert(
        0, {"text": "Paragraph 4-1-9", "target": "aim-4-1-9", "source": {"href": "x"}}
    )
    registry = build_registry(far_docs, aim_layer)
    far = aim_notes.FarTargets(sections=frozenset({"91.217"}), parts=frozenset())
    note = aim_notes.build_paragraph_note(para, [], registry.aim_targets, far)
    section = note.body.split("## Explicit Cross-References\n\n", 1)[1].strip().splitlines()
    assert section == [
        "- [[4-1-9|AIM 4-1-9 — Traffic Advisory Practices at Airports Without "
        "Operating Control Towers]]",
        "- [[91.217|14 CFR § 91.217]]",
    ]
    # Without a FAR corpus the note is unchanged apart from the FAR link.
    plain = aim_notes.build_paragraph_note(para, [], registry.aim_targets)
    assert plain.body == note.body.replace("\n- [[91.217|14 CFR § 91.217]]", "")


def test_combined_plan_links_only_far_notes_that_exist(combined_plan):
    # The part-91 slice lacks 91.215/91.217/91.225 and 99.13, so 4-1-20 gains
    # no FAR link: zero broken links by construction.
    note = combined_plan[("AIM", "Chapter 04", "4-1-20.md")].decode()
    assert "|14 CFR §" not in note
    assert "[[Part " not in note


# ---------------------------------------------------------------------------
# Phase 6: AIM → PCG glossary terms
# ---------------------------------------------------------------------------


def test_glossary_terms_render_in_their_own_section(aim_layer, far_docs):
    from far_aim.links import glossary

    def term(tid: str, text: str, *blocks: dict) -> dict:
        entry = {"type": "entry", "text": f"{text}-"}
        return {"id": tid, "term": text, "letter": text[0], "content": [entry, *blocks]}

    see = {"type": "reference", "kind": "see", "label": "See", "text": "COMMON…", "target": None}
    defn = {"type": "text", "text": "x"}
    pcg_docs = {
        "letter-t": {
            "terms": [
                term("pcg-traffic-pattern", "TRAFFIC PATTERN", defn),
                term("pcg-ctaf", "CTAF", see),
                term("pcg-aircraft", "AIRCRAFT", defn),
            ]
        }
    }
    index = glossary.GlossaryIndex.build(pcg_docs, glossary.Gate())
    targets = {
        "pcg-traffic-pattern": ("TRAFFIC PATTERN", "TRAFFIC PATTERN"),
        "pcg-ctaf": ("CTAF", "CTAF"),
        "pcg-aircraft": ("AIRCRAFT", "AIRCRAFT"),
    }
    links = aim_notes.GlossaryLinks(index, targets)
    para = _aim_paragraph(aim_layer, "4-1-9")  # traffic advisory practices: CTAF, traffic pattern
    registry = build_registry(far_docs, aim_layer)
    note = aim_notes.build_paragraph_note(para, [], registry.aim_targets, None, links)
    tail = note.body.split("## Glossary Terms\n\n", 1)[1].strip().splitlines()
    # Sorted by term; AIRCRAFT is a single defined word and stays gated out.
    assert tail == ["- [[CTAF|CTAF]]", "- [[TRAFFIC PATTERN|TRAFFIC PATTERN]]"]
    assert note.body.rstrip().endswith(tail[-1])  # the section closes the note
    # Without a glossary the note is unchanged.
    plain = aim_notes.build_paragraph_note(para, [], registry.aim_targets)
    assert "## Glossary Terms" not in plain.body
