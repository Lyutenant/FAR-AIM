"""Phase 10a tests: archived ACS PDF → canonical JSON.

The fixture PDF is a page subset of FAA-S-ACS-6C (see ``test_acs_source``),
so the whole-document cross-checks run in their subset mode
(``REQUIRE_COMPLETE = False``). Grammar error paths are exercised on
synthetic line streams rather than doctored PDFs.
"""

from __future__ import annotations

import copy

import pytest

from far_aim.models import acs as model
from far_aim.models import cfr as cfr_model
from far_aim.parsers import acs as parser
from far_aim.sources import acs as acs_source
from tests.test_acs_source import FIXTURE_AREAS, LABEL, PDF_URL, VERSION, fetch_fixture

SOURCE = {
    "provider": "faa",
    "publication": "acs",
    "source_version": VERSION,
    "edition_label": LABEL,
    "effective_date": "2024-05-31",
    "url": PDF_URL,
    "retrieved_at": "2026-09-13T00:00:00Z",
    "raw_checksum": "sha256:" + "0" * 64,
}


@pytest.fixture(scope="module")
def docs(tmp_path_factory) -> dict[str, dict]:
    _, result = fetch_fixture(tmp_path_factory.mktemp("acs-parse"))
    metadata = acs_source.load_metadata(result.snapshot_dir)
    assert metadata is not None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(parser, "REQUIRE_COMPLETE", False)
        return parser.build_acs_docs(result.snapshot_dir, metadata, SOURCE)


def _lines(*texts: str, page: int = 10) -> list[parser.Line]:
    return [parser.Line(raw=t, text=parser.normalize_line(t), page=page) for t in texts]


# ---------------------------------------------------------------------------
# Corpus shape
# ---------------------------------------------------------------------------


def test_fixture_corpus_shape(docs):
    assert set(docs) == {"publication", "area-01", "area-11", "area-12",
                         "appendix-1", "appendix-2", "appendix-3"}
    areas = [docs[k] for k in sorted(docs) if docs[k]["document_type"] == model.DOCUMENT_TYPE_AREA]
    assert {a["roman"] for a in areas} == FIXTURE_AREAS
    assert sum(parser.count_tasks(a) for a in areas) == 12
    assert sum(parser.count_elements(a) for a in areas) == 212
    area_one = docs["area-01"]
    assert area_one["heading"] == "Area of Operation I. Preflight Preparation"
    assert [t["letter"] for t in area_one["tasks"]] == list("ABCDEFGHI")


def test_publication_front_matter(docs):
    pub = docs["publication"]
    assert pub["document_number"] == VERSION
    assert pub["title"] == "Private Pilot for Airplane Category Airman Certification Standards"
    assert pub["edition_date"] == "November 2023" and pub["edition_month"] == "2023-11"
    assert pub["cover"]["publisher"] == ["Flight Standards Service", "Washington, DC 20591"]
    assert [b["type"] for b in pub["foreword"]] == ["text"] * 5
    assert pub["revision_history"]["columns"] == ["Document #", "Description", "Date"]
    assert pub["revision_history"]["rows"][-1] == {
        "document_number": VERSION,
        "description": "Private Pilot for Airplane Category Airman Certification Standards",
        "date": "November 2023",
    }
    assert len(pub["changes"]["added"]) == 87 and len(pub["changes"]["removed"]) == 18
    assert "PA.I.B.K1e" in pub["changes"]["added"] and "PA.XII.A.S1" in pub["changes"]["removed"]
    groups = [e["title"] for e in pub["contents"] if e["kind"] == "group"]
    assert groups[0] == "Introduction" and groups[1] == "Area of Operation I. Preflight Preparation"
    assert groups[-1] == (
        "Appendix 3: Aircraft, Equipment, and Operational Requirements & Limitations"
    )
    assert pub["introduction"][0] == {
        "type": "heading",
        "text": "Airman Certification Standards Concept",
    }
    assert pub["source"]["url"] == PDF_URL and pub["source"]["extractor"] == parser.EXTRACTOR


