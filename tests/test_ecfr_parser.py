"""Phase 2 tests: eCFR XML → canonical JSON.

The fixture ``part-91-slice.xml`` is verbatim official text extracted from
the accepted 2026-08-19 snapshot (whole elements removed to keep it small;
retained wording untouched). Exact-text assertions below are transcribed
from the same snapshot — they guard against silent wording drift
(plan §17.3, §32.3).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from far_aim.models import cfr as model
from far_aim.parsers import ecfr as parser

FIXTURES = Path(__file__).parent / "fixtures" / "ecfr"
SLICE_PATH = FIXTURES / "part-91-slice.xml"
PART_234_PATH = FIXTURES / "part-234-slice.xml"

SOURCE = {
    "provider": "ecfr",
    "source_version": "2026-08-19",
    "url": "https://www.ecfr.gov/api/versioner/v1/full/2026-08-19/title-14.xml",
    "retrieved_at": "2026-08-20T10:05:13Z",
    "raw_checksum": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
}


@pytest.fixture(scope="module")
def part91() -> dict:
    root = ET.parse(SLICE_PATH).getroot()
    return parser.build_part_docs(root, SOURCE, {"91"})["91"]


def _walk_sections(node: object):
    if isinstance(node, dict):
        if node.get("document_type") == model.DOCUMENT_TYPE_SECTION:
            yield node
        for value in node.values():
            if isinstance(value, list):
                for item in value:
                    yield from _walk_sections(item)


def _section(doc: dict, number: str) -> dict:
    return next(s for s in _walk_sections(doc) if s["section"] == number)


def _paragraph(blocks: list[dict], *labels: str) -> dict:
    node = None
    for label in labels:
        pool = node["children"] if node is not None else blocks
        node = next(b for b in pool if b.get("type") == "paragraph" and b["label"] == label)
    return node


# ---------------------------------------------------------------------------
# Hierarchy and identity
# ---------------------------------------------------------------------------


def test_part_identity_and_hierarchy(part91):
    assert part91["id"] == "cfr-14-part-91"
    assert part91["document_type"] == "cfr_part"
    assert part91["part"] == "91"
    assert part91["chapter"] == "I"
    assert part91["subchapter"] == "F"
    assert part91["heading"] == "GENERAL OPERATING AND FLIGHT RULES"
    assert part91["authority"]["heading"] == "Authority:"
    assert "49 U.S.C." in part91["authority"]["text"]


def test_section_context_fields(part91):
    s = _section(part91, "91.155")
    assert s["id"] == "cfr-14-91.155"
    assert s["part"] == "91"
    assert s["subpart"] == "B"
    assert s["subject_group"] == "Visual Flight Rules"
    assert s["heading"] == "Basic VFR weather minimums."
    s = _section(part91, "91.3")
    assert s["subpart"] == "A"
    assert s["subject_group"] is None


def test_reserved_section_range(part91):
    s = _section(part91, "91.27-91.99")
    assert s["id"] == "cfr-14-91.27-91.99"
    assert s["reserved"] is True
    assert s["heading"] == "[Reserved]"
    assert s["content"] == []


def test_subpart_source_notes(part91):
    subparts = {c["label"]: c for c in part91["children"] if c.get("type") == "subpart"}
    assert subparts["A"]["source_note"]["heading"] == "Source:"
    # Subpart J carries no SOURCE element in the eCFR XML.
    assert subparts["J"]["source_note"] is None


# ---------------------------------------------------------------------------
# Exact official text (transcribed from the 2026-08-19 snapshot)
# ---------------------------------------------------------------------------


def test_exact_text_91_155_a(part91):
    a = _paragraph(_section(part91, "91.155")["content"], "(a)")
    assert a["text"] == (
        "Except as provided in paragraph (b) of this section and § 91.157, no person "
        "may operate an aircraft under VFR when the flight visibility is less, or at a "
        "distance from clouds that is less, than that prescribed for the corresponding "
        "altitude and class of airspace in the following table:"
    )


def test_exact_text_91_3_a(part91):
    a = _paragraph(_section(part91, "91.3")["content"], "(a)")
    assert a["text"] == (
        "The pilot in command of an aircraft is directly responsible for, and is the "
        "final authority as to, the operation of that aircraft."
    )


def test_fraction_flattened_with_wording_intact(part91):
    # 91.155(b)(1) uses <FR>1/2</FR>; the characters must survive flattening.
    b1 = _paragraph(_section(part91, "91.155")["content"], "(b)", "(1)")
    assert b1["subject"] == "Helicopter."
    assert "within 1/2 mile of the runway" in b1["text"]


# ---------------------------------------------------------------------------
# Paragraph nesting
# ---------------------------------------------------------------------------


def test_91_175_top_level_sequence(part91):
    content = _section(part91, "91.175")["content"]
    top = [b["label"] for b in content if b.get("type") == "paragraph"]
    assert top == ["(a)", "(b)", "(c)", "(d)", "(e)", "(f)", "(g)", "(h)", "(i)", "(j)", "(k)"]


def test_91_175_alpha_i_not_roman(part91):
    """After (h)(2), '(i) Operations on unpublished routes…' is top-level alpha."""
    i = _paragraph(_section(part91, "91.175")["content"], "(i)")
    assert i["subject"].startswith("Operations on unpublished routes")


def test_91_175_roman_children(part91):
    c3 = _paragraph(_section(part91, "91.175")["content"], "(c)", "(3)")
    romans = [b["label"] for b in c3["children"] if b.get("type") == "paragraph"]
    assert romans == [
        "(i)", "(ii)", "(iii)", "(iv)", "(v)", "(vi)", "(vii)", "(viii)", "(ix)", "(x)"
    ]


def test_91_175_run_in_subject_and_child(part91):
    """'(h) SUBJECT. (1) text' yields (h) with subject and (1) as its child."""
    h = _paragraph(_section(part91, "91.175")["content"], "(h)")
    assert h["subject"] == "Comparable values of RVR and ground visibility."
    assert h["text"] == ""
    h1 = _paragraph([h], "(h)", "(1)")
    assert h1["text"].startswith("Except for Category II or Category III minimums")


def test_table_attached_under_empty_paragraph(part91):
    """91.175(h)(2) is a bare label whose content is the RVR conversion table."""
    h2 = _paragraph(_section(part91, "91.175")["content"], "(h)", "(2)")
    assert h2["text"] == ""
    kinds = [b["type"] for b in h2["children"]]
    assert kinds == ["table"]
    table = h2["children"][0]
    assert table["header_rows"][0][0]["text"] == "RVR (feet)"
    assert table["rows"][0][0]["text"] == "1,600"


def test_91_155_table_structure(part91):
    a = _paragraph(_section(part91, "91.155")["content"], "(a)")
    table = next(b for b in a["children"] if b["type"] == "table")
    header = [cell["text"] for cell in table["header_rows"][0]]
    assert header == ["Airspace", "Flight visibility", "Distance from clouds"]
    assert [
        cell["text"] for cell in table["rows"][0]
    ] == ["Class A", "Not Applicable", "Not Applicable."]


def test_italic_paragraph_levels(part91):
    """91.107(a)(3)(iii)(B) nests italic levels: (1)…(4) then italic romans."""
    b = _paragraph(
        _section(part91, "91.107")["content"], "(a)", "(3)", "(iii)", "(B)"
    )
    assert [c["label"] for c in b["children"] if c["type"] == "paragraph"] == [
        "(1)", "(2)", "(3)", "(4)"
    ]
    b2 = _paragraph([b], "(B)", "(2)")
    assert [c["label"] for c in b2["children"]] == ["(i)", "(ii)"]


def test_gap_tolerance_in_real_text(part91):
    """91.107(a)(3)(iii)(B)(3)'s children start at italic (ii) — the (i) was
    removed by amendment without renumbering. The parser must place them
    rather than silently drop or crash (plan §32.2)."""
    b3 = _paragraph(
        _section(part91, "91.107")["content"], "(a)", "(3)", "(iii)", "(B)", "(3)"
    )
    assert [c["label"] for c in b3["children"]] == ["(ii)", "(iii)", "(iv)"]


# ---------------------------------------------------------------------------
# Section metadata fields
# ---------------------------------------------------------------------------


def test_citations_and_approvals(part91):
    s = _section(part91, "91.155")
    assert len(s["citations"]) == 1
    assert s["citations"][0].startswith("[Docket 24458, 56 FR 65660")
    s = _section(part91, "91.3")
    assert s["approvals"] == [
        "(Approved by the Office of Management and Budget under control number 2120-0005)"
    ]


def test_amendment_note_captured(part91):
    s = _section(part91, "91.220")
    assert len(s["amendment_notes"]) == 1
    assert "Link to an amendment" in s["amendment_notes"][0]


def test_extract_block(part91):
    s = _section(part91, "91.905")
    extract = next(b for b in s["content"] if b.get("type") == "extract")
    styles = {b["style"] for b in extract["blocks"]}
    assert "flush-2" in styles
    assert any(b["text"] == "91.107 Use of safety belts." for b in extract["blocks"])


# ---------------------------------------------------------------------------
# Appendices and SFARs
# ---------------------------------------------------------------------------


def _appendix(doc: dict, id_suffix: str) -> dict:
    return next(
        c
        for c in doc["children"]
        if c.get("document_type") == model.DOCUMENT_TYPE_APPENDIX
        and c["id"].endswith(id_suffix)
    )


def test_sfar_appendices(part91):
    sfar = _appendix(part91, "sfar-50-2")
    assert sfar["heading"].startswith("Special Federal Aviation Regulation No. 50-2")
    assert any(b["type"] == "note" for b in sfar["content"])
    sfar60 = _appendix(part91, "sfar-60")
    assert sfar60["section_authority"] is not None


def test_reserved_appendix_range(part91):
    appendix = _appendix(part91, "appendixes-B-C")
    assert appendix["reserved"] is True
    assert appendix["content"] == []


def test_appendix_editorial_notes(part91):
    appendix = _appendix(part91, "appendix-D")
    headings = {n["heading"] for n in appendix["editorial_notes"]}
    assert headings == {"Editorial Note:", "Effective Date Note:"}


# ---------------------------------------------------------------------------
# Determinism and canonical hashing
# ---------------------------------------------------------------------------


def test_parse_is_deterministic():
    def build() -> str:
        root = ET.parse(SLICE_PATH).getroot()
        doc = parser.build_part_docs(root, SOURCE, {"91"})["91"]
        return json.dumps(doc, sort_keys=True, ensure_ascii=False)

    assert build() == build()


def test_canonical_hash_excludes_provenance(part91):
    other_source = {**SOURCE, "retrieved_at": "2027-01-01T00:00:00Z", "url": "elsewhere"}
    root = ET.parse(SLICE_PATH).getroot()
    other = parser.build_part_docs(root, other_source, {"91"})["91"]
    assert other["canonical_hash"] == part91["canonical_hash"]
    assert (
        _section(other, "91.155")["canonical_hash"]
        == _section(part91, "91.155")["canonical_hash"]
    )
    # and the stored hash is reproducible from the document itself
    assert model.canonical_hash(part91) == part91["canonical_hash"]


# ---------------------------------------------------------------------------
# Fail-loud behavior
# ---------------------------------------------------------------------------

_MINI_TEMPLATE = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
<DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
<DIV5 N="9" TYPE="PART"><HEAD>PART 9—TEST PART</HEAD>
<DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   Test section.</HEAD>
{body}
</DIV8></DIV5></DIV3></DIV1></ECFR>"""


