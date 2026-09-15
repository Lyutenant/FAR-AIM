"""Phase 10b tests: the curated exam-prep layer (plan §39.3–§39.4).

The ACS element map and the study guide are loaded, verified and rendered
against the fixture corpora: the FAR part-91 slice, the AIM and PCG
fixtures, and the ACS page subset (Areas I, XI, XII). Synthetic files keep
the tests independent of the committed curation, which the full-title
build test exercises for real.
"""

from __future__ import annotations

import json

import pytest

from far_aim.config import Config
from far_aim.generate import BuildError, prep_notes
from far_aim.generate.build import plan_vault, sync_vault
from far_aim.generate.enrich import EnrichmentLayer
from far_aim.links import study
from far_aim.links.citations import collect_text
from far_aim.manifest import SourceState
from tests.test_acs_generate import acs_layer  # noqa: F401
from tests.test_acs_source import VERSION as ACS_VERSION
from tests.test_pcg_generate import _sources, aim_layer, far_docs, pcg_layer  # noqa: F401

ACS_MAP = {
    "schema": 1,
    "tasks": {
        "PA.I.A": {"far": ["91.155", "Part 91"]},
        "PA.I.B": {"out_of_corpus": "FAA-H-8083-25 — airworthiness background"},
        "PA.I.C": {"aim": ["4-1-15"]},
        "PA.I.D": {"far": ["91.155"]},
        "PA.I.E": {"far": ["91.155"], "aim": ["4-1-2"]},
        "PA.I.F": {"out_of_corpus": "FAA-H-8083-25 ch. 11"},
        "PA.I.G": {"out_of_corpus": "FAA-H-8083-25 ch. 7"},
        "PA.I.H": {"out_of_corpus": "FAA-H-8083-25 ch. 17"},
        "PA.I.I": {"out_of_corpus": "FAA-H-8083-23"},
        "PA.XI.A": {"far": ["91.175"]},
        "PA.XII.A": {"out_of_corpus": "FAA-H-8083-3 ch. 2"},
        "PA.XII.B": {"out_of_corpus": "FAA-H-8083-23"},
    },
    "elements": {
        "PA.I.A.K1": {"far": ["91.175"], "pcg": ["KNOWN TRAFFIC"]},
        "PA.I.B.K3": {"far": ["91.155"]},
    },
}

STUDY = {
    "schema": 1,
    "stages": {"pre-solo": "First rules.", "solo-xc": "Leaving the pattern."},
    "entries": {
        "91.155": {
            "gist": "The VFR weather minimums table.",
            "why": "Most tested rule.",
            "numbers": [
                {"value": "3 SM", "quote": "3 statute miles"},
                {
                    "value": "Ceiling 1,000 ft",
                    "quote": "when the ceiling is less than 1,000 feet",
                    "where": "(c)",
                },
            ],
            "traps": ["Class G at night is different."],
            "questions": [
                {"q": "Minimums in Class E?", "a": "3-152.", "cite": ["91.155", "4-1-15"]}
            ],
            "acs": ["PA.I.E.K1"],
            "stage": "solo-xc",
        },
        "4-1-15": {
            "gist": "Radar traffic information is advisory.",
            "why": "Flight following.",
            "numbers": [{"value": "Clock position", "quote": "clock"}],
            "questions": [{"q": "Is flight following separation?", "a": "No."}],
            "acs": [],
            "stage": "pre-solo",
            "review": "reviewed",
        },
    },
    "oral": {
        "PA.I.A": [
            {
                "scenario": "You last flew 95 days ago. Passengers tonight?",
                "answer": "Not until three landings; full stop for night.",
                "cite": ["91.155"],
                "find_it": "Part 61 subpart A, the 61.5x range",
            },
            {"scenario": "Second one.", "answer": "Short answer.", "cite": []},
        ]
    },
}


def _enrichment(acs_map: dict | None, guide: dict | None) -> EnrichmentLayer:
    return EnrichmentLayer(
        acs_map=study.AcsMap.from_data(acs_map, where="acs-map") if acs_map else None,
        study=study.StudyGuide.from_data(guide, where="ppl-study") if guide else None,
    )