def test_task_document(docs):
    task = docs["area-01"]["tasks"][0]
    assert task["id"] == "acs-PA.I.A" and task["code"] == "PA.I.A"
    assert task["heading"] == "Task A. Pilot Qualifications"
    assert task["title"] == "Pilot Qualifications" and task["classes"] == []
    assert task["references_text"] == (
        "14 CFR parts 61, 68, 91; AC 68-1; FAA-H-8083-2, FAA-H-8083-3, FAA-H-8083-25"
    )
    assert task["references"][0] == {
        "kind": "cfr_parts", "text": "14 CFR parts 61, 68, 91", "parts": ["61", "68", "91"]
    }
    assert [r["kind"] for r in task["references"]] == [
        "cfr_parts", "advisory_circular", "handbook", "handbook", "handbook"
    ]
    assert task["objective"].startswith("To determine the applicant exhibits satisfactory")
    assert task["objective"].endswith("operating as pilot-in-command as a private pilot.")
    assert task["notes"] == []
    assert task["knowledge"]["lead_in"] == "The applicant demonstrates understanding of:"
    assert task["risk"]["label"] == "Risk Management:"
    codes = [e["code"] for e in task["knowledge"]["elements"]]
    assert codes == ["PA.I.A.K1", "PA.I.A.K2", "PA.I.A.K3", "PA.I.A.K4", "PA.I.A.K5"]
    assert task["skills"]["elements"][0]["text"].startswith("Apply requirements to act")
    assert task["source"]["url"] == f"{PDF_URL}#page=10" and task["page"] == 10


def test_sub_elements_notes_and_classes(docs):
    airworthiness = docs["area-01"]["tasks"][1]
    knowledge = airworthiness["knowledge"]["elements"]
    sub = next(e for e in knowledge if e["code"] == "PA.I.B.K1a")
    assert sub["sub"] == "a" and sub["parent"] == "PA.I.B.K1"
    assert sub["text"] == "a. Location and expiration dates of required aircraft certificates"
    weather = docs["area-01"]["tasks"][2]
    assert len(weather["notes"]) == 2 and weather["notes"][0].startswith("If K2 is selected")
    seaplane = docs["area-01"]["tasks"][8]
    assert seaplane["classes"] == ["ASES", "AMES"]
    assert seaplane["title"].startswith("Water and Seaplane Characteristics")
    assert seaplane["references_text"].endswith("POH/AFM; USCG Navigation Rules")
    night = docs["area-11"]["tasks"][0]
    assert night["skills"]["elements"] == []
    assert night["skills"]["lead_in"] == (
        "The applicant exhibits the skill to:[Intentionally left blank]."
    )
    assert night["notes"] == [
        "For applicants that reside in Alaska, refer to 14 CFR part 61, section 61.110."
    ]


def test_archived_placeholders_match_the_change_note(docs):
    removed = set(docs["publication"]["changes"]["removed"])
    archived = {
        e["code"]
        for a in docs.values()
        if a["document_type"] == model.DOCUMENT_TYPE_AREA
        for t in a["tasks"]
        for k in ("knowledge", "risk", "skills")
        for e in t[k]["elements"]
        if "[Archived]" in e["text"]
    }
    assert archived == {"PA.XII.A.R2", "PA.XII.A.S1", "PA.XII.B.R2"}
    assert archived <= removed


def test_appendix_blocks(docs):
    appendix = docs["appendix-1"]
    assert appendix["heading"] == "Appendix 1: Practical Test Roles, Responsibilities, and Outcomes"
    assert appendix["title"] == "Practical Test Roles, Responsibilities, and Outcomes"
    kinds = {b["type"] for b in appendix["content"]}
    assert kinds == {"heading", "text", "note", "list", "preformatted"}
    headings = [b["text"] for b in appendix["content"] if b["type"] == "heading"]
    assert headings[:3] == [
        "Eligibility Requirements for a Private Pilot Certificate",
        "Private Pilot Airplane Knowledge Test Table",
        "Use of the ACS During a Practical Test",
    ]
    table = next(b for b in appendix["content"] if b["type"] == "preformatted")
    assert "Pilot Airplane" in table["lines"][2]  # column layout kept verbatim
    bullets = next(b for b in appendix["content"] if b["type"] == "list")
    assert bullets["items"][0]["text"].startswith("any elements in which the applicant")
    # A paragraph continued across a page break is one paragraph.
    appendix_two = docs["appendix-2"]
    joined = next(
        b
        for b in appendix_two["content"]
        if b["type"] == "text" and "For example, the evaluator" in b["text"]
    )
    assert "the evaluator may develop a scenario" in joined["text"]


