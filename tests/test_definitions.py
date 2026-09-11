"""FAR → 14 CFR Part 1 defined-term recognition (``far_aim.links.definitions``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from far_aim.generate import BuildError
from far_aim.links import definitions as defs


def _def(term: str, text: str = "means something.", children: list[dict] | None = None) -> dict:
    return {"type": "definition", "term": term, "text": text, "children": children or []}


def _para(label: str, text: str, children: list[dict] | None = None) -> dict:
    return {
        "type": "paragraph",
        "label": label,
        "designator": label.strip("()"),
        "subject": None,
        "text": text,
        "children": children or [],
    }


def _section(
    section: str,
    content: list[dict],
    chapter: str = "I",
    *,
    heading: str = "Definitions.",
    subpart: str | None = None,
    subchapter: str | None = None,
) -> dict:
    return {
        "id": f"cfr-14-{section}",
        "document_type": "cfr_section",
        "section": section,
        "part": section.split(".")[0],
        "chapter": chapter,
        "subchapter": subchapter,
        "subpart": subpart,
        "heading": heading,
        "content": content,
    }


def _lead(text: str) -> dict:
    return {"type": "text", "style": "plain", "text": text}


def _index(
    *sections: dict, deny: dict[tuple[str, str | None], str] | None = None
) -> defs.DefinitionIndex:
    # Chapter-wide sources unless the section says otherwise (Part 1 style).
    sources = [defs.Source(sec, defs.SCOPE_CHAPTER) for sec in sections]
    return defs.DefinitionIndex.build(sources, defs.DefinitionsGate(deny or {}))


def _labels(index: defs.DefinitionIndex, *texts: str) -> list[str]:
    return sorted(index.targets[key].label for key in index.find(texts))


# ---------------------------------------------------------------------------
# Labels and block ids
# ---------------------------------------------------------------------------


def test_definition_label_strips_trailing_punctuation_only():
    assert defs.definition_label("Approved,") == "Approved"
    assert defs.definition_label("Type:") == "Type"
    assert defs.definition_label("Fireproof—") == "Fireproof"
    assert defs.definition_label("Alert Area.") == "Alert Area"
    label = "Air Traffic Service (ATS) route"
    assert defs.definition_label(label) == label
    assert defs.definition_label("Rated 2 1/2-minute OEI power,") == "Rated 2 1/2-minute OEI power"


def test_block_ids_are_slugs_unique_within_the_document():
    content = [
        _def("Night"),
        _def("Air Traffic Service (ATS) route"),
        _def("VS0"),
        _def("Rated 2 1/2-minute OEI power,"),
    ]
    ids = defs.block_ids(content)
    assert [ids[id(block)] for block in content] == [
        "def-night",
        "def-air-traffic-service-ats-route",
        "def-vs0",
        "def-rated-2-1-2-minute-oei-power",
    ]


def test_duplicate_terms_take_their_leading_words():
    # § 1.1 lists "Synthetic vision" twice: the eCFR italicizes only the head
    # of "Synthetic vision system", so the second text opens "system means".
    content = [
        _def("Synthetic vision", "means a computer-generated image."),
        _def("Synthetic vision", "system means an electronic means to display it."),
    ]
    assert defs.definition_labels(content) == ["Synthetic vision", "Synthetic vision system"]
    ids = defs.block_ids(content)
    assert [ids[id(b)] for b in content] == ["def-synthetic-vision", "def-synthetic-vision-system"]
    index = _index(_section("1.1", content))
    assert _labels(index, "a synthetic vision system shows") == ["Synthetic vision system"]
    assert _labels(index, "synthetic vision") == ["Synthetic vision"]


def test_unresolved_duplicates_are_numbered_and_never_linked():
    content = [_def("Foo", "means one."), _def("Foo", "means two.")]
    ids = defs.block_ids(content)
    assert [ids[id(b)] for b in content] == ["def-foo", "def-foo-2"]
    index = _index(_section("1.1", content))
    assert len(index.targets) == 2
    assert index.find(["a foo"]) == set()


def test_block_ids_walk_nested_definitions():
    inner = _def("Cross-country time")
    content = [_para("(b)", "For the purpose of this part:", [inner]), _def("Solo")]
    ids = defs.block_ids(content)
    assert ids[id(inner)] == "def-cross-country-time"
    assert ids[id(content[1])] == "def-solo"
    assert list(defs.iter_definitions(content)) == [inner, content[1]]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_phrases_match_case_insensitively_singular_or_plural():
    index = _index(_section("1.1", [_def("Airport"), _def("Category"), _def("Class:")]))
    assert _labels(index, "at AIRPORTS and an airport") == ["Airport"]
    assert _labels(index, "the categories and classes") == ["Category", "Class"]
    assert _labels(index, "airportsx") == []


def test_abbreviations_match_in_capitals_at_three_characters():
    index = _index(_section("1.2", [_def("IFR"), _def("DH"), _def("TCAS"), _def("TCAS II")]))
    assert _labels(index, "under IFR") == ["IFR"]
    assert _labels(index, "under ifr") == []
    assert _labels(index, "the DH") == []  # too short to be safe
    assert _labels(index, "a TCAS II unit") == ["TCAS II"]  # longest alias wins
    assert _labels(index, "a TCAS unit") == ["TCAS"]


def test_parenthetical_labels_yield_both_forms():
    index = _index(
        _section(
            "1.1",
            [
                _def("Decision altitude (DA)"),
                _def("Air Traffic Service (ATS) route"),
                _def("Small unmanned aircraft system (small UAS)"),
            ],
        ),
        _section("1.2", [_def("NDB (ADF)")]),
    )
    assert _labels(index, "the decision altitude") == ["Decision altitude (DA)"]
    assert _labels(index, "the DA") == []  # a two-letter abbreviation never links
    assert _labels(index, "an ATS route") == ["Air Traffic Service (ATS) route"]
    assert _labels(index, "Air Traffic Service routes") == ["Air Traffic Service (ATS) route"]
    assert _labels(index, "a small UAS") == ["Small unmanned aircraft system (small UAS)"]
    assert _labels(index, "NDB or ADF") == ["NDB (ADF)"]


def test_shared_alias_goes_to_the_label_that_is_the_alias():
    index = _index(
        _section("1.1", [_def("Area navigation (RNAV)")]),
        _section("1.2", [_def("RNAV")]),
    )
    assert index.find(["RNAV"]) == {"1.2#^def-rnav"}
    assert index.find(["area navigation"]) == {"1.1#^def-area-navigation-rnav"}


def test_shared_alias_at_equal_rank_is_dropped():
    index = _index(_section("1.1", [_def("Foo (BAR)")]), _section("1.2", [_def("Baz (BAR)")]))
    assert index.find(["BAR"]) == set()
    assert _labels(index, "foo and baz") == ["Baz (BAR)", "Foo (BAR)"]


def test_longest_alias_wins_across_sections():
    index = _index(
        _section("1.1", [_def("IFR conditions")]),
        _section("1.2", [_def("IFR")]),
    )
    assert _labels(index, "in IFR conditions") == ["IFR conditions"]
    assert _labels(index, "under IFR only") == ["IFR"]


def test_typography_folds_on_both_sides():
    index = _index(_section("1.1", [_def("Over‐the‐top")]))
    assert _labels(index, "VFR over-the-top flight") == ["Over-the-top"]  # label folded too


# ---------------------------------------------------------------------------
# Scope and gate
# ---------------------------------------------------------------------------


def test_lead_in_is_the_text_introducing_the_first_definition():
    nested = _para("(b)", "For the purpose of this part:", [_def("Solo")])
    content = [_para("(a)", "This part prescribes stuff for this section.", []), nested]
    assert defs.lead_in_text(content) == "For the purpose of this part:"
    assert defs.lead_in_text([_lead("In this chapter:"), _def("AGL")]) == "In this chapter:"
    assert defs.lead_in_text([_def("AGL")]) == ""
    extract = {"type": "extract", "blocks": [_def("Deep")]}
    assert defs.lead_in_text([_lead("Says this part"), extract]) == ""


def test_scope_comes_from_the_sections_own_words_narrowest_first():
    def level(lead: str, **kw) -> int | None:
        return defs.scope_level(_section("61.1", [_lead(lead), _def("X")], **kw))

    assert level("As used in this chapter, unless the context requires otherwise:") == (
        defs.SCOPE_CHAPTER
    )
    assert level("For the purpose of this subchapter, the term—") == defs.SCOPE_SUBCHAPTER
    assert level("For the purpose of this part:") == defs.SCOPE_PART
    assert level("For the purposes of §§ 91.851 through 91.877 of this subpart:") == (
        defs.SCOPE_SUBPART
    )
    # A citation's "of this chapter" never widens a part-scoped section.
    assert level("In addition to § 1.1 of this chapter, these apply to this part:") == (
        defs.SCOPE_PART
    )
    # Terms for the section alone: nothing to link elsewhere.
    assert level("For the purposes of this section:") is None
    assert level("Any term used in this part and, except in this section, …") is None


def test_scope_without_a_phrase_falls_back_on_heading_and_subpart():
    assert defs.scope_level(_section("5.3", [_def("X")], subpart="A")) == defs.SCOPE_PART
    assert defs.scope_level(_section("5.3", [_def("X")], subpart=None)) == defs.SCOPE_PART
    assert defs.scope_level(_section("26.41", [_def("X")], subpart="E")) == defs.SCOPE_SUBPART
    plain = _section("91.9", [_def("X")], heading="Civil aircraft flight manual.")
    assert defs.scope_level(plain) is None
    with_phrase = _section("97.3", [_lead("As used in this part—"), _def("X")], heading="Terms.")
    assert defs.scope_level(with_phrase) == defs.SCOPE_PART


def test_source_covers_documents_in_its_scope_only():
    chapter = defs.Source(_section("1.1", [], subchapter="A"), defs.SCOPE_CHAPTER)
    subchapter = defs.Source(_section("110.2", [], subchapter="G"), defs.SCOPE_SUBCHAPTER)
    part = defs.Source(_section("61.1", [], subpart="A"), defs.SCOPE_PART)
    subpart = defs.Source(_section("91.851", [], subpart="I"), defs.SCOPE_SUBPART)
    in_61 = ("I", "D", "61", "B")
    in_91_i = ("I", "F", "91", "I")
    in_121 = ("I", "G", "121", "A")
    in_401 = ("III", "A", "401", None)
    assert [chapter.covers(x) for x in (in_61, in_91_i, in_121, in_401)] == [
        True, True, True, False
    ]
    assert [subchapter.covers(x) for x in (in_61, in_121)] == [False, True]
    assert [part.covers(x) for x in (in_61, in_91_i)] == [True, False]
    assert [subpart.covers(x) for x in (in_91_i, ("I", "F", "91", "B"))] == [True, False]


def test_document_scope_gives_an_appendix_its_parts_chapter():
    part_doc = {"chapter": "I", "subchapter": "F"}
    apx = {"document_type": "cfr_appendix", "part": "91", "chapter": None, "subchapter": None}
    assert defs.document_scope(apx, part_doc) == ("I", "F", "91", None)
    sec = _section("91.155", [], subchapter="F", subpart="B")
    assert defs.document_scope(sec, part_doc) == ("I", "F", "91", "B")


def _part(part: str, *sections: dict, chapter: str = "I") -> dict:
    return {"part": part, "chapter": chapter, "children": list(sections)}


def test_discover_sources_walks_subparts_and_skips_self_scoped_sections():
    one = _part(
        "1",
        {
            "type": "subpart",
            "children": [
                {
                    "type": "subject_group",
                    "sections": [_section("1.2", [_lead("In this chapter:"), _def("AGL")])],
                }
            ],
        },
        _section("1.1", [_lead("As used in this chapter:"), _def("Night")]),
    )
    ninety_one = _part(
        "91",
        _section("91.227", [_lead("For the purposes of this section:"), _def("ADS-B Out")]),
        _section(
            "91.851", [_lead("For the purposes of this subpart:"), _def("Stage 2")], subpart="I"
        ),
        _section("91.155", [_para("(a)", "text")], heading="Basic VFR weather minimums."),
    )
    found = defs.discover_sources({"91": ninety_one, "1": one})
    assert [(s.number, s.level) for s in found] == [
        ("1.2", defs.SCOPE_CHAPTER),
        ("1.1", defs.SCOPE_CHAPTER),
        ("91.851", defs.SCOPE_SUBPART),
    ]
    assert defs.discover_sources({"91": _part("91", ninety_one["children"][2])}) == []


def test_links_pick_the_sources_covering_each_document_and_never_itself():
    part_doc = _part("139", chapter="I")
    p1 = _section("1.1", [_lead("As used in this chapter:"), _def("Airport"), _def("Night")])
    p139 = _section("139.5", [_lead("As used in this part:"), _def("Airport")], subpart="A")
    links = defs.DefinitionLinks([
        defs.Source(p1, defs.SCOPE_CHAPTER), defs.Source(p139, defs.SCOPE_PART)
    ], defs.DefinitionsGate())
    in_139 = _section("139.7", [], subpart="A")
    index = links.index_for(in_139, part_doc)
    assert index is not None
    # The part's own definition overrides § 1.1's; § 1.1's other terms still apply.
    assert index.find(["the airport at night"]) == {"139.5#^def-airport", "1.1#^def-night"}
    in_91 = _section("91.155", [])
    assert links.index_for(in_91, _part("91")).find(["the airport"]) == {"1.1#^def-airport"}
    # A source lists other sources' terms but never its own.
    own = links.index_for(p139, part_doc)
    assert own is not None and own.find(["airport"]) == {"1.1#^def-airport"}
    assert links.index_for(p1, _part("1")) is None  # nothing else covers Part 1
    other_chapter = _section("401.5", [], chapter="III")
    assert links.index_for(other_chapter, _part("401", chapter="III")) is None


def test_narrower_scope_wins_over_ownership_rank():
    wide = _section("1.1", [_lead("As used in this chapter:"), _def("RNAV")])
    narrow = _section("97.3", [_lead("As used in this part—"), _def("Area navigation (RNAV)")])
    index = defs.DefinitionIndex.build(
        [defs.Source(wide, defs.SCOPE_CHAPTER), defs.Source(narrow, defs.SCOPE_PART)],
        defs.DefinitionsGate(),
    )
    assert index.find(["RNAV"]) == {"97.3#^def-area-navigation-rnav"}


def test_deny_blocks_every_alias_of_the_term():
    section = _section("1.1", [_def("Instrument"), _def("Instrument approach procedure (IAP)")])
    assert _labels(_index(section), "an instrument") == ["Instrument"]
    index = _index(section, deny={("Instrument", None): "ambiguous"})
    assert _labels(index, "an instrument") == []
    assert _labels(index, "an instrument approach procedure or IAP") == [
        "Instrument approach procedure (IAP)"
    ]
    assert "1.1#^def-instrument" in index.targets  # still a block, just never linked


def test_gate_entries_may_target_one_source_section():
    p1 = _section("1.1", [_def("Airport")])
    p139 = _section("139.5", [_def("Airport")])
    sources = [defs.Source(p1, defs.SCOPE_CHAPTER), defs.Source(p139, defs.SCOPE_PART)]
    index = defs.DefinitionIndex.build(
        sources, defs.DefinitionsGate({("Airport", "139.5"): "only there"})
    )
    assert index.find(["airport"]) == {"1.1#^def-airport"}
    index = defs.DefinitionIndex.build(sources, defs.DefinitionsGate({("Airport", None): "all"}))
    assert index.find(["airport"]) == set()


def test_gate_must_name_a_defined_term():
    source = defs.Source(_section("1.1", [_def("Night")]), defs.SCOPE_CHAPTER)
    with pytest.raises(BuildError, match="unknown term 'Nope'"):
        defs.DefinitionLinks([source], defs.DefinitionsGate({("Nope", None): "typo"}))
    with pytest.raises(BuildError, match="'Night' that § 61.1 does not define"):
        defs.DefinitionLinks([source], defs.DefinitionsGate({("Night", "61.1"): "typo"}))
    defs.DefinitionLinks([source], defs.DefinitionsGate({("Night", "1.1"): "fine"}))


def test_gate_schema_fails_closed(tmp_path):
    path = tmp_path / defs.GATE_FILENAME
    with pytest.raises(BuildError, match="missing"):
        defs.DefinitionsGate.load(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BuildError, match="cannot read"):
        defs.DefinitionsGate.load(path)
    for bad, message in (
        ("[]", "top level"),
        ('{"deny": [], "allow": []}', "unknown section"),
        ("{}", "missing section deny"),
        ('{"deny": {}}', "must be a list"),
        ('{"deny": [{"term": "Night"}]}', "need non-empty"),
        ('{"deny": [{"term": "", "reason": "x"}]}', "need non-empty"),
        ('{"deny": [{"term": "Night", "reason": "x", "section": 1}]}', "need non-empty"),
        ('{"deny": [{"term": "Night", "reason": "x", "extra": 1}]}', "need non-empty"),
        (
            '{"deny": [{"term": "Night", "reason": "a"}, {"term": "Night", "reason": "b"}]}',
            "duplicate",
        ),
    ):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(BuildError, match=message):
            defs.DefinitionsGate.load(path)
    good = (
        '{"_comment": "c", "deny": [{"term": "Night", "reason": "r"},'
        ' {"term": "Airport", "reason": "s", "section": "139.5"}]}'
    )
    path.write_text(good, encoding="utf-8")
    gate = defs.DefinitionsGate.load(path)
    assert gate.deny == {("Night", None): "r", ("Airport", "139.5"): "s"}
    assert gate.denies("Night", "1.1") and gate.denies("Airport", "139.5")
    assert not gate.denies("Airport", "1.1")


def test_committed_gate_matches_accepted_layer():
    # The real gate file must name only terms the parsed sources define; the
    # normalized layer is a local cache, so skip when it is absent.
    normalized = Path("data/normalized/ecfr")
    if not normalized.is_dir():
        pytest.skip("normalized eCFR layer not present")
    docs = {}
    for path in normalized.glob("part-*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        docs[doc["part"]] = doc
    gate = defs.DefinitionsGate.load(Path("data/links") / defs.GATE_FILENAME)
    links = defs.DefinitionLinks.build(docs, gate)  # raises on a stale entry
    assert links is not None
    by_number = {s.number: s for s in links.sources}
    assert by_number["1.1"].level == defs.SCOPE_CHAPTER
    assert by_number["61.1"].level == defs.SCOPE_PART
    assert by_number["91.851"].level == defs.SCOPE_SUBPART
    assert by_number["110.2"].level == defs.SCOPE_SUBCHAPTER
    assert "91.227" not in by_number  # "for the purposes of this section"
    sec_91_155 = next(
        d for d in _walk(docs["91"]) if d.get("section") == "91.155"
    )
    index = links.index_for(sec_91_155, docs["91"])
    assert index is not None
    assert index.find(["at night under IFR"]) == {"1.1#^def-night", "1.2#^def-ifr"}
    assert index.find(["a type certificate"]) == set()  # denied
    sec_139 = next(d for d in _walk(docs["139"]) if d.get("section") == "139.101")
    assert links.index_for(sec_139, docs["139"]).find(["the airport"]) == {"139.5#^def-airport"}


def _walk(node: dict):
    yield node
    for child in node.get("children") or node.get("sections") or []:
        yield from _walk(child)