def _plan(far_docs, aim_layer, pcg_layer, acs_layer, acs_map, guide):  # noqa: F811
    sources = _sources(far_docs, aim_layer, pcg_layer)
    sources["acs_private_airplane"] = SourceState(
        accepted_version=ACS_VERSION,
        effective_date="2024-05-31",
        canonical_hash=acs_layer.title_hash,
    )
    return plan_vault(
        far_docs,
        "2026-08-19",
        sources["ecfr_title_14"].canonical_hash,
        sources,
        aim_layer,
        pcg_layer,
        _enrichment(acs_map, guide),
        acs=acs_layer,
    )


@pytest.fixture(scope="module")
def prep_plan(far_docs, aim_layer, pcg_layer, acs_layer):  # noqa: F811
    return _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, STUDY)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def test_acs_map_shape_rules():
    acs_map = study.AcsMap.from_data(ACS_MAP, where="m")
    assert acs_map.tasks["PA.I.A"].stems == ("91.155", "Part 91")
    assert acs_map.entry_for("PA.I.A.K1a", "PA.I.A.K1", "PA.I.A").far == ("91.175",)  # parent
    assert acs_map.entry_for("PA.I.A.K2", None, "PA.I.A") is acs_map.tasks["PA.I.A"]  # task default
    assert acs_map.entry_for("PA.II.A.K1", None, "PA.II.A") is None
    for broken, message in (
        ({"schema": 2}, "unsupported schema"),
        ({"schema": 1, "tasks": {"PA.I": {"far": ["91.155"]}}}, "not a task code"),
        ({"schema": 1, "elements": {"PA.I.A.K1": {"far": []}}}, "not both and not neither"),
        (
            {"schema": 1, "elements": {"PA.I.A.K1": {"far": ["x"], "out_of_corpus": "y"}}},
            "not both",
        ),
        ({"schema": 1, "elements": {"PA.I.A.K1": {"sections": ["91.155"]}}}, "unknown field"),
        ({"schema": 1, "elements": {"PA.I.A.K1": {"far": ["91.155", "91.155"]}}}, "duplicate"),
    ):
        with pytest.raises(BuildError, match=message):
            study.AcsMap.from_data(broken, where="m")


def test_study_guide_shape_rules():
    guide = study.StudyGuide.from_data(STUDY, where="s")
    entry = guide.entries["91.155"]
    assert entry.numbers[1].where == "(c)" and entry.questions[0].cite == ("91.155", "4-1-15")
    assert guide.entries["4-1-15"].questions[0].cite == ("4-1-15",)  # defaults to the entry
    assert guide.entries["4-1-15"].review == "reviewed" and entry.review == "unreviewed"
    assert [e.stem for e in guide.by_stage()["solo-xc"]] == ["91.155"]
    assert guide.oral_areas() == ["I"]
    assert guide.oral["PA.I.A"][0].find_it == "Part 61 subpart A, the 61.5x range"
    assert guide.oral["PA.I.A"][1].cite == () and guide.oral["PA.I.A"][1].find_it is None
    for oral, message in (
        ({"PA.I.A.K1": [{"scenario": "s", "answer": "a"}]}, "not an ACS Task code"),
        ({"PA.I.A": []}, "non-empty list"),
        ({"PA.I.A": [{"scenario": "s"}]}, "expected text"),
        ({"PA.I.A": [{"scenario": "s", "answer": "a", "hint": "h"}]}, "bad shape"),
    ):
        data = {**STUDY, "oral": oral}
        with pytest.raises(BuildError, match=message):
            study.StudyGuide.from_data(data, where="s")
    base = {"gist": "g", "why": "w", "questions": [{"q": "q", "a": "a"}], "stage": "pre-solo"}
    for patch, message in (
        ({"stage": "checkride"}, "is not one of"),
        ({"questions": []}, "at least one question"),
        ({"numbers": [{"value": "1", "quote": "x", "where": "a"}]}, "paragraph label path"),
        ({"review": "maybe"}, "review must be"),
        ({"extra": 1}, "unknown field"),
        ({"gist": " padded"}, "untrimmed"),
    ):
        data = {"schema": 1, "stages": STUDY["stages"], "entries": {"91.155": {**base, **patch}}}
        with pytest.raises(BuildError, match=message):
            study.StudyGuide.from_data(data, where="s")


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_verbatim_numbers_gate(far_docs, aim_layer, pcg_layer, acs_layer):  # noqa: F811
    guide = json.loads(json.dumps(STUDY))
    guide["entries"]["91.155"]["numbers"].append({"value": "x", "quote": "four statute miles"})
    with pytest.raises(BuildError, match="does not occur verbatim in the official text of 91.155"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, guide)
    guide = json.loads(json.dumps(STUDY))
    guide["entries"]["91.155"]["numbers"] = [
        {"value": "x", "quote": "3 statute miles", "where": "(c)"}  # right words, wrong paragraph
    ]
    with pytest.raises(BuildError, match="paragraph \\(c\\) of 91.155"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, guide)
    guide["entries"]["91.155"]["numbers"] = [{"value": "x", "quote": "y", "where": "(z)"}]
    with pytest.raises(BuildError, match="cannot read the official text of paragraph \\(z\\)"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, guide)


