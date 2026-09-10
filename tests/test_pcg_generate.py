"""Phase 5 tests: canonical PCG JSON → Obsidian vault notes.

Golden files under ``tests/fixtures/vault/`` are the committed expected
Markdown for representative PCG notes generated from the fixture corpus
(verbatim official text; see ``test_pcg_source.py``), planned together with
the FAR part-91 slice and the AIM fixture corpus so cross-corpus naming and
alias rules are exercised.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import pytest

from far_aim.config import Config
from far_aim.generate import BuildError, naming
from far_aim.generate.build import (
    AimLayer,
    PcgLayer,
    aim_asset_hashes,
    build_registry,
    plan_vault,
    sync_vault,
)
from far_aim.manifest import SourceManifest, SourceState
from far_aim.models import cfr as cfr_model
from far_aim.parsers import aim as aim_parser
from far_aim.parsers import ecfr as ecfr_parser
from far_aim.parsers import pcg as pcg_parser
from far_aim.sources import aim as aim_source
from far_aim.sources import pcg as pcg_source
from tests.test_aim_parser import SOURCE as AIM_SOURCE
from tests.test_aim_source import FIXTURE_APPENDICES, FIXTURE_CHAPTERS
from tests.test_aim_source import fetch_fixture as aim_fetch_fixture
from tests.test_ecfr_parser import SLICE_PATH
from tests.test_ecfr_parser import SOURCE as ECFR_SOURCE
from tests.test_pcg_parser import SOURCE as PCG_SOURCE
from tests.test_pcg_source import FIXTURE_LETTERS
from tests.test_pcg_source import VERSION as PCG_VERSION
from tests.test_pcg_source import fetch_fixture as pcg_fetch_fixture

VAULT_FIXTURES = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture(scope="module")
def pcg_layer(tmp_path_factory) -> PcgLayer:
    _, result = pcg_fetch_fixture(tmp_path_factory.mktemp("pcg-layer"))
    metadata = pcg_source.load_metadata(result.snapshot_dir)
    assert metadata is not None
    with (
        mock.patch.object(pcg_source, "REQUIRED_LETTERS", FIXTURE_LETTERS),
        mock.patch.object(pcg_parser, "REQUIRE_RESOLVED_REFERENCES", False),
    ):
        docs = pcg_parser.build_pcg_docs(result.snapshot_dir, metadata, PCG_SOURCE)
    title_hash = cfr_model.canonical_hash({k: d["canonical_hash"] for k, d in docs.items()})
    return PcgLayer(docs=docs, title_hash=title_hash)


@pytest.fixture(scope="module")
def aim_layer(tmp_path_factory) -> AimLayer:
    _, result = aim_fetch_fixture(tmp_path_factory.mktemp("aim-layer"))
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


def _sources(far_docs, aim: AimLayer, pcg: PcgLayer) -> dict[str, SourceState]:
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(
        accepted_version="2026-08-19",
        canonical_hash=cfr_model.canonical_hash(
            {number: doc["canonical_hash"] for number, doc in far_docs.items()}
        ),
    )
    manifest.sources["aim"] = SourceState(
        accepted_version="2026-07-09-change-3",
        effective_date="2026-07-09",
        change=3,
        canonical_hash=aim.title_hash,
    )
    manifest.sources["pcg"] = SourceState(
        accepted_version=PCG_VERSION,
        effective_date="2026-07-09",
        change=3,
        canonical_hash=pcg.title_hash,
    )
    return manifest.sources


@pytest.fixture(scope="module")
def combined_plan(far_docs, aim_layer, pcg_layer) -> dict[tuple[str, ...], bytes]:
    sources = _sources(far_docs, aim_layer, pcg_layer)
    title_hash = sources["ecfr_title_14"].canonical_hash
    return plan_vault(far_docs, "2026-08-19", title_hash, sources, aim_layer, pcg_layer)


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def test_pcg_naming_rules():
    assert naming.pcg_letter_folder("a") == "A"
    assert naming.pcg_term_stem("CONTROLLED AIRSPACE") == "CONTROLLED AIRSPACE"
    assert naming.pcg_term_stem("NAVIGATION SPECIFICATION [ICAO]") == (
        "NAVIGATION SPECIFICATION (ICAO)"
    )
    assert naming.pcg_term_stem("DECISION ALTITUDE/DECISION HEIGHT [ICAO Annex 6]") == (
        "DECISION ALTITUDE-DECISION HEIGHT (ICAO Annex 6)"
    )
    assert naming.pcg_term_stem("CHART SUPPLEMENT U.S.") == "CHART SUPPLEMENT U.S"
    assert naming.pcg_term_stem("CIRCLE‐TO‐LAND MANEUVER") == "CIRCLE-TO-LAND MANEUVER"
    assert naming.pcg_term_stem("CENTER'S AREA") == "CENTER'S AREA"
    # Reserved by the AIM index note and FAR citation shapes.
    assert naming.pcg_term_stem("AIM") == "AIM (PCG)"
    assert naming.pcg_term_stem("91.155") == "91.155 (PCG)"
    with pytest.raises(ValueError):
        naming.pcg_term_stem("§§")


# ---------------------------------------------------------------------------
# Plan: registry, golden notes, shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "KNOWN TRAFFIC.md",  # plain definition
        "TRAFFIC PATTERN.md",  # sub-lists, note boxes, see/refer rows, external link
        "OUTER FIX.md",  # OR-joined duplicate definitions merged
        "NAVSPEC.md",  # CLASS_21 stub + parenthetical [ICAO] reference
        "PCG.md",  # publication index: purpose, summary, letters
    ],
)
def test_golden_pcg_note(combined_plan, name):
    match = [data for parts, data in combined_plan.items() if parts[-1] == name]
    assert len(match) == 1, f"note {name} not planned exactly once"
    assert match[0] == (VAULT_FIXTURES / name).read_bytes()


def test_combined_plan_shape(combined_plan, pcg_layer):
    paths = set(combined_plan)
    assert ("PCG", "PCG.md") in paths
    assert ("PCG", "K", "KNOWN TRAFFIC.md") in paths
    assert ("PCG", "O", "OUTER FIX.md") in paths
    terms = sum(pcg_parser.count_terms(d) for d in pcg_layer.docs.values())
    assert terms == 117
    pcg_files = [parts for parts in paths if parts[0] == "PCG"]
    assert len(pcg_files) == terms + 1  # + PCG.md


def test_registry_targets_and_aliases(far_docs, aim_layer, pcg_layer):
    registry = build_registry(far_docs, aim_layer, pcg_layer)
    assert registry.pcg_note_count == 118
    stem, display = registry.pcg_targets["pcg-navigation-specification-icao"]
    assert stem == "NAVIGATION SPECIFICATION (ICAO)"
    assert display == "NAVIGATION SPECIFICATION (ICAO)"
    # Sanitization changed the stem, so the verbatim term becomes an alias.
    assert registry.aliases["pcg-navigation-specification-icao"] == [
        "NAVIGATION SPECIFICATION [ICAO]"
    ]
    # Unchanged stems carry no redundant alias.
    assert registry.aliases["pcg-known-traffic"] == []
    assert registry.stems["KNOWN TRAFFIC"] == ("PCG", "K", "KNOWN TRAFFIC.md")


def test_home_note_links_every_built_layer(combined_plan):
    # With all three layers present, Home links each corpus index plus the
    # curated entry notes (registered stems, not planned files).
    home = combined_plan[("Home.md",)].decode()
    assert "[[Title 14|" in home
    assert "[[AIM|Aeronautical Information Manual]]" in home
    assert "[[PCG|Pilot/Controller Glossary]]" in home
    for curated in ("[[Collections]]", "[[Topics]]", "[[Study]]"):
        assert curated in home
    assert ("Collections", "Collections.md") not in combined_plan
    # Home is also the reader's guide: every generated tree and curated
    # folder is described, and the note anatomy names the sections a
    # three-layer build renders (Glossary Terms exist only with AIM + PCG).
    for section in ("## What is here", "## Reading a note", "## Writing your own notes"):
        assert section in home
    for described in ("`FAR/`", "`AIM/`", "`PCG/`", "**Glossary Terms**", "**See Also**"):
        assert described in home
    assert "`Concepts/`" not in home  # no enrichment in this plan


def test_wikilinks_resolve_and_note_renders(combined_plan):
    note = combined_plan[("PCG", "N", "NDB.md")].decode()
    assert "[[NONDIRECTIONAL BEACON|NONDIRECTIONAL BEACON]]" in note
    index = combined_plan[("PCG", "PCG.md")].decode()
    assert "## K" in index and "[[KNOWN TRAFFIC|KNOWN TRAFFIC]]" in index
    assert "PURPOSE" in index


def test_sub_lists_render_as_nested_items():
    from far_aim.generate.pcg_notes import render_pcg_blocks

    blocks = [
        {"type": "entry", "text": "TERM- Means:"},
        {"type": "list", "style": "a", "items": [{"text": "First."}, {"text": "Second\nline."}]},
        {"type": "list", "style": "1", "items": [{"text": ""}]},
    ]
    assert render_pcg_blocks(blocks) == [
        "TERM- Means:",
        "- **a.** First.",
        "- **b.** Second\\\n    line.",
        "- **1.**",
    ]


def test_embedded_definition_link_renders_in_references():
    """A hyperlink embedded in the official definition stays usable: its
    verbatim text remains in the Official Text and the destination renders
    as a link under ## References (Markdown escaping defeats autolinking)."""
    from far_aim.generate import pcg_notes

    url = "https://www.fly.faa.gov/rmt/nfdc_preferred_routes_database.jsp"
    term_doc = {
        "id": "pcg-preferred-ifr-routes",
        "document_type": "pcg_term",
        "letter": "P",
        "term": "PREFERRED IFR ROUTES",
        "content": [
            {"type": "entry", "text": f"PREFERRED IFR ROUTES- Routes listed at {url}."},
            {
                "type": "reference",
                "kind": "link",
                "label": None,
                "form": "embedded",
                "text": "",
                "target": None,
                "url": url,
                "source": {"href": url},
            },
        ],
        "source": {
            "edition_label": "Basic with Change 1, 2 and 3",
            "effective_date": "2026-07-09",
            "change": 3,
            "url": "https://www.faa.gov/air_traffic/publications/atpubs/pcg_html/"
            "glossary-p.html#PREFERRED_IFR_ROUTES",
        },
        "canonical_hash": "sha256:" + "0" * 64,
    }
    note = pcg_notes.build_term_note(term_doc, [], {})
    assert "## References" in note.body
    assert f"[{url.replace('_', chr(92) + '_')}]({url})" in note.body


