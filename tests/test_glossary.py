"""Phase 6 tests: AIM → PCG glossary term recognition (plan §12.2)."""

from __future__ import annotations

import json

import pytest

from far_aim.generate import BuildError
from far_aim.links import glossary


def _term(term: str, *blocks: dict, letter: str = "A") -> dict:
    tid = "pcg-" + term.lower().replace(" ", "-")
    entry = {"type": "entry", "text": f"{term}-"}
    return {"id": tid, "term": term, "letter": letter, "content": [entry, *blocks]}


def _see(target: str) -> dict:
    return {"type": "reference", "kind": "see", "label": "See", "text": target, "target": None}


def _defn(text: str) -> dict:
    return {"type": "text", "text": text}


PCG = {
    "letter-a": {
        "terms": [
            _term("AIR TRAFFIC CONTROL", _defn("A service…")),
            _term("AIR TRAFFIC", _defn("Aircraft operating…")),
            _term("ATC", _see("AIR TRAFFIC CONTROL")),
            _term("AIRSPACE FLOW PROGRAM (AFP)", _defn("A traffic management…")),
            _term("AIRCRAFT", _defn("Device(s)…")),
            _term("AC", _see("ADVISORY CIRCULAR")),
            _term("ADS-B", _see("AUTOMATIC DEPENDENT SURVEILLANCE-BROADCAST")),
            _term("ADS [ICAO]", _defn("Automatic dependent surveillance.")),
            _term("VFR‐ON‐TOP", _defn("ATC authorization…")),
            _term("VFR", _see("VISUAL FLIGHT RULES")),
            _term("AERODROME", _defn("A defined area…")),
            _term("AERODROME [ICAO]", _defn("ICAO wording…")),
            _term("PILOT'S DISCRETION", _defn("When used…")),
            _term("TRANSPONDER", _defn("The airborne radar beacon…")),
            _term("CAT", _see("CLEAR-AIR TURBULENCE")),
            _term("CLEAR-AIR TURBULENCE (CAT)", _defn("Turbulence…")),
            _term("HEAVY (AIRCRAFT)", _defn("Phraseology placeholder…")),
            _term("EXPECT DEPARTURE CLEARANCE TIME (FIX)", _defn("Placeholder…")),
            _term("NOTAM", _see("NOTICE TO AIRMEN")),
            _term("NOTICE TO AIRMEN (NOTAM)", _defn("A notice…")),
            _term("TERMINAL RADAR APPROACH CONTROL (TRACON)", _defn("A facility…")),
            # Upstream typography: an en-dash entry label, a Unicode-hyphen
            # acronym, a bracket-qualified See-only entry.
            {"id": "pcg-saw", "term": "SAW", "letter": "S",
             "content": [{"type": "entry", "text": "SAW–"}, _see("SEVERE WEATHER…")]},
            {"id": "pcg-e-msaw", "term": "E‐MSAW", "letter": "E",
             "content": [{"type": "entry", "text": "E‐MSAW-"}, _see("EN ROUTE MSAW")]},
            {"id": "pcg-aip-icao", "term": "AIP [ICAO]", "letter": "A",
             "content": [{"type": "entry", "text": "AIP [ICAO]-"}, _see("AERONAUTICAL…")]},
            _term("AUTOMATIC DEPENDENT SURVEILLANCE (ADS)", _defn("A surveillance…")),
        ]
    }
}


def _ids(found: set[str]) -> list[str]:
    return sorted(found)


def test_phrases_match_case_insensitively_and_longest_wins():
    index = glossary.GlossaryIndex.build(PCG, glossary.Gate())
    found = index.find(["Contact air traffic control; air traffic is heavy."])
    assert _ids(found) == ["pcg-air-traffic", "pcg-air-traffic-control"]
    # One mention of the longer phrase is not also the shorter one.
    assert _ids(index.find(["Air Traffic Control services"])) == ["pcg-air-traffic-control"]


def test_acronyms_match_only_in_capitals_and_at_three_letters():
    index = glossary.GlossaryIndex.build(PCG, glossary.Gate())
    assert _ids(index.find(["contact ATC on 121.5"])) == ["pcg-atc"]
    assert index.find(["the atc facility"]) == set()
    assert index.find(["an AC describes"]) == set()  # two letters: never
    # A parenthetical acronym links its phrase entry.
    assert _ids(index.find(["an AFP is in effect"])) == ["pcg-airspace-flow-program-(afp)"]
    assert _ids(index.find(["the airspace flow program"])) == ["pcg-airspace-flow-program-(afp)"]