def _build_mini(body: str) -> dict:
    root = ET.fromstring(_MINI_TEMPLATE.format(body=body))
    return parser.build_part_docs(root, SOURCE)["9"]


def test_unknown_tag_fails_loudly():
    with pytest.raises(parser.ParseError, match="unhandled element 'MYSTERY'"):
        _build_mini("<MYSTERY>text</MYSTERY>")


def test_image_in_table_cell_fails_loudly():
    """Inline images are handled in paragraph flow (test_inline_image_becomes_block)
    but remain unmodeled inside table cells — those must fail, not vanish."""
    with pytest.raises(parser.ParseError, match="non-text element 'img'"):
        _build_mini(
            '<TABLE><TR><TD>cell <img src="x.gif"/></TD></TR></TABLE>'
        )


def test_impossible_label_fails_loudly():
    with pytest.raises(parser.ParseError, match="cannot place paragraph label"):
        # A section cannot open at the fourth level: no reading admits (A).
        _build_mini("<P>(A) Orphan paragraph.</P>")


def test_lossless_check_catches_dropped_words():
    root = ET.fromstring(_MINI_TEMPLATE.format(body="<P>(a) Some regulatory text.</P>"))
    context, div5 = parser.iter_parts(root)[0]
    doc = parser.parse_part(div5, context, SOURCE)
    doc["children"][0]["content"][0]["text"] = "Some text."  # drop "regulatory"
    with pytest.raises(parser.ParseError, match="lossless-capture check failed"):
        parser._verify_lossless(div5, doc)