def test_layout_artifacts_are_repaired(docs):
    texts = [
        e["text"]
        for a in docs.values()
        if a["document_type"] == model.DOCUMENT_TYPE_AREA
        for t in a["tasks"]
        for k in ("knowledge", "risk", "skills")
        for e in t[k]["elements"]
    ]
    assert not any(" ’s" in t for t in texts)
    assert any("pilot-in-command" in t for t in texts)
    assert parser.normalize_line("   manufacturer ’s   guidance ") == "manufacturer’s guidance"
    assert parser.join_lines(["Straight-and-", "Level Flight"]) == "Straight-and-Level Flight"
    assert parser.join_lines(["first line", "second"]) == "first line second"


def test_hashes_are_stable_and_exclude_provenance(docs):
    for doc in docs.values():
        assert cfr_model.canonical_hash(doc) == doc["canonical_hash"]
    altered = copy.deepcopy(docs["area-01"])
    altered["source"]["extractor"] = "pypdf/0.0.0"
    altered["source"]["retrieved_at"] = "2030-01-01T00:00:00Z"
    assert cfr_model.canonical_hash(altered) == docs["area-01"]["canonical_hash"]
    ids = [t["id"] for a in docs.values() if "tasks" in a for t in a["tasks"]]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Grammar on synthetic streams
# ---------------------------------------------------------------------------


def test_running_header_and_page_number_stripping():
    page = parser.Page(
        number=10,
        lines=_lines(
            "Area of Operation I.  Preflight Preparation",
            "",
            "Task A.  Pilot Qualifications",
            "",
            "   2",
            "",
        ),
    )
    parser.strip_furniture(page)
    assert page.running_header == "Area of Operation I. Preflight Preparation"
    assert page.page_label == "2"
    assert [line.text for line in page.lines] == ["Task A. Pilot Qualifications"]
    cover = parser.Page(number=1, lines=_lines("FAA-S-ACS-6C", "", "Title", ""))
    parser.strip_furniture(cover)
    assert cover.page_label is None and cover.running_header is None


def test_page_labels_must_be_sequential(monkeypatch):
    monkeypatch.setattr(parser, "REQUIRE_COMPLETE", True)

    def pages(*labels: str | None) -> list[parser.Page]:
        out = []
        for index, label in enumerate(labels, start=1):
            page = parser.Page(number=index, lines=[])
            page.page_label = label
            out.append(page)
        return out

    parser.verify_page_labels(pages(None, "i", "ii", "1", "2"))
    with pytest.raises(parser.ParseError, match="expected 2"):
        parser.verify_page_labels(pages(None, "i", "1", "3"))
    with pytest.raises(parser.ParseError, match="no page number"):
        parser.verify_page_labels(pages(None, "i", None))
    with pytest.raises(parser.ParseError, match="cover page carries"):
        parser.verify_page_labels(pages("1", "2"))
    monkeypatch.setattr(parser, "REQUIRE_COMPLETE", False)
    parser.verify_page_labels(pages(None, "i", "1", "63"))  # a subset may skip ahead
    with pytest.raises(parser.ParseError, match="expected 64"):
        parser.verify_page_labels(pages(None, "1", "63", "10"))


def test_body_grammar_rejects_misplaced_and_out_of_sequence_elements():
    body = parser._BodyParser()
    for line in _lines(
        "Area of Operation I.  Preflight Preparation",
        "Task A.  Pilot Qualifications",
        "References:      14 CFR parts 61, 68, 91",
        "Objective:     To determine the applicant exhibits satisfactory knowledge.",
        "Knowledge:          The applicant demonstrates understanding of:",
        "PA.I.A.K1           Certification requirements.",
    ):
        body.feed(line)
    with pytest.raises(parser.ParseError, match="out of sequence"):
        body.feed(_lines("PA.I.A.K3           Skipped a number.")[0])
    with pytest.raises(parser.ParseError, match="does not belong to Area I Task A"):
        body.feed(_lines("PA.I.B.K2           Wrong task.")[0])
    with pytest.raises(parser.ParseError, match="risk element PA.I.A.R1 inside the knowledge"):
        body.feed(_lines("PA.I.A.R1           Wrong section.")[0])
    with pytest.raises(parser.ParseError, match="no parent element"):
        body.feed(_lines("PA.I.A.K2a              a.  Orphan sub-element")[0])
    # A task that ends before its sections are complete is an error, not a
    # partial document.
    with pytest.raises(parser.ParseError, match="expected Knowledge, Risk Management, Skills"):
        body.feed(_lines("Task B.  Airworthiness Requirements")[0])