def test_single_defined_words_need_the_gate():
    assert glossary.GlossaryIndex.build(PCG, glossary.Gate()).find(["the aircraft"]) == set()
    gate = glossary.Gate(allow_words={"TRANSPONDER": "aviation noun"})
    index = glossary.GlossaryIndex.build(PCG, gate)
    assert _ids(index.find(["Transponder required; aircraft too."])) == ["pcg-transponder"]
    gate = glossary.Gate(allow_acronyms={"ADS [ICAO]": "capitals only"})
    index = glossary.GlossaryIndex.build(PCG, gate)
    assert _ids(index.find(["ADS contracts"])) == ["pcg-ads-[icao]"]
    assert index.find(["ads contracts"]) == set()


def test_hyphenated_see_only_acronyms_stay_capitals_only():
    index = glossary.GlossaryIndex.build(PCG, glossary.Gate())
    assert _ids(index.find(["ADS-B Out equipment"])) == ["pcg-ads-b"]
    assert index.find(["ads-b equipment", "Ads-B"]) == set()
    assert "ADS-B" in index.sensitive and "ads-b" not in index.insensitive


def test_overlapping_classes_take_the_longest_alias():
    gate = glossary.Gate(allow_acronyms={"ADS [ICAO]": "capitals only"})
    index = glossary.GlossaryIndex.build(PCG, gate)
    assert _ids(index.find(["ADS-B Out equipment"])) == ["pcg-ads-b"]
    assert _ids(index.find(["operating VFR-on-top"])) == ["pcg-vfr‐on‐top"]
    assert _ids(index.find(["operating VFR today"])) == ["pcg-vfr"]


def test_unicode_hyphen_and_apostrophe_fold():
    index = glossary.GlossaryIndex.build(PCG, glossary.Gate())
    assert _ids(index.find(["at pilot’s discretion"])) == ["pcg-pilot's-discretion"]
    assert _ids(index.find(["VFR‐ON‐TOP"])) == ["pcg-vfr‐on‐top"]


def test_bracket_qualified_duplicate_goes_to_exact_term():
    index = glossary.GlossaryIndex.build(PCG, glossary.Gate())
    assert index.insensitive.get("aerodrome") is None  # single words are gated…
    gate = glossary.Gate(allow_words={"AERODROME": "x", "AERODROME [ICAO]": "y"})
    index = glossary.GlossaryIndex.build(PCG, gate)
    assert _ids(index.find(["the aerodrome"])) == ["pcg-aerodrome"]


def test_deny_blocks_the_alias_for_every_term():
    gate = glossary.Gate(deny={"CAT": "approach categories"})
    index = glossary.GlossaryIndex.build(PCG, gate)
    # Neither the CAT entry nor CLEAR-AIR TURBULENCE (CAT)'s parenthetical
    # links a capital CAT, but the expanded phrase still does.
    assert index.find(["CAT II approach"]) == set()
    assert _ids(index.find(["expect clear-air turbulence"])) == ["pcg-clear-air-turbulence-(cat)"]
    assert glossary.GlossaryIndex.build(PCG, glossary.Gate()).find(["CAT II"]) == {"pcg-cat"}


def test_parenthetical_placeholders_are_not_acronyms():
    index = glossary.GlossaryIndex.build(PCG, glossary.Gate())
    assert index.find(["AIRCRAFT on final", "the FIX inbound"]) == set()
    assert "AIRCRAFT" not in index.sensitive and "FIX" not in index.sensitive
    # Evidenced parentheticals still link: an initialism, a contraction
    # readable as one, or the PCG's own See-only abbreviation entry.
    assert _ids(index.find(["an AFP is in effect"])) == ["pcg-airspace-flow-program-(afp)"]
    assert _ids(index.find(["the TRACON"])) == ["pcg-terminal-radar-approach-control-(tracon)"]
    assert _ids(index.find(["check NOTAM data"])) == ["pcg-notam"]  # exact entry wins


def test_is_initialism_rules():
    assert glossary.is_initialism("AFP", "AIRSPACE FLOW PROGRAM")
    assert glossary.is_initialism("CAT", "CLEAR-AIR TURBULENCE")
    assert glossary.is_initialism("TRACON", "TERMINAL RADAR APPROACH CONTROL")
    assert glossary.is_initialism("NOTAM", "NOTICE TO AIRMEN")
    assert glossary.is_initialism("RID", "REMOTE IDENTIFICATION")
    assert glossary.is_initialism("ICAO", "INTERNATIONAL CIVIL AVIATION ORGANIZATION")
    # Backtracking: the first word's inner letters must not swallow a later
    # word's initial (STOL is not S-T from SHORT).
    assert glossary.is_initialism("STOL", "SHORT TAKEOFF AND LANDING AIRCRAFT")
    assert glossary.is_initialism("VTOL", "VERTICAL TAKEOFF AND LANDING AIRCRAFT")
    assert glossary.is_initialism("CAS", "CALIBRATED AIRSPEED")
    # A contraction is still readable: AIR(men's) MET(eorological).
    assert glossary.is_initialism("AIRMET", "AIRMEN'S METEOROLOGICAL INFORMATION")
    assert not glossary.is_initialism("AIRCRAFT", "HEAVY")
    assert not glossary.is_initialism("FIX", "EXPECT DEPARTURE CLEARANCE TIME")
    assert not glossary.is_initialism("FACILITY", "CONTACT")
    assert not glossary.is_initialism("", "ANY")