def test_surplus_hierarchy_head_fails_loudly():
    """Hierarchy headings sit outside the per-part lossless check; a second
    HEAD in a chapter would be silently skipped by the walk, so it must be
    rejected instead."""
    xml = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <HEAD>CHAPTER I—SECOND HEADING</HEAD>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—TEST PART</HEAD>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   Test section.</HEAD></DIV8>
    </DIV5></DIV3></DIV1></ECFR>"""
    with pytest.raises(parser.ParseError, match="has 2 HEAD elements"):
        parser.build_part_docs(ET.fromstring(xml), SOURCE)


def test_head_at_document_root_fails_loudly():
    """A HEAD directly under the ECFR root has no home in the model and
    must not vanish silently."""
    xml = """<ECFR><HEAD>Stray heading</HEAD>
    <DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—TEST PART</HEAD>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   Test section.</HEAD></DIV8>
    </DIV5></DIV3></DIV1></ECFR>"""
    with pytest.raises(parser.ParseError, match="unexpected HEAD element at the ECFR"):
        parser.build_part_docs(ET.fromstring(xml), SOURCE)


def test_hierarchy_division_without_designator_fails_loudly():
    """Hierarchy designators live in N attributes, outside the lossless
    word comparison; a chapter without one must not silently publish its
    parts with a null hierarchy context."""
    xml = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—TEST PART</HEAD>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   Test section.</HEAD></DIV8>
    </DIV5></DIV3></DIV1></ECFR>"""
    with pytest.raises(parser.ParseError, match="chapter division has no N attribute"):
        parser.build_part_docs(ET.fromstring(xml), SOURCE)


def test_markerless_head_on_ordinary_section_fails_loudly():
    """"9.1 Applicability" without its § marker means the citation markup
    was lost; it must fail, not enter the canonical layer with an empty
    head_marker."""
    root = ET.fromstring(_MINI_TEMPLATE.format(body="").replace("§ 9.1", "9.1"))
    with pytest.raises(parser.ParseError, match="has no citation marker"):
        parser.build_part_docs(root, SOURCE)