def test_body_grammar_reads_split_risk_label_and_wrapped_titles():
    body = parser._BodyParser()
    for line in _lines(
        "Area of Operation X.  Multiengine Operations",
        "Task C.  One Engine Inoperative (Simulated) During Straight-and-",
        "             Level Flight and Turns (AMEL, AMES)",
        "References:      FAA-H-8083-2",
        "Objective:     To determine the applicant exhibits satisfactory knowledge, risk",
        "                  management, and skills.",
        "Note:    See Appendix 2: Safety of Flight.",
        "Knowledge:          The applicant demonstrates understanding of:",
        "PA.X.C.K1           Procedures used if engine failure occurs during straight-and-level",
        "                    flight and turns while on instruments.",
        "Risk",
        "Management:         The applicant is able to identify, assess, and mitigate risk:",
        "PA.X.C.R1           Identification of the inoperative engine.",
        "Skills:             The applicant exhibits the skill to:",
        "PA.X.C.S1           Promptly recognize an engine failure.",
        "PA.X.C.S1a              a.  Maintain positive aircraft control",
        "PA.X.C.S1b              b.  Trim as required",
        "Area of Operation XI.  Night Operations",
    ):
        body.feed(line)
    task = body.areas[0].tasks[0]
    doc = parser._task_doc(body.areas[0], task, "PA", SOURCE, PDF_URL)
    assert doc["title"] == (
        "One Engine Inoperative (Simulated) During Straight-and-Level Flight and Turns (AMEL, AMES)"
    )
    assert doc["classes"] == ["AMEL", "AMES"]
    assert doc["objective"] == (
        "To determine the applicant exhibits satisfactory knowledge, risk management, and skills."
    )
    assert doc["notes"] == ["See Appendix 2: Safety of Flight."]
    first = doc["knowledge"]["elements"][0]["text"]
    assert first.endswith("flight and turns while on instruments.")
    skills = [e["code"] for e in doc["skills"]["elements"]]
    assert skills == ["PA.X.C.S1", "PA.X.C.S1a", "PA.X.C.S1b"]
    with pytest.raises(parser.ParseError, match="not followed by 'Management:'"):
        parser._BodyParser.feed  # noqa: B018 - attribute exists
        broken = parser._BodyParser()
        for line in _lines(
            "Area of Operation I.  Preflight Preparation",
            "Task A.  Pilot Qualifications",
            "References:      14 CFR part 61",
            "Objective:     To determine.",
            "Knowledge:          The applicant demonstrates understanding of:",
            "PA.I.A.K1           Something.",
            "Risk",
            "PA.I.A.R1           Not a label.",
        ):
            broken.feed(line)


def test_block_grammar():
    blocks = parser.parse_blocks(
        _lines(
            "Eligibility Requirements for a Private Pilot Certificate",
            "",
            "The prerequisite requirements and general eligibility for a practical test and the "
            "specific",
            "of a Private Pilot Certificate can be found in 14 CFR part 61.",
            "",
            "   Note:    An   applicant   seeking   to   add   a   class   must   comply   with"
            "   14   CFR",
            "            section 61.63, as applicable.",
            "",
            "The required minimum elements include:",
            "       •   at least one knowledge element;",
            "       •   at least one risk management element; and",
            "           continued item text.",
            "",
            "                      Test                              Number of",
            "                     Code       Test Name              Questions",
            "",
            "                      PAR       Private Pilot Airplane          60",
            "",
            "           PA   = Applicable ACS",
            "           I = Area of Operation",
        ),
        "Appendix 1",
    )
    # Consecutive preformatted runs merge into one block (a table's rows).
    assert [b["type"] for b in blocks] == [
        "heading", "text", "note", "text", "list", "preformatted"
    ]
    assert blocks[1]["text"].startswith("The prerequisite requirements")
    assert blocks[2] == {
        "type": "note",
        "label": "Note",
        "text": (
            "An applicant seeking to add a class must comply with 14 CFR section 61.63, as "
            "applicable."
        ),
    }
    assert blocks[4]["items"] == [
        {"text": "at least one knowledge element;"},
        {"text": "at least one risk management element; and continued item text."},
    ]
    assert blocks[5]["lines"][0].startswith("           Test") and blocks[5]["lines"][2] == ""
    assert blocks[5]["lines"][-2:] == ["PA   = Applicable ACS", "I = Area of Operation"]
    words: list[str] = []
    parser.block_words(blocks, words)
    assert words[:3] == ["Eligibility", "Requirements", "for"] and "•" in words