def test_far_paragraph_text_walks_label_paths(far_docs):  # noqa: F811
    from far_aim.generate.build import _iter_documents

    section = next(c for c in _iter_documents(far_docs["91"]) if c.get("section") == "91.155")
    whole = study.far_paragraph_text(section, None, collect_text)
    assert (
        whole is not None and "3 statute miles" in whole and "ceiling is less than 1,000" in whole
    )
    para_c = study.far_paragraph_text(section, "(c)", collect_text)
    assert para_c is not None and "ceiling is less than 1,000 feet" in para_c
    assert "3 statute miles" not in para_c
    assert study.far_paragraph_text(section, "(b)(1)", collect_text) is not None
    assert study.far_paragraph_text(section, "(q)", collect_text) is None


def test_unresolved_stems_codes_and_coverage_fail(far_docs, aim_layer, pcg_layer, acs_layer):  # noqa: F811
    bad_map = json.loads(json.dumps(ACS_MAP))
    bad_map["elements"]["PA.I.A.K1"] = {"far": ["91.9999"]}
    with pytest.raises(BuildError, match="names '91.9999', which no generated note"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, bad_map, STUDY)
    bad_map = json.loads(json.dumps(ACS_MAP))
    bad_map["elements"]["PA.II.A.K1"] = {"far": ["91.155"]}
    with pytest.raises(BuildError, match="names an element the ACS does not have"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, bad_map, STUDY)
    bad_map = json.loads(json.dumps(ACS_MAP))
    del bad_map["tasks"]["PA.I.D"]
    with pytest.raises(BuildError, match="PA.I.D.K1 .* neither mapped nor marked out_of_corpus"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, bad_map, STUDY)
    bad_guide = json.loads(json.dumps(STUDY))
    bad_guide["entries"]["91.155"]["acs"] = ["PA.II.A.K1"]
    with pytest.raises(BuildError, match="ACS code 'PA.II.A.K1' is not in the accepted ACS"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, bad_guide)
    bad_guide = json.loads(json.dumps(STUDY))
    bad_guide["entries"]["91.155"]["questions"][0]["cite"] = ["Part 61"]
    with pytest.raises(BuildError, match="not a FAR section or AIM paragraph the vault holds"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, bad_guide)
    bad_guide = json.loads(json.dumps(STUDY))
    bad_guide["oral"]["PA.II.A"] = [{"scenario": "s", "answer": "a"}]
    with pytest.raises(BuildError, match="oral\\[PA.II.A\\]: Task 'PA.II.A' is not in the"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, bad_guide)
    bad_guide = json.loads(json.dumps(STUDY))
    bad_guide["oral"]["PA.I.A"][0]["cite"] = ["91.9999"]
    with pytest.raises(BuildError, match="oral\\[PA.I.A\\]\\[0\\]: '91.9999' is not a FAR"):
        _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, bad_guide)