def test_markerless_reserved_and_cab_heads_still_parse():
    """The corpus's only markerless heads: reserved ranges ("13.83-13.87
    [Reserved]") and CAB-era locally numbered sections ("19-8.1 Purpose.",
    hyphenated prefix). Both stay accepted."""
    xml = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <DIV5 N="13" TYPE="PART"><HEAD>PART 13—FIRST</HEAD>
    <DIV8 N="13.83-13.87" TYPE="SECTION"><HEAD>13.83-13.87   [Reserved]</HEAD></DIV8>
    </DIV5>
    <DIV5 N="241" TYPE="PART"><HEAD>PART 241—SECOND</HEAD>
    <DIV8 N="19-8.1" TYPE="SECTION"><HEAD>19-8.1   Purpose.</HEAD>
    <P>(a) Some text.</P></DIV8>
    </DIV5></DIV3></DIV1></ECFR>"""
    docs = parser.build_part_docs(ET.fromstring(xml), SOURCE)
    reserved = next(_walk_sections(docs["13"]))
    assert (reserved["head_marker"], reserved["reserved"]) == ("", True)
    cab = next(_walk_sections(docs["241"]))
    assert (cab["head_marker"], cab["section"]) == ("", "19-8.1")


def test_head_and_attribute_mismatch_fails():
    root = ET.fromstring(
        _MINI_TEMPLATE.format(body="").replace('N="9.1"', 'N="9.2"')
    )
    with pytest.raises(parser.ParseError, match="does not cite N attribute '9.2'"):
        parser.build_part_docs(root, SOURCE)


def test_unknown_part_requested():
    root = ET.parse(SLICE_PATH).getroot()
    with pytest.raises(parser.ParseError, match="not present in this title"):
        parser.build_part_docs(root, SOURCE, {"91", "137"})


# ---------------------------------------------------------------------------
# Ambiguity resolution unit cases
# ---------------------------------------------------------------------------


def test_gap_tolerance_missing_first_roman():
    """A child list starting at (ii) — the (i) removed by amendment — parses."""
    doc = _build_mini(
        "<P>(a) Intro:</P><P>(1) First:</P>"
        "<P>(ii) Starts at two;</P><P>(iii) And three.</P>"
    )
    a1 = doc["children"][0]["content"][0]["children"][0]
    assert [c["label"] for c in a1["children"]] == ["(ii)", "(iii)"]


def test_lookahead_prefers_alpha_after_h():
    body = (
        "<P>(g) Text g.</P><P>(h) Text h:</P><P>(1) Sub one.</P><P>(2) Sub two.</P>"
        "<P>(i) Text i.</P><P>(j) Text j.</P>"
    )
    doc = _build_mini(body)
    top = [b["label"] for b in doc["children"][0]["content"] if b["type"] == "paragraph"]
    assert top == ["(g)", "(h)", "(i)", "(j)"]


def test_lookahead_prefers_roman_when_sequence_continues():
    body = (
        "<P>(g) Text g.</P><P>(h) Text h:</P><P>(1) Sub one.</P><P>(2) Sub two:</P>"
        "<P>(i) Roman one;</P><P>(ii) Roman two.</P>"
    )
    doc = _build_mini(body)
    top = [b["label"] for b in doc["children"][0]["content"] if b["type"] == "paragraph"]
    assert top == ["(g)", "(h)"]
    h2 = _paragraph(doc["children"][0]["content"], "(h)", "(2)")
    assert [c["label"] for c in h2["children"]] == ["(i)", "(ii)"]


# ---------------------------------------------------------------------------
# Integration against the real accepted snapshot (local raw cache only)
# ---------------------------------------------------------------------------

_RAW_SNAPSHOT = (
    Path(__file__).parent.parent / "data" / "raw" / "ecfr" / "2026-08-19" / "title-14.xml"
)


@pytest.mark.skipif(not _RAW_SNAPSHOT.exists(), reason="raw snapshot cache not present")
def test_full_part_91_from_accepted_snapshot():
    root = ET.parse(_RAW_SNAPSHOT).getroot()
    doc = parser.build_part_docs(root, SOURCE, {"91"})["91"]
    assert parser.count_sections(doc) == 287
    section = _section(doc, "91.155")
    assert section["heading"] == "Basic VFR weather minimums."
    # every section belongs to part 91 and has a unique id
    ids = [s["id"] for s in _walk_sections(doc)]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Title-wide structures (definitions, re-entry, run-in subjects, markers)
# ---------------------------------------------------------------------------


def test_definition_paragraphs_restart_lists():
    """Defined terms open their own sub-lists whose numbering restarts (1.1)."""
    doc = _build_mini(
        "<P>As used in this part:</P>"
        "<P><I>Alpha term</I> means the first thing:</P>"
        "<P>(1) One; and</P><P>(2) Two.</P>"
        "<P><I>Beta term</I> means the second thing:</P>"
        "<P>(1) One again; and</P><P>(2) Two again.</P>"
    )
    content = doc["children"][0]["content"]
    definitions = [b for b in content if b["type"] == "definition"]
    assert [d["term"] for d in definitions] == ["Alpha term", "Beta term"]
    for definition in definitions:
        assert [c["label"] for c in definition["children"]] == ["(1)", "(2)"]


def test_definition_term_absorbs_subscript():
    """§1.2's V-speeds are an italic V plus subscript: the term is 'V2min'."""
    doc = _build_mini("<P><I>V</I><E T=\"52\">2min</E> means minimum takeoff safety speed.</P>")
    definition = doc["children"][0]["content"][0]
    assert definition["type"] == "definition"
    assert definition["term"] == "V2min"
    assert definition["text"] == "means minimum takeoff safety speed."