def test_no_volatile_timestamps_in_pcg_notes(combined_plan):
    for parts, data in combined_plan.items():
        if parts[0] == "PCG":
            text = data.decode()
            assert "retrieved_at" not in text
            assert "2026-08-30T" not in text


def test_reserved_term_alias_goes_through_collision_filter(far_docs, aim_layer, pcg_layer):
    """A term sanitized away from a reserved stem must not alias back onto it.

    The glossary term ``AIM`` is filed as ``AIM (PCG).md``; its verbatim
    form equals the AIM index note's stem, so the alias is dropped and
    ``[[AIM]]`` stays unambiguous.
    """
    import copy

    docs = copy.deepcopy(pcg_layer.docs)
    letter = docs["letter-k"]
    fake = copy.deepcopy(letter["terms"][0])
    fake["id"] = "pcg-aim"
    fake["term"] = "AIM"
    fake["content"] = [{"type": "entry", "text": "AIM-"}]
    letter["terms"].append(fake)
    registry = build_registry(far_docs, aim_layer, PcgLayer(docs=docs, title_hash="x"))
    assert registry.stems["AIM (PCG)"] == ("PCG", "K", "AIM (PCG).md")
    assert registry.aliases["pcg-aim"] == []


def test_duplicate_pcg_term_display_collision_fails(far_docs, aim_layer, pcg_layer):
    import copy

    docs = copy.deepcopy(pcg_layer.docs)
    letter = docs["letter-k"]
    clone = copy.deepcopy(letter["terms"][0])
    clone["id"] = "pcg-known-traffic-2"
    letter["terms"].append(clone)
    with pytest.raises(BuildError, match="filename collision"):
        build_registry(far_docs, aim_layer, PcgLayer(docs=docs, title_hash="x"))