def test_map_without_acs_layer_and_codes_without_acs_fail(far_docs, aim_layer, pcg_layer):  # noqa: F811
    sources = _sources(far_docs, aim_layer, pcg_layer)
    with pytest.raises(BuildError, match="no ACS layer is built"):
        plan_vault(
            far_docs,
            "2026-08-19",
            sources["ecfr_title_14"].canonical_hash,
            sources,
            aim_layer,
            pcg_layer,
            _enrichment(ACS_MAP, None),
        )
    with pytest.raises(BuildError, match="no ACS layer is built"):
        plan_vault(
            far_docs,
            "2026-08-19",
            sources["ecfr_title_14"].canonical_hash,
            sources,
            aim_layer,
            pcg_layer,
            _enrichment(None, STUDY),
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_study_callout_precedes_official_text(prep_plan):
    note = prep_plan[("FAR", "Part 091", "91.155.md")].decode()
    callout_at = note.index("> [!study] Private Pilot study aid — curated, not official text")
    assert note.index("> [!info] Source") < callout_at < note.index("## Official Text")
    assert "> **Gist:** The VFR weather minimums table." in note
    assert "> **Numbers:** 3 SM · Ceiling 1,000 ft" in note
    assert "> **Watch out:** Class G at night is different." in note
    assert "**ACS:** [[PA.I.E#^pa-i-e-k1|PA.I.E.K1]] — **Stage:** solo-xc" in note
    aim_note = prep_plan[("AIM", "Chapter 04", "4-1-15.md")].decode()
    assert "> [!study]" in aim_note and aim_note.index("> [!study]") < aim_note.index(
        "## Official Text"
    )
    assert "**ACS:**" not in aim_note and "**Stage:** pre-solo" in aim_note
    untouched = prep_plan[("FAR", "Part 091", "91.175.md")].decode()
    assert "[!study]" not in untouched


def test_where_to_study_section_on_acs_tasks(prep_plan):
    note = prep_plan[("ACS", "Private Pilot Airplane", "PA.I.A.md")].decode()
    section = note.split("## Where to study (curated)", 1)[1]
    minimums = "[[91.155|§ 91.155 — Basic VFR weather minimums]]"
    assert f"- **All Knowledge and Risk elements** — {minimums}, [[Part 91]]" in section
    ifr = "[[91.175|§ 91.175 — Takeoff and landing under IFR]]"
    assert f"- **PA.I.A.K1** — {ifr}, [[KNOWN TRAFFIC]]" in section
    outside = prep_plan[("ACS", "Private Pilot Airplane", "PA.I.F.md")].decode()
    assert "not in the FAR/AIM: FAA-H-8083-25 ch. 11" in outside
    airworthiness = prep_plan[("ACS", "Private Pilot Airplane", "PA.I.B.md")].decode()
    assert (
        "- **PA.I.B.K3** — [[91.155|" in airworthiness
    )  # element override beside the task default


def test_prep_notes_render(prep_plan):
    paths = {p for p in prep_plan if p[0] == "Prep"}
    assert paths == {
        ("Prep", "Private Pilot", "Private Pilot Prep.md"),
        ("Prep", "Private Pilot", "Part 91 Map.md"),
        ("Prep", "Private Pilot", "Numbers Sheet.md"),
        ("Prep", "Private Pilot", "Where Do I Look.md"),
        ("Prep", "Private Pilot", "Reading Path.md"),
        ("Prep", "Private Pilot", "ACS Checklist.md"),
        ("Prep", "Private Pilot", "Oral Prep", "Oral Prep I.md"),
        ("Prep", "Private Pilot", "anki", "private-pilot.txt"),
    }
    index = prep_plan[("Prep", "Private Pilot", "Private Pilot Prep.md")].decode()
    assert "- [[ACS Checklist]] —" in index and "[[Oral Prep I|I]]" in index
    assert "`anki/private-pilot.txt`" in index
    assert 'type: "prep"' in index and "generated: true" in index
    assert "| [[PA.I|I. Preflight Preparation]] |" in index
    assert "| **Total** |" in index
    assert (
        "2 covered sections and paragraphs, 1 reviewed by a human and 1 still unreviewed" in index
    )
    minimums = "[[91.155|§ 91.155 — Basic VFR weather minimums]]"
    assert f"- {minimums} — The VFR weather minimums table." in index
    part_map = prep_plan[("Prep", "Private Pilot", "Part 91 Map.md")].decode()
    assert "## Subpart B — Flight Rules" in part_map and "**1 studied**" in part_map
    numbers = prep_plan[("Prep", "Private Pilot", "Numbers Sheet.md")].decode()
    assert "## Area of Operation I — Preflight Preparation" in numbers
    assert (
        f"| **Ceiling 1,000 ft** | when the ceiling is less than 1,000 feet | {minimums} (c) |"
        in numbers
    )
    assert "## General" in numbers  # the AIM entry names no ACS code
    lookup = prep_plan[("Prep", "Private Pilot", "Where Do I Look.md")].decode()
    assert "### [[PA.I.E|PA.I.E — National Airspace System]]" in lookup
    radar = "[[4-1-15|AIM 4-1-15 — Radar Traffic Information Service]]"
    assert f"| Minimums in Class E? | 3-152. | {minimums}, {radar} |" in lookup
    assert "## By citation" in lookup
    path = prep_plan[("Prep", "Private Pilot", "Reading Path.md")].decode()
    assert path.index("## pre-solo") < path.index("## solo-xc")
    assert "**ACS Tasks this stage touches:** [[PA.I.E|PA.I.E — National Airspace System]]" in path
    assert "- [[4-1-15|AIM 4-1-15 — Radar Traffic Information Service]] — Radar traffic" in path
    home = prep_plan[("Home.md",)].decode()
    assert "[[Private Pilot Prep]]" in home and "`Prep/`" in home and "Oral Prep" in home


def test_oral_prep_and_checklist_render(prep_plan):
    oral = prep_plan[("Prep", "Private Pilot", "Oral Prep", "Oral Prep I.md")].decode()
    assert 'id: "prep-oral-i"' in oral and "# Oral Prep — I. Preflight Preparation" in oral
    assert "## [[PA.I.A|PA.I.A — Pilot Qualifications]]" in oral
    # Elements verbatim from the ACS, as block links onto the Task note.
    assert "- [[PA.I.A#^pa-i-a-k1|PA.I.A.K1]] Certification requirements" in oral
    assert "**Scenario 1.** You last flew 95 days ago. Passengers tonight?" in oral
    assert "**Answer (study aid):** Not until three landings; full stop for night." in oral
    assert "**Find it:** Part 61 subpart A, the 61.5x range" in oral
    assert "**Cites:** [[91.155|§ 91.155 — Basic VFR weather minimums]]" in oral
    assert "**Scenario 2.** Second one.\n**Answer (study aid):** Short answer.\n\n" in oral
    assert "## [[PA.I.B|PA.I.B — Airworthiness Requirements]]" in oral
    assert "_No scenarios yet._" in oral
    checklist = prep_plan[("Prep", "Private Pilot", "ACS Checklist.md")].decode()
    assert "> [!warning] Template — copy it before you tick it" in checklist
    assert "## I. Preflight Preparation" in checklist
    assert "### [[PA.I.A|PA.I.A — Pilot Qualifications]]" in checklist
    assert "- [ ] **PA.I.A.K1** Certification requirements" in checklist
    assert "    - [ ] **PA.I.B.K1a**" in checklist  # sub-elements nested
    assert "**PA.I.A.S1**" not in checklist  # Knowledge and Risk only


def test_anki_export(far_docs, aim_layer, pcg_layer, acs_layer, prep_plan):  # noqa: F811
    data = prep_plan[("Prep", "Private Pilot", "anki", "private-pilot.txt")]
    lines = data.decode().split("\n")
    assert lines[:7] == [
        "#separator:tab",
        "#html:true",
        "#guid column:1",
        "#notetype column:2",
        "#deck column:3",
        "#tags column:6",
        prep_notes.EXPORT_MARKER,
    ]
    rows = [line.split("\t") for line in lines[7:] if line]
    assert all(len(r) == 6 for r in rows)
    kinds = {r[1] for r in rows}
    assert kinds == {"Basic", "Basic (and reversed card)", "Cloze"}
    gist = next(r for r in rows if r[1] == "Basic (and reversed card)")
    assert gist[2:] == [
        "Private Pilot::I. Preflight Preparation",
        "§ 91.155 — Basic VFR weather minimums",
        "The VFR weather minimums table.",
        "far-aim ppl::solo-xc cite::91.155",
    ]
    question = next(r for r in rows if r[3] == "Minimums in Class E?")
    assert question[4].startswith("3-152.<br><br>§ 91.155 — Basic VFR weather minimums; AIM 4-1-15")
    clozes = [r for r in rows if r[1] == "Cloze"]
    # Value in the gist → clozed there; otherwise a standalone cloze with the quote as extra.
    assert clozes[0][3] == "§ 91.155 — Basic VFR weather minimums: {{c1::3 SM}}"
    assert clozes[0][4] == "“3 statute miles” (§ 91.155 — Basic VFR weather minimums)"
    assert clozes[1][4].endswith("(§ 91.155 — Basic VFR weather minimums (c))")
    element = next(r for r in rows if r[3].startswith("<b>PA.I.A.K1</b>"))
    assert element[4] == "§ 91.175 — Takeoff and landing under IFR<br>KNOWN TRAFFIC"
    outside = next(r for r in rows if r[3].startswith("<b>PA.I.B.K1</b>"))
    assert outside[4] == "Not in the FAR/AIM: FAA-H-8083-25 — airworthiness background"
    assert outside[2] == "Private Pilot::I. Preflight Preparation"
    assert outside[5] == "far-aim acs::PA.I.B"
    # Deterministic and GUID-stable across rebuilds (plan §39.5).
    again = _plan(far_docs, aim_layer, pcg_layer, acs_layer, ACS_MAP, STUDY)
    assert again[("Prep", "Private Pilot", "anki", "private-pilot.txt")] == data
    guids = [r[0] for r in rows]
    assert len(set(guids)) == len(guids) and all(len(g) == 16 for g in guids)


def test_separability(far_docs, aim_layer, pcg_layer, acs_layer, prep_plan):  # noqa: F811
    """Without the two files nothing but the Prep root and the callouts differs (§32.12)."""
    sources = _sources(far_docs, aim_layer, pcg_layer)
    sources["acs_private_airplane"] = SourceState(
        accepted_version=ACS_VERSION,
        effective_date="2024-05-31",
        canonical_hash=acs_layer.title_hash,
    )
    bare = plan_vault(
        far_docs,
        "2026-08-19",
        sources["ecfr_title_14"].canonical_hash,
        sources,
        aim_layer,
        pcg_layer,
        None,
        acs=acs_layer,
    )
    assert set(prep_plan) - set(bare) == {p for p in prep_plan if p[0] == "Prep"}
    changed = {p for p in bare if bare[p] != prep_plan[p]}
    assert changed == {
        ("FAR", "Part 091", "91.155.md"),
        ("AIM", "Chapter 04", "4-1-15.md"),
        ("Home.md",),
        *{p for p in bare if p[0] == "ACS" and p[-1].startswith("PA.") and p[-1].count(".") == 3},
    }


def test_sync_owns_the_prep_root(tmp_path, prep_plan):
    config = Config.load(tmp_path)
    sync_vault(config, prep_plan)
    stale = config.vault_dir / "Prep" / "Private Pilot" / "Old Sheet.md"
    stale.write_bytes(prep_plan[("Prep", "Private Pilot", "Numbers Sheet.md")])
    stats = sync_vault(config, prep_plan)
    assert stats.deleted == 1 and not stale.exists()
    study_note = config.vault_dir / "Study" / "mine.md"
    study_note.parent.mkdir()
    study_note.write_text("# mine\n", encoding="utf-8")
    sync_vault(config, prep_plan)
    assert study_note.exists()  # the reader's folder is never touched
    # A non-note file under Prep is owned only by the export marker.
    anki_dir = config.vault_dir / "Prep" / "Private Pilot" / "anki"
    stale_export = anki_dir / "old.txt"
    stale_export.write_text(f"#separator:tab\n{prep_notes.EXPORT_MARKER}\nrow\n", encoding="utf-8")
    curated_export = anki_dir / "mine.txt"
    curated_export.write_text("#separator:tab\nmy cards\n", encoding="utf-8")
    stats = sync_vault(config, prep_plan)
    assert stats.deleted == 1 and not stale_export.exists() and curated_export.exists()
    assert any("mine.txt" in w for w in stats.warnings)
    planned_export = anki_dir / "private-pilot.txt"
    planned_export.write_text("#separator:tab\nhand-written\n", encoding="utf-8")
    with pytest.raises(BuildError, match="curated file at generated path"):
        sync_vault(config, prep_plan)


def test_code_link_helper():
    assert prep_notes.code_link("PA.I.A.K1", frozenset({"PA.I.A.K1"})) == (
        "[[PA.I.A#^pa-i-a-k1|PA.I.A.K1]]"
    )
    assert prep_notes.code_link("PA.II.A.K1", frozenset()) == "`PA.II.A.K1`"