def test_label_reentry_keeps_full_label():
    """150.21-style '(f)(1)' then '(f)(2)': no duplicate (f) node."""
    doc = _build_mini(
        "<P>(e) Text e.</P><P>(f)(1) First item.</P><P>(f)(2) Second item.</P>"
    )
    content = doc["children"][0]["content"]
    top = [b["label"] for b in content if b["type"] == "paragraph"]
    assert top == ["(e)", "(f)"]
    f = _paragraph(content, "(f)")
    assert [c["label"] for c in f["children"]] == ["(1)", "(f)(2)"]


def test_em_dash_run_in_subjects():
    """DOT style '(b) <I>Subject</I>—(1) <I>Sub.</I> (i) text' chains."""
    doc = _build_mini(
        "<P>(a) Plain text.</P>"
        "<P>(b) <I>Persons to be served</I>—(1) <I>Air carriers.</I> "
        "(i) In certificate proceedings, all persons.</P>"
    )
    b = _paragraph(doc["children"][0]["content"], "(b)")
    assert b["subject"] == "Persons to be served"
    b1 = b["children"][0]
    assert b1["label"] == "(1)"
    assert b1["subject"] == "Air carriers."
    assert b1["children"][0]["label"] == "(i)"


def test_italic_alpha_levels():
    """372.24 nests italic letters under romans: (a)(2)(ii)(a-italic)…"""
    doc = _build_mini(
        "<P>(a) Top:</P><P>(1) One.</P><P>(2) Two:</P>"
        "<P>(i) Roman one;</P><P>(ii) Roman two:</P>"
        "<P>(<I>a</I>) Italic a;</P><P>(<I>b</I>) Italic b.</P>"
    )
    ii = _paragraph(doc["children"][0]["content"], "(a)", "(2)", "(ii)")
    assert [c["label"] for c in ii["children"]] == ["(a)", "(b)"]


def test_mid_sequence_child_after_buried_run_in():
    """61.157-style: '(a) … : (1) …' buried mid-text leaves (i)…(iii) then a
    bare (2) that must nest under (a), not fail."""
    doc = _build_mini(
        "<P>(a) The applicant must: (1) do the following:</P>"
        "<P>(i) One;</P><P>(ii) Two;</P><P>(iii) Three.</P>"
        "<P>(2) Also this.</P><P>(b) Next paragraph.</P>"
    )
    content = doc["children"][0]["content"]
    top = [b["label"] for b in content if b["type"] == "paragraph"]
    assert top == ["(a)", "(b)"]
    a = _paragraph(content, "(a)")
    assert [c["label"] for c in a["children"] if c["type"] == "paragraph"] == [
        "(i)", "(ii)", "(iii)", "(2)"
    ]


def test_inline_image_becomes_block():
    doc = _build_mini(
        '<P>(a) Calculated using the following equation:</P>'
        '<img src="/graphics/er16fe24.041.gif" />'
    )
    a = _paragraph(doc["children"][0]["content"], "(a)")
    assert a["children"] == [{"type": "image", "src": "/graphics/er16fe24.041.gif"}]


def test_footnote_block():
    doc = _build_mini(
        "<P>(a) Subject to the limits.<FTNT><P><SU>1</SU> The footnote "
        "text.</P></FTNT></P>"
    )
    a = _paragraph(doc["children"][0]["content"], "(a)")
    footnote = a["children"][0]
    assert footnote["type"] == "footnote"
    assert footnote["blocks"][0]["text"] == "1 The footnote text."