def test_contents_grammar_joins_wrapped_entries():
    entries = parser.parse_contents(
        _lines(
            "Introduction",
            "     Airman Certification Standards Concept.................   1",
            "",
            "Area of Operation X.  Multiengine Operations",
            "     Task C.  One Engine Inoperative (Simulated) During Straight-",
            "                    and-Level Flight and Turns (AMEL, AMES)..........60",
        )
    )
    assert entries == [
        {"kind": "group", "title": "Introduction"},
        {"kind": "entry", "title": "Airman Certification Standards Concept", "page": 1},
        {"kind": "group", "title": "Area of Operation X. Multiengine Operations"},
        {
            "kind": "entry",
            "title": (
                "Task C. One Engine Inoperative (Simulated) During Straight-and-Level Flight "
                "and Turns (AMEL, AMES)"
            ),
            "page": 60,
        },
    ]
    with pytest.raises(parser.ParseError, match="dangling"):
        parser.parse_contents(_lines("Task D.  Never finished"))


def test_changes_grammar():
    changes = parser.parse_changes(
        _lines(
            "• The following ACS codes have been added:",
            "           PA.I.B.K1e                 PA.II.A.S4",
            "           PA.I.B.K4",
            "",
            "• The following ACS codes have been removed and archived. Please see the Companion",
            "  Guide for Pilots (FAA-G-ACS-2) for more information.",
            "          PA.III.A.R3",
            "• Legends have been added.",
        ),
        "FAA-S-ACS-6C",
    )
    assert changes["added"] == ["PA.I.B.K1e", "PA.II.A.S4", "PA.I.B.K4"]
    assert changes["removed"] == ["PA.III.A.R3"]
    assert [b["text"] for b in changes["bullets"]][2] == "Legends have been added."
    with pytest.raises(parser.ParseError, match="unrecognized bullet"):
        parser.parse_changes(_lines("• Something else:", "           PA.I.B.K1e"), "FAA-S-ACS-6C")
    with pytest.raises(parser.ParseError, match="before the first bullet"):
        parser.parse_changes(_lines("PA.I.B.K1e"), "FAA-S-ACS-6C")


def test_change_note_cross_check():
    area = {
        "roman": "I",
        "tasks": [
            {
                "knowledge": {"elements": [{"code": "PA.I.A.K1", "text": "Live."}]},
                "risk": {"elements": [{"code": "PA.I.A.R1", "text": "[Archived]"}]},
                "skills": {"elements": []},
            }
        ],
    }
    parser._verify_changes({"added": ["PA.I.A.K1"], "removed": ["PA.I.A.R1"]}, [area])
    with pytest.raises(parser.ParseError, match="does not contain"):
        parser._verify_changes({"added": ["PA.I.A.K9"], "removed": ["PA.I.A.R1"]}, [area])
    with pytest.raises(parser.ParseError, match="still live"):
        parser._verify_changes({"added": [], "removed": ["PA.I.A.K1", "PA.I.A.R1"]}, [area])
    with pytest.raises(parser.ParseError, match="not listed under removed"):
        parser._verify_changes({"added": [], "removed": []}, [area])


def test_references_grammar():
    refs = parser.parse_references(
        "14 CFR part 91; AC 91-92; AIM; FAA-H-8083-2, FAA-H-8083-28; POH/AFM"
    )
    assert [r["kind"] for r in refs] == [
        "cfr_parts", "advisory_circular", "aim", "handbook", "handbook", "other"
    ]
    assert refs[0]["parts"] == ["91"] and refs[-1]["text"] == "POH/AFM"
    with pytest.raises(parser.ParseError, match="unparseable CFR citation"):
        parser.parse_references("14 CFR part")