def test_sync_writes_pcg_tree_and_deletes_stale(tmp_path, combined_plan):
    config = Config.load(tmp_path)
    stats = sync_vault(config, combined_plan)
    assert stats.written == len(combined_plan)
    note = config.vault_dir / "PCG" / "K" / "KNOWN TRAFFIC.md"
    assert note.exists()
    # A stale generated PCG note (a removed term) is pruned on the next sync.
    stale = config.vault_dir / "PCG" / "K" / "OLD TERM.md"
    stale.write_bytes(note.read_bytes())
    stats = sync_vault(config, combined_plan)
    assert stats.deleted == 1 and not stale.exists()
    # A curated note at a generated path refuses the build.
    note.write_text("# my own\n", encoding="utf-8")
    with pytest.raises(BuildError, match="curated note at generated path"):
        sync_vault(config, combined_plan)


# ---------------------------------------------------------------------------
# Phase 6: PCG "Refer to" rows link the FAR / AIM notes they name
# ---------------------------------------------------------------------------


def test_refer_rows_link_far_and_aim_notes(combined_plan):
    note = combined_plan[("PCG", "T", "TRAFFIC PATTERN.md")].decode()
    refs = note.split("## References\n\n", 1)[1].strip().splitlines()
    assert refs == ["- [[Part 91|14 CFR part 91]]", "- [[AIM]]"]


