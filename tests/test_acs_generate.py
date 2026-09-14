"""Phase 10a tests: canonical ACS JSON → Obsidian vault notes.

Golden files under ``tests/fixtures/vault/`` are the committed expected
Markdown for representative ACS notes generated from the fixture corpus
(a page subset of FAA-S-ACS-6C; see ``test_acs_source.py``), planned
together with the FAR part-91 slice, the AIM and the PCG fixture corpora so
cross-corpus naming, linking and alias rules are exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from far_aim.config import Config
from far_aim.generate import BuildError, acs_notes
from far_aim.generate.build import AcsLayer, build_registry, plan_vault, sync_vault
from far_aim.manifest import SourceState
from far_aim.models import cfr as cfr_model
from far_aim.parsers import acs as acs_parser
from far_aim.sources import acs as acs_source
from tests.test_acs_parser import SOURCE as ACS_SOURCE
from tests.test_acs_source import VERSION as ACS_VERSION
from tests.test_acs_source import fetch_fixture as acs_fetch_fixture
from tests.test_pcg_generate import _sources, aim_layer, far_docs, pcg_layer  # noqa: F401

VAULT_FIXTURES = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture(scope="module")
def acs_layer(tmp_path_factory) -> AcsLayer:
    _, result = acs_fetch_fixture(tmp_path_factory.mktemp("acs-layer"))
    metadata = acs_source.load_metadata(result.snapshot_dir)
    assert metadata is not None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(acs_parser, "REQUIRE_COMPLETE", False)
        docs = acs_parser.build_acs_docs(result.snapshot_dir, metadata, ACS_SOURCE)
    title_hash = cfr_model.canonical_hash({k: d["canonical_hash"] for k, d in docs.items()})
    return AcsLayer(docs=docs, title_hash=title_hash)


@pytest.fixture(scope="module")
def combined_plan(far_docs, aim_layer, pcg_layer, acs_layer) -> dict[tuple[str, ...], bytes]:  # noqa: F811
    sources = _sources(far_docs, aim_layer, pcg_layer)
    sources["acs_private_airplane"] = SourceState(
        accepted_version=ACS_VERSION,
        effective_date="2024-05-31",
        canonical_hash=acs_layer.title_hash,
    )
    title_hash = sources["ecfr_title_14"].canonical_hash
    return plan_vault(
        far_docs, "2026-08-19", title_hash, sources, aim_layer, pcg_layer, acs=acs_layer
    )


def test_acs_naming_rules():
    assert acs_notes.task_stem("PA.I.A") == "PA.I.A"
    assert acs_notes.area_stem("PA.XII") == "PA.XII"
    assert acs_notes.appendix_stem(2) == "ACS Appendix 2"
    assert acs_notes.element_block_id("PA.VIII.E.K1a") == "pa-viii-e-k1a"
    assert acs_notes.task_code_of("PA.I.B.K1e") == "PA.I.B"
    with pytest.raises(BuildError):
        acs_notes.task_code_of("PA.I.B")


@pytest.mark.parametrize(
    "name",
    [
        "PA.I.A.md",  # plain task: references, objective, three sections, glossary terms
        "PA.I.B.md",  # sub-elements nested under their parent
        "PA.XI.A.md",  # note, empty skills section, archived-free
        "PA.I.md",  # area index
        "ACS Appendix 2.md",  # prose appendix: headings, paragraphs, bullets
        "ACS Private Pilot Airplane.md",  # publication index with change-note block links
    ],
)
def test_golden_acs_note(combined_plan, name):
    match = [data for parts, data in combined_plan.items() if parts[-1] == name]
    assert len(match) == 1, f"note {name} not planned exactly once"
    assert match[0] == (VAULT_FIXTURES / name).read_bytes()


def test_combined_plan_shape(combined_plan, acs_layer):
    acs_files = [parts for parts in combined_plan if parts[0] == "ACS"]
    assert all(parts[1] == "Private Pilot Airplane" for parts in acs_files)
    tasks = sum(acs_parser.count_tasks(d) for d in acs_layer.docs.values())
    assert tasks == 12
    assert len(acs_files) == tasks + 3 + 3 + 1  # + areas, appendices, index
    for parts in combined_plan:
        assert not (parts[0] == "ACS" and parts[-1] == "Pilot Qualifications.md")


def test_registry_targets_and_no_title_aliases(far_docs, aim_layer, pcg_layer, acs_layer):  # noqa: F811
    registry = build_registry(far_docs, aim_layer, pcg_layer, None, acs_layer)
    assert registry.acs_note_count == 12 + 3 + 3 + 1
    stem, display = registry.acs_targets["acs-PA.I.A"]
    assert stem == "PA.I.A" and display == "PA.I.A — Pilot Qualifications"
    assert registry.stems["PA.I.A"] == ("ACS", "Private Pilot Airplane", "PA.I.A.md")
    assert registry.aliases["acs-PA.I.A"] == []  # titles are never aliases (§39.1)
    assert "PA.I.B.K1e" in registry.acs_element_codes
    assert "PA.I.B.K3d" in registry.acs_element_codes


def test_task_note_links_and_block_ids(combined_plan):
    note = combined_plan[("ACS", "Private Pilot Airplane", "PA.I.A.md")].decode()
    assert "- [[Part 91]]" in note  # the only referenced part the FAR slice holds
    assert "[[Part 61]]" not in note
    assert (
        "**PA.I.A.K1** Certification requirements, recent flight experience, and "
        "recordkeeping. ^pa-i-a-k1"
    ) in note
    assert "aliases:" not in note
    assert "## Glossary Terms" not in note  # no fixture glossary term in this task's wording
    index = combined_plan[
        ("ACS", "Private Pilot Airplane", "ACS Private Pilot Airplane.md")
    ].decode()
    assert "[[PA.I.B#^pa-i-b-k1e|PA.I.B.K1e]]" in index  # added code the layer holds
    assert "`PA.II.A.S4`" in index  # added code outside the fixture's areas
    assert "[[PA.XII.A#^pa-xii-a-s1|PA.XII.A.S1]]" in index  # archived placeholder


def test_home_and_status_describe_the_acs(combined_plan):
    home = combined_plan[("Home.md",)].decode()
    assert "[[ACS Private Pilot Airplane|" in home and "`ACS/`" in home
    assert "Search an ACS code" in home
    status = combined_plan[("Source Status.md",)].decode()
    assert "| Private Pilot Airplane ACS | FAA-S-ACS-6C — effective 2024-05-31 |" in status


def test_no_volatile_timestamps_in_acs_notes(combined_plan):
    for parts, data in combined_plan.items():
        if parts[0] == "ACS":
            text = data.decode()
            assert "retrieved_at" not in text and "2026-09-13T" not in text
            assert "extractor" not in text


def test_sync_writes_acs_tree_and_deletes_stale(tmp_path, combined_plan):
    config = Config.load(tmp_path)
    stats = sync_vault(config, combined_plan)
    assert stats.written == len(combined_plan)
    note = config.vault_dir / "ACS" / "Private Pilot Airplane" / "PA.I.A.md"
    assert note.exists()
    stale = config.vault_dir / "ACS" / "Private Pilot Airplane" / "PA.II.A.md"
    stale.write_bytes(note.read_bytes())
    stats = sync_vault(config, combined_plan)
    assert stats.deleted == 1 and not stale.exists()
    note.write_text("# my own\n", encoding="utf-8")
    with pytest.raises(BuildError, match="curated note at generated path"):
        sync_vault(config, combined_plan)


def test_render_blocks_rejects_unknown_types():
    with pytest.raises(BuildError, match="no renderer"):
        acs_notes.render_blocks([{"type": "mystery"}])
    with pytest.raises(BuildError, match="code fence"):
        acs_notes.render_blocks([{"type": "preformatted", "lines": ["```"]}])


def test_glossary_terms_link_pcg_entries(acs_layer, pcg_layer):  # noqa: F811
    """Task wording that uses a glossary term links it under ## Glossary Terms."""
    from far_aim.links import glossary

    task = dict(acs_layer.docs["area-01"]["tasks"][0])
    task["objective"] = "Sequence with known traffic in the pattern."
    publication = acs_layer.docs["publication"]
    index = glossary.GlossaryIndex.build(pcg_layer.docs, pcg_layer.gate)
    targets = {"pcg-known-traffic": ("KNOWN TRAFFIC", "KNOWN TRAFFIC")}
    context = acs_notes.AcsContext(
        publication=publication,
        glossary=acs_notes.GlossaryLinks(index, targets),
    )
    note = acs_notes.build_task_note(task, acs_layer.docs["area-01"], [], context)
    assert note.body.rstrip().endswith("## Glossary Terms\n\n- [[KNOWN TRAFFIC|KNOWN TRAFFIC]]")
    plain = acs_notes.build_task_note(
        task, acs_layer.docs["area-01"], [], acs_notes.AcsContext(publication=publication)
    )
    assert "## Glossary Terms" not in plain.body