def test_section_head_marker_variants():
    """Part 241 uses 'Section 03' / 'Sec. 1-1' markers and local numbering."""
    xml = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 N="II" TYPE="CHAPTER"><HEAD>CHAPTER II—TEST</HEAD>
    <DIV5 N="241" TYPE="PART"><HEAD>PART 241—TEST PART</HEAD>
    <DIV8 N="03" TYPE="SECTION"><HEAD>Section 03   Definitions.</HEAD></DIV8>
    <DIV8 N="1-1" TYPE="SECTION"><HEAD>Sec. 1-1   Applicability.</HEAD></DIV8>
    </DIV5></DIV3></DIV1></ECFR>"""
    doc = parser.build_part_docs(ET.fromstring(xml), SOURCE)["241"]
    sections = list(_walk_sections(doc))
    assert [(s["id"], s["head_marker"]) for s in sections] == [
        ("cfr-14-241-03", "Section"),
        ("cfr-14-241-1-1", "Sec."),
    ]


@pytest.mark.skipif(not _RAW_SNAPSHOT.exists(), reason="raw snapshot cache not present")
def test_full_title_parses_from_accepted_snapshot():
    """Phase 2 exit gate: every part of Title 14 parses losslessly."""
    root = ET.parse(_RAW_SNAPSHOT).getroot()
    docs = parser.build_part_docs(root, SOURCE)
    assert len(docs) == 226
    total = sum(parser.count_sections(doc) for doc in docs.values())
    assert total == 6363  # matches the fetch validator's DIV8 SECTION count
    ids = []

    def collect(node):
        if isinstance(node, dict):
            if "id" in node:
                ids.append(node["id"])
            for value in node.values():
                if isinstance(value, list):
                    for item in value:
                        collect(item)

    for doc in docs.values():
        collect(doc)
    assert len(ids) == len(set(ids))


def test_head_citing_longer_number_fails():
    """N="9.1" with HEAD "§ 9.10 …" is a mismatch, not a prefix match."""
    root = ET.fromstring(
        _MINI_TEMPLATE.format(body="").replace(
            "§ 9.1   Test section.", "§ 9.10   Wrong section."
        )
    )
    with pytest.raises(parser.ParseError, match="does not cite N attribute '9.1'"):
        parser.build_part_docs(root, SOURCE)


def test_duplicate_part_numbers_fail_loudly():
    """Two DIV5 elements with the same N must not silently shadow each other."""
    xml = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—FIRST</HEAD>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   One.</HEAD></DIV8></DIV5>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—SECOND</HEAD>
    <DIV8 N="9.2" TYPE="SECTION"><HEAD>§ 9.2   Two.</HEAD></DIV8></DIV5>
    </DIV3></DIV1></ECFR>"""
    with pytest.raises(parser.ParseError, match="duplicate part number"):
        parser.build_part_docs(ET.fromstring(xml), SOURCE)


def test_italic_run_in_heading_is_not_a_definition():
    """§29.755/§19-8.9 style: outside a definitions section, a period-ending
    italic run is a subject heading; following labels stay top-level."""
    doc = _build_mini(
        "<P><I>Sample accuracy and reliability.</I> To maximize accuracy, "
        "each carrier is to:</P>"
        "<P>(a) Develop a written statement;</P>"
        "<P>(b) Submit any proposed changes.</P>"
    )
    content = doc["children"][0]["content"]
    first = content[0]
    assert first["type"] == "paragraph"
    assert first["label"] is None
    assert first["subject"] == "Sample accuracy and reliability."
    assert first["children"] == []
    top = [b["label"] for b in content if b.get("type") == "paragraph"]
    assert top == [None, "(a)", "(b)"]


def test_period_terms_are_definitions_in_definitions_sections():
    """§1.1's "Restricted area." keeps definition semantics in a section
    whose heading names it a definitions section."""
    xml = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—TEST PART</HEAD>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   General definitions.</HEAD>
    <P><I>Restricted area.</I> A restricted area is airspace designated:</P>
    <P>(1) Under part 73; and</P><P>(2) Elsewhere.</P>
    </DIV8></DIV5></DIV3></DIV1></ECFR>"""
    doc = parser.build_part_docs(ET.fromstring(xml), SOURCE)["9"]
    definition = doc["children"][0]["content"][0]
    assert definition["type"] == "definition"
    assert definition["term"] == "Restricted area."
    assert [c["label"] for c in definition["children"]] == ["(1)", "(2)"]


def test_lossless_check_catches_dropped_punctuation():
    """Punctuation inside authoritative wording is part of the comparison."""
    root = ET.fromstring(
        _MINI_TEMPLATE.format(body="<P>(a) Do this, if that applies.</P>")
    )
    context, div5 = parser.iter_parts(root)[0]
    doc = parser.parse_part(div5, context, SOURCE)
    doc["children"][0]["content"][0]["text"] = "Do this if that applies."  # comma dropped
    with pytest.raises(parser.ParseError, match="lossless-capture check failed"):
        parser._verify_lossless(div5, doc)


def test_lossless_check_catches_dropped_prose_hyphen():
    """Structural-glyph normalization must not excuse altered prose: a
    hyphen dropped inside authoritative wording leaves the word multiset
    intact after splitting, but is not a structural reshape."""
    root = ET.fromstring(
        _MINI_TEMPLATE.format(body="<P>(a) Fixed-wing aircraft only.</P>")
    )
    context, div5 = parser.iter_parts(root)[0]
    doc = parser.parse_part(div5, context, SOURCE)
    doc["children"][0]["content"][0]["text"] = "Fixed wing aircraft only."
    with pytest.raises(parser.ParseError, match="reshaped outside structural fields"):
        parser._verify_lossless(div5, doc)


def test_lossless_check_catches_dropped_prose_parentheses():
    """Parentheses around a prose abbreviation are authoritative wording,
    not a paragraph label — dropping them must fail the gate."""
    root = ET.fromstring(
        _MINI_TEMPLATE.format(
            body="<P>(a) Reports go to the Administrator (FAA) for review.</P>"
        )
    )
    context, div5 = parser.iter_parts(root)[0]
    doc = parser.parse_part(div5, context, SOURCE)
    doc["children"][0]["content"][0]["text"] = (
        "Reports go to the Administrator FAA for review."
    )
    with pytest.raises(parser.ParseError, match="reshaped outside structural fields"):
        parser._verify_lossless(div5, doc)


def test_lossless_accepts_hyphenated_subpart_heading():
    """Part 17's "Subpart G—Pre-Disputes": the heading split off the
    em-dash join is itself hyphenated. Its punctuation survives verbatim
    inside the join, so this is a legitimate reshape, not altered prose."""
    xml = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—TEST PART</HEAD>
    <DIV6 N="G" TYPE="SUBPART"><HEAD>Subpart G—Pre-Disputes</HEAD>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   Test section.</HEAD>
    <P>(a) Some text.</P></DIV8></DIV6></DIV5></DIV3></DIV1></ECFR>"""
    doc = parser.build_part_docs(ET.fromstring(xml), SOURCE)["9"]
    subpart = doc["children"][0]
    assert subpart["label"] == "G"
    assert subpart["heading"] == "Pre-Disputes"