def test_gate_names_must_exist_and_carry_reasons(tmp_path):
    with pytest.raises(BuildError, match="unknown PCG term"):
        glossary.GlossaryIndex.build(PCG, glossary.Gate(deny={"NOPE": "x"}))
    path = tmp_path / glossary.GATE_FILENAME

    def gate_json(deny: list[dict]) -> str:
        return json.dumps({"deny": deny, "allow_words": [], "allow_acronyms": []})

    path.write_text(gate_json([{"term": "CAT"}]), encoding="utf-8")
    with pytest.raises(BuildError, match="term and reason"):
        glossary.Gate.load(path)
    # Wrong types and blank strings fail as gate errors, never as TypeErrors.
    for bad in ({"term": ["CAT"], "reason": "x"}, {"term": "CAT", "reason": 3},
                {"term": " ", "reason": "x"}, {"term": "CAT", "reason": ""}, "CAT"):
        path.write_text(gate_json([bad]), encoding="utf-8")
        with pytest.raises(BuildError, match="non-empty string term and reason"):
            glossary.Gate.load(path)
    path.write_text(
        gate_json([{"term": "CAT", "reason": "a"}, {"term": "CAT", "reason": "b"}]),
        encoding="utf-8",
    )
    with pytest.raises(BuildError, match="duplicate"):
        glossary.Gate.load(path)
    with pytest.raises(BuildError, match="missing"):
        glossary.Gate.load(tmp_path / "missing.json")


def test_gate_schema_fails_closed(tmp_path):
    path = tmp_path / glossary.GATE_FILENAME
    empty = {"deny": [], "allow_words": [], "allow_acronyms": []}

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(BuildError, match="missing section"):
        glossary.Gate.load(path)

    incomplete = {k: v for k, v in empty.items() if k != "allow_words"}
    path.write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(BuildError, match="missing section.*allow_words"):
        glossary.Gate.load(path)

    misspelled = {**incomplete, "allow_wordz": []}
    path.write_text(json.dumps(misspelled), encoding="utf-8")
    with pytest.raises(BuildError, match="unknown section.*allow_wordz"):
        glossary.Gate.load(path)

    path.write_text(json.dumps({**empty, "deny": None}), encoding="utf-8")
    with pytest.raises(BuildError, match="deny must be a list"):
        glossary.Gate.load(path)

    path.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(BuildError, match="top level must be an object"):
        glossary.Gate.load(path)

    path.write_text(json.dumps({**empty, "_comment": "ok"}), encoding="utf-8")
    assert glossary.Gate.load(path) == glossary.Gate()


def test_committed_gate_matches_accepted_pcg_terms():
    # The real gate file must name only terms the parsed PCG defines; the
    # normalized layer is a local cache, so skip when it is absent.
    from pathlib import Path

    normalized = Path("data/normalized/pcg")
    if not normalized.is_dir():
        pytest.skip("normalized PCG layer not present")
    docs = {
        p.stem: json.loads(p.read_text(encoding="utf-8")) for p in normalized.glob("letter-*.json")
    }
    gate = glossary.Gate.load(Path("data/links") / glossary.GATE_FILENAME)
    glossary.GlossaryIndex.build(docs, gate)  # raises on a stale entry


def test_see_only_detection_normalizes_entry_labels():
    index = glossary.GlossaryIndex.build(PCG, glossary.Gate())
    assert _ids(index.find(["a SAW is issued"])) == ["pcg-saw"]
    assert _ids(index.find(["E-MSAW alerts", "E‐MSAW"])) == ["pcg-e-msaw"]
    assert _ids(index.find(["the AIP lists"])) == ["pcg-aip-icao"]
    assert index.find(["saw", "e-msaw", "aip"]) == set()


def test_bracket_qualified_entry_owns_its_alias_outright():
    # ADS [ICAO] (gated as an acronym) and the parenthetical of AUTOMATIC
    # DEPENDENT SURVEILLANCE (ADS) both claim "ADS"; the entry whose
    # normalized text is the alias wins instead of the alias being dropped.
    gate = glossary.Gate(allow_acronyms={"ADS [ICAO]": "capitals only"})
    index = glossary.GlossaryIndex.build(PCG, gate)
    assert _ids(index.find(["ADS contracts"])) == ["pcg-ads-[icao]"]
    assert _ids(index.find(["automatic dependent surveillance"])) == [
        "pcg-automatic-dependent-surveillance-(ads)"
    ]