def test_refer_row_rendering_shapes():
    from far_aim.generate import aim_notes
    from far_aim.generate.pcg_notes import ReferTargets, _refer_links

    far = aim_notes.FarTargets(sections=frozenset({"1.1", "135.100"}), parts=frozenset({"1", "91"}))
    refer = ReferTargets(far=far, aim_index_stem="AIM")
    assert _refer_links("14 CFR part 91", refer) == ["[[Part 91]]"]
    assert _refer_links("14 CFR part 1, §1.1", refer) == ["[[1.1|§ 1.1]]", "[[Part 1]]"]
    assert _refer_links("14 CFR section 135.100", refer) == ["[[135.100|§ 135.100]]"]
    assert _refer_links("AIM", refer) == ["[[AIM]]"]
    assert _refer_links("FAA Order JO 7110.65, Para 10-6-4, INFLIGHT CONTINGENCIES", refer) == []
    # Without the AIM corpus, "AIM" stays the FAA link; no FAR corpus, no links.
    assert _refer_links("AIM", ReferTargets(far=far)) == []
    no_far = ReferTargets(far=aim_notes.FarTargets(), aim_index_stem="AIM")
    assert _refer_links("14 CFR part 91", no_far) == []


def test_refer_to_aim_prefers_the_publication_over_the_glossary_entry():
    # PRM APPROACH's "Refer to AIM" row was resolved by the parser to the
    # glossary's own AIM term; the publication index must still win.
    from far_aim.generate import aim_notes
    from far_aim.generate.pcg_notes import ReferTargets, _reference_sections

    term_doc = {
        "id": "pcg-prm-approach",
        "term": "PRM APPROACH",
        "content": [
            {"type": "entry", "text": "PRM APPROACH-"},
            {
                "type": "reference",
                "kind": "refer",
                "label": "Refer to",
                "text": "AIM",
                "target": "pcg-aim",
                "source": {},
            },
        ],
    }
    targets = {"pcg-aim": ("AIM (PCG)", "AIM")}
    refer = ReferTargets(far=aim_notes.FarTargets(), aim_index_stem="AIM")
    assert _reference_sections(term_doc, targets, refer) == ["## References", "- [[AIM]]"]
    # Without the AIM corpus the glossary entry remains the best target.
    no_aim = ReferTargets(far=aim_notes.FarTargets())
    assert _reference_sections(term_doc, targets, no_aim) == [
        "## References",
        "- [[AIM (PCG)|AIM]]",
    ]