def test_non_cell_element_in_table_row_fails_loudly():
    with pytest.raises(parser.ParseError, match="unexpected 'DIV' inside TR"):
        _build_mini("<TABLE><TR><TD>cell</TD><DIV>layout</DIV></TR></TABLE>")


def test_duplicate_section_ids_fail_loudly():
    """A repeated section citation inside a part must not produce two
    documents sharing one stable id."""
    xml = """<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
    <DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—TEST PART</HEAD>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   First.</HEAD></DIV8>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   Second.</HEAD></DIV8>
    </DIV5></DIV3></DIV1></ECFR>"""
    with pytest.raises(parser.ParseError, match="duplicate stable id.*cfr-14-9.1"):
        parser.build_part_docs(ET.fromstring(xml), SOURCE)


def test_wrong_title_number_fails_loudly():
    """A valid eCFR document for another title must not be republished as
    Title 14 (ids and title_number fields are fixed to 14)."""
    root = ET.fromstring(_MINI_TEMPLATE.format(body="").replace('N="14"', 'N="15"'))
    with pytest.raises(parser.ParseError, match="expected Title 14, found DIV1 N='15'"):
        parser.build_part_docs(root, SOURCE)


def test_ac_accents_decoded_to_combining_marks():
    """GPO <AC/> accents (overbar, dot above) must survive in canonical
    text as combining characters, attached to the preceding character."""
    doc = _build_mini(
        '<P>(a) Expressed in x, y, z, x\n<AC T="b" />, y\n<AC T="b" />, '
        'z\n<AC T="b" />.</P>'
        '<P>(b) W\n<AC T="8" /><E T="52">az</E> is the mean wind speed.</P>'
    )
    content = doc["children"][0]["content"]
    a = _paragraph(content, "(a)")
    assert "ẋ, ẏ, ż" in a["text"]
    b = _paragraph(content, "(b)")
    assert b["text"].startswith("W̄az is the mean wind speed.")


def test_unknown_accent_code_fails_loudly():
    with pytest.raises(parser.ParseError, match="unrecognized AC accent code 'z'"):
        _build_mini('<P>(a) Value x\n<AC T="z" />.</P>')


def test_unknown_inline_tag_fails_loudly():
    """An unknown tag inside a text run could carry attribute-borne meaning;
    it must not be silently flattened to its character content."""
    with pytest.raises(parser.ParseError, match="unexpected element 'GLYPH' inside 'P'"):
        _build_mini('<P>(a) Value <GLYPH T="alpha">a</GLYPH>.</P>')


def test_chapters_outside_a_title_division_fail_loudly():
    """Malformed XML with chapters directly under the root must not be
    republished with Title 14 ids."""
    xml = """<ECFR><DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I—TEST</HEAD>
    <DIV5 N="9" TYPE="PART"><HEAD>PART 9—TEST PART</HEAD>
    <DIV8 N="9.1" TYPE="SECTION"><HEAD>§ 9.1   One.</HEAD></DIV8>
    </DIV5></DIV3></ECFR>"""
    with pytest.raises(parser.ParseError, match="outside a Title 14 division"):
        parser.build_part_docs(ET.fromstring(xml), SOURCE)


def test_document_without_title_division_fails_loudly():
    with pytest.raises(parser.ParseError, match="expected exactly one Title 14 division"):
        parser.build_part_docs(ET.fromstring("<ECFR></ECFR>"), SOURCE)


def test_inline_block_position_preserved():
    """text → footnote → text must not become text+text → footnote."""
    doc = _build_mini(
        "<P>(a) Subject to the limit,<FTNT><P><SU>1</SU> The footnote.</P></FTNT> "
        "the operator must comply.</P>"
    )
    a = _paragraph(doc["children"][0]["content"], "(a)")
    assert a["text"] == "Subject to the limit,"
    kinds = [(b["type"], b.get("text")) for b in a["children"]]
    assert kinds[0][0] == "footnote"
    assert kinds[1] == ("text", "the operator must comply.")


def test_leading_inline_image_in_paragraph():
    """An image opening a paragraph precedes the text, with no empty block."""
    doc = _build_mini('<P><img src="/graphics/eq.gif" /> Where SAR is defined below.</P>')
    content = doc["children"][0]["content"]
    assert content[0] == {"type": "image", "src": "/graphics/eq.gif"}
    assert content[1]["type"] == "text"
    assert content[1]["text"] == "Where SAR is defined below."


def test_invalid_table_span_fails_loudly():
    with pytest.raises(parser.ParseError, match="invalid table cell colspan 'abc'"):
        _build_mini('<TABLE><TR><TD colspan="abc">cell</TD></TR></TABLE>')


def test_title_heading_captured(part91):
    """The DIV1 heading is authoritative wording and must be stored and
    hashed, not dropped with the generic HEAD skip."""
    assert part91["title_heading"] == "Title 14—Aeronautics and Space"


def test_title_heading_affects_canonical_hash():
    root = ET.fromstring(_MINI_TEMPLATE.format(body="<P>(a) Text.</P>"))
    doc_a = parser.build_part_docs(root, SOURCE)["9"]
    changed = ET.fromstring(
        _MINI_TEMPLATE.format(body="<P>(a) Text.</P>").replace(
            "<HEAD>Title 14</HEAD>", "<HEAD>Title 14—Renamed</HEAD>", 1
        )
    )
    doc_b = parser.build_part_docs(changed, SOURCE)["9"]
    assert doc_a["title_heading"] == "Title 14"
    assert doc_b["title_heading"] == "Title 14—Renamed"
    assert doc_a["canonical_hash"] != doc_b["canonical_hash"]


def test_subtitle_heading_captured_and_hashed():
    """A DIV2 subtitle's designator and heading are authoritative wording:
    they must land in the canonical model and affect the hash, not vanish
    with the generic HEAD skip."""
    plain = _MINI_TEMPLATE.format(body="<P>(a) Text.</P>")
    with_subtitle = plain.replace(
        '<DIV3 N="I" TYPE="CHAPTER">',
        '<DIV2 N="A" TYPE="SUBTITLE"><HEAD>Subtitle A—Test Subtitle</HEAD>'
        '<DIV3 N="I" TYPE="CHAPTER">',
    ).replace("</DIV3></DIV1>", "</DIV3></DIV2></DIV1>")
    doc_plain = parser.build_part_docs(ET.fromstring(plain), SOURCE)["9"]
    doc_sub = parser.build_part_docs(ET.fromstring(with_subtitle), SOURCE)["9"]
    assert doc_plain["subtitle"] is None
    assert doc_sub["subtitle"] == "A"
    assert doc_sub["subtitle_heading"] == "Subtitle A—Test Subtitle"
    assert doc_sub["canonical_hash"] != doc_plain["canonical_hash"]


def test_missing_hierarchy_heading_fails_loudly():
    """Hierarchy headings sit outside every DIV5 subtree and escape the
    lossless check; a missing one must fail, not publish None."""
    no_chapter_head = _MINI_TEMPLATE.format(body="").replace(
        "<HEAD>CHAPTER I—TEST</HEAD>", ""
    )
    with pytest.raises(parser.ParseError, match="chapter 'I' has no heading"):
        parser.build_part_docs(ET.fromstring(no_chapter_head), SOURCE)
    no_title_head = _MINI_TEMPLATE.format(body="").replace("<HEAD>Title 14</HEAD>", "")
    with pytest.raises(parser.ParseError, match="title division '14' has no heading"):
        parser.build_part_docs(ET.fromstring(no_title_head), SOURCE)


# ---------------------------------------------------------------------------
# Pending-amendment XREF inside an authority citation (issue 2026-09-03)
# ---------------------------------------------------------------------------


def test_authority_pending_amendment_link_kept_apart_from_authority_text():
    """Part 234 (verbatim from the 2026-09-03 issue) attaches an XREF to its
    AUTH element; it must parse, with the link stored as an amendment note
    and the authority text itself unchanged."""
    root = ET.parse(PART_234_PATH).getroot()
    part = parser.build_part_docs(root, SOURCE, {"234"})["234"]
    assert part["authority"]["heading"] == "Authority:"
    assert part["authority"]["text"] == "49 U.S.C. 329, 41708, and 41709."
    assert part["authority"]["amendment_notes"] == [
        "Link to an amendment published at 91 FR 56592, Sept. 3, 2026."
    ]


def test_notes_without_pending_amendment_carry_no_amendment_key(part91):
    # The key exists only when the source attaches an XREF, so the grammar
    # extension leaves every other document's canonical hash untouched.
    assert "amendment_notes" not in part91["authority"]
    assert all("amendment_notes" not in n for n in part91["editorial_notes"])

