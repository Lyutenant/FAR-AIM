"""Phase 4 tests: FAA AIM HTML → canonical AIM JSON.

Exact-text assertions are transcribed from the accepted Basic-with-Change-3
edition (effective 2026-07-09) and guard against silent wording drift
(plan §17.3, §32.3). The fixture corpus and its provenance are described in
``test_aim_source.py``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from far_aim.models import aim as model
from far_aim.parsers import aim as parser
from far_aim.sources import aim as aim_source
from tests.test_aim_source import (
    AIM_HTML,
    FIXTURE_APPENDICES,
    FIXTURE_CHAPTERS,
    VERSION,
    fetch_fixture,
)

SOURCE = {
    "provider": "faa",
    "publication": "aim",
    "source_version": VERSION,
    "edition_label": "Basic with Change 1, 2 and 3",
    "effective_date": "2026-07-09",
    "change": 3,
    "url": "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/index.html",
    "retrieved_at": "2026-08-29T02:16:02Z",
    "raw_checksum": "sha256:" + "0" * 64,
}


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> tuple[Path, dict]:
    config, result = fetch_fixture(tmp_path_factory.mktemp("aim-snapshot"))
    metadata = aim_source.load_metadata(result.snapshot_dir)
    assert metadata is not None
    return result.snapshot_dir, metadata


@pytest.fixture(autouse=True)
def fixture_baseline(monkeypatch):
    """The fixture corpus is a deliberate subset: relax the completeness gates."""
    monkeypatch.setattr(aim_source, "REQUIRED_CHAPTERS", FIXTURE_CHAPTERS)
    monkeypatch.setattr(aim_source, "REQUIRED_APPENDICES", FIXTURE_APPENDICES)
    monkeypatch.setattr(parser, "REQUIRE_RESOLVED_REFERENCES", False)


@pytest.fixture(scope="module")
def docs(snapshot) -> dict[str, dict]:
    snapshot_dir, metadata = snapshot
    with (
        mock.patch.multiple(
            aim_source, REQUIRED_CHAPTERS=FIXTURE_CHAPTERS, REQUIRED_APPENDICES=FIXTURE_APPENDICES
        ),
        mock.patch.object(parser, "REQUIRE_RESOLVED_REFERENCES", False),
    ):
        return parser.build_aim_docs(snapshot_dir, metadata, SOURCE)


def _paragraph(docs: dict[str, dict], number: str) -> dict:
    for section in docs["chapter-04"]["sections"]:
        for para in section["paragraphs"]:
            if para["paragraph"] == number:
                return para
    raise AssertionError(number)


def _fixture_sha(name: str) -> str:
    return "sha256:" + hashlib.sha256((AIM_HTML / "images" / name).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_document_set(docs):
    assert sorted(docs) == [
        "appendix-1", "appendix-3", "chapter-00", "chapter-04", "publication",
    ]
    publication = docs["publication"]
    assert publication["id"] == "aim"
    assert publication["document_type"] == model.DOCUMENT_TYPE_PUBLICATION
    assert publication["title"] == "Aeronautical Information Manual"
    assert publication["description"] == [
        {
            "type": "text",
            "text": (
                "The Aeronautical Information Manual is the official guide to basic flight "
                "information and ATC procedures."
            ),
        }
    ]
    assert [b["text"] for b in publication["summary"]] == [
        "Effective: 7/9/2026",
        "Change: Change 3",
        "FAA National Headquarters - Aeronautical Information Services",
        "DEPARTMENT OF TRANSPORTATION",
        "FEDERAL AVIATION ADMINISTRATION",
    ]
    assert publication["source"]["url"].endswith("/aim_html/index.html")
    chapter = docs["chapter-04"]
    assert chapter["id"] == "aim-chapter-4"
    assert chapter["document_type"] == model.DOCUMENT_TYPE_CHAPTER
    assert chapter["heading"] == "Air Traffic Control"
    assert chapter["source"]["url"].endswith("/aim_html/chap_4.html")
    assert [s["id"] for s in chapter["sections"]] == ["aim-4-1"]
    section = chapter["sections"][0]
    assert section["heading"] == "Services Available to Pilots"
    assert section["source"]["url"].endswith("/aim_html/chap4_section_1.html")
    assert section["content"] == []
    assert [p["paragraph"] for p in section["paragraphs"]] == [
        "4-1-1", "4-1-2", "4-1-4", "4-1-8", "4-1-9", "4-1-15", "4-1-20",
    ]
    assert parser.count_sections(chapter) == 1
    assert parser.count_paragraphs(chapter) == 7
    assert parser.count_paragraphs(docs["appendix-3"]) == 0


def test_paragraph_identity_fields(docs):
    para = _paragraph(docs, "4-1-9")
    assert para["id"] == "aim-4-1-9"
    assert para["document_type"] == model.DOCUMENT_TYPE_PARAGRAPH
    assert (para["chapter"], para["section"], para["number"]) == (4, 1, 9)
    assert para["heading"] == (
        "Traffic Advisory Practices at Airports Without Operating Control Towers"
    )
    assert para["source"] == {**SOURCE, "url": SOURCE["url"].replace(
        "index.html", "chap4_section_1.html#4-1-9"
    )}
    assert "page" not in para


def test_chapter_zero_section_without_paragraphs(docs):
    chapter = docs["chapter-00"]
    assert chapter["heading"] == "General Information"
    section = chapter["sections"][0]
    assert section["id"] == "aim-0-0"
    assert section["heading"] == "Explanation of Changes"
    assert section["paragraphs"] == []
    assert section["content"][0] == {
        "type": "heading", "level": 2, "text": "EXPLANATION OF CHANGES",
    }
    assert section["content"][1] == {"type": "text", "text": "Explanation of Changes"}
    assert section["content"][2] == {"type": "text", "text": "Effective: July 9, 2026"}
    # A <br> inside a run-in heading survives as a newline; raw source
    # newlines/tabs collapse to single spaces.
    assert section["content"][5] == {
        "type": "text",
        "text": (
            "b. 5-1-1. PREFLIGHT PREPARATION\n5-4-5. INSTRUMENT APPROACH PROCEDURE (IAP) CHARTS"
        ),
    }
    assert section["content"][3]["text"] == (
        "a. 4-7-4. AUTHORITY FOR OPERATONS WITH A SINGLE LONG-RANGE NAVIGATION SYSTEM"
    )


# ---------------------------------------------------------------------------
# Exact text and blocks
# ---------------------------------------------------------------------------


def test_plain_paragraph_text_verbatim(docs):
    assert _paragraph(docs, "4-1-1")["content"] == [
        {
            "type": "text",
            "text": (
                "Centers are established primarily to provide air traffic service to aircraft "
                "operating on IFR flight plans within controlled airspace, and principally "
                "during the en route phase of flight."
            ),
        }
    ]


def test_reference_box_and_unresolved_cross_reference(docs):
    para = _paragraph(docs, "4-1-2")
    assert para["content"][1] == {
        "type": "note",
        "kind": "reference",
        "title": "REFERENCE-",
        "blocks": [{"type": "text", "text": "AIM, Para 5-4-3, Approach Control."}],
    }
    # 5-4-3 is not part of the fixture corpus: recorded, but unresolved.
    assert para["explicit_references"] == [
        {"text": "5-4-3", "target": None, "source": {"href": "chap5_section_4.html#5-4-3"}}
    ]


def test_level_one_list(docs):
    para = _paragraph(docs, "4-1-4")
    (lst,) = para["content"]
    assert lst["type"] == "list" and lst["level"] == 1 and "html_type" not in lst
    assert len(lst["items"]) == 2
    assert lst["items"][1]["blocks"][0]["text"].startswith(
        "Where the public access telephone is recorded, a beeper tone is not required."
    )


def test_note_box_inside_list_item(docs):
    para = _paragraph(docs, "4-1-8")
    item = para["content"][0]["items"][0]
    assert item["blocks"][0]["type"] == "text"
    assert item["blocks"][1] == {
        "type": "note",
        "kind": "note",
        "title": "NOTE-",
        "blocks": [
            {
                "type": "text",
                "text": (
                    "Pilot use of “have numbers” does not indicate receipt of the ATIS "
                    "broadcast. In addition, the controller will provide traffic advisories "
                    "on a workload permitting basis."
                ),
            }
        ],
    }


def test_nested_lists_phraseology_and_table(docs):
    para = _paragraph(docs, "4-1-9")
    assert para["content"][0] == {"type": "text", "text": "(See TBL 4-1-1.)"}
    top = para["content"][1]
    assert top["type"] == "list" and top["level"] == 1
    level_two = top["items"][0]["blocks"][1]
    assert level_two["level"] == 2 and level_two["html_type"] == "a"
    level_three = level_two["items"][0]["blocks"][1]
    assert level_three["level"] == 3 and level_three["html_type"] == "i"
    assert level_three["items"][0]["blocks"][0]["text"] == (
        "All radio-equipped aircraft transmit/receive on a common frequency identified for "
        "the purpose of airport advisories; and"
    )

    tables = _find(para["content"], "table")
    assert len(tables) == 1
    table = tables[0]
    assert table["number"] == "TBL 4-1-9"
    assert table["title"] == "Summary of Recommended Communication Procedures"
    assert len(table["header_rows"]) == 1
    header = table["header_rows"][0]
    assert header[3]["colspan"] == 3 and header[3]["header"] is True
    assert header[3]["blocks"] == [{"type": "text", "text": "Communication/Broadcast Procedures"}]
    assert header[0]["blocks"] == []
    assert len(table["rows"]) == 7
    assert table["rows"][1][1]["blocks"] == [{"type": "text", "text": "UNICOM (No Tower or FSS)"}]
    assert table["foot_rows"] == []

    phraseology = [n for n in _find(para["content"], "note") if n["kind"] == "phraseology"]
    assert len(phraseology) == 2
    assert phraseology[0]["title"] == "PHRASEOLOGY-"
    assert phraseology[0]["blocks"][0]["text"].split("\n") == [
        "FREDERICK UNICOM CESSNA EIGHT ZERO ONE TANGO FOXTROT 10 MILES SOUTHEAST DESCENDING "
        "THROUGH (altitude) LANDING FREDERICK, REQUEST WIND AND RUNWAY INFORMATION FREDERICK.",
        "FREDERICK TRAFFIC CESSNA EIGHT ZERO ONE TANGO FOXTROT ENTERING DOWNWIND/BASE/ FINAL "
        "(as appropriate) FOR RUNWAY ONE NINER (full stop/touch-and-go) FREDERICK.",
        "FREDERICK TRAFFIC CESSNA EIGHT ZERO ONE TANGO FOXTROT CLEAR OF RUNWAY ONE NINER "
        "FREDERICK.",
    ]


def test_figures_carry_archived_checksums(docs):
    para = _paragraph(docs, "4-1-15")
    figures = _find(para["content"], "figure")
    # The FAA numbers both illustrations FIG 4-1-15 (verbatim, not corrected).
    assert [f["number"] for f in figures] == ["FIG 4-1-15", "FIG 4-1-15"]
    assert [f["image"]["source"]["src"] for f in figures] == [
        "images/aim0401_fig79_recovered.svg",
        "images/aim0401_fig80_recovered.svg",
    ]
    assert figures[0]["title"] == "Induced Error in Position of Traffic"
    assert figures[0]["image"] == {
        "alt": "Induced Error in Position of Traffic",
        "sha256": _fixture_sha("aim0401_fig79_recovered.svg"),
        "source": {"src": "images/aim0401_fig79_recovered.svg"},
    }
    examples = [n for n in _find(para["content"], "note") if n["kind"] == "example"]
    assert any(
        e["blocks"][0]["text"].startswith("In FIG 4-1-1 traffic information") for e in examples
    )
    # The example links back to this paragraph's own figure anchor.
    assert {
        "text": "FIG 4-1-1",
        "target": "aim-4-1-15",
        "source": {"href": "chap4_section_1.html#4-1-15"},
    } in para["explicit_references"]


def test_level_four_list_and_section_reference(docs):
    para = _paragraph(docs, "4-1-20")
    levels = {lst["level"] for lst in _find(para["content"], "list")}
    assert levels == {1, 2, 3, 4}
    hrefs = {ref["source"]["href"] for ref in para["explicit_references"]}
    assert "chap5_section_6.html#chap5_section_6" in hrefs


def test_appendix_with_bare_images(docs):
    apx = docs["appendix-1"]
    assert apx["id"] == "aim-appendix-1"
    assert apx["document_type"] == model.DOCUMENT_TYPE_APPENDIX
    assert apx["heading"] == "Bird/Other Wildlife Strike Report"
    images = _find(apx["content"], "image")
    assert [i["source"]["src"] for i in images] == [
        "images/aimapd1_floating1.png",
        "images/aimapd1_BlankFooter0.png",
        "images/aimapd1_BlankFooter0.png",
        "images/aimapd1_floating0.png",
    ]
    assert images[0]["sha256"] == _fixture_sha("aimapd1_floating1.png")


def test_appendix_designation_must_match_page_identity(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "appendix_3.html",
        "Appendix 3. Abbreviations/Acronyms</strong>",
        "Appendix 4. Abbreviations/Acronyms</strong>",
    )
    with pytest.raises(parser.ParseError, match="does not match page appendix 3"):
        parser.build_aim_docs(copy, metadata, SOURCE)
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path / "b", "appendix_3.html",
        "Appendix 3. Abbreviations/Acronyms</strong>", "Appendix 3. Acronyms</strong>",
    )
    with pytest.raises(parser.ParseError, match="does not match page appendix 3"):
        parser.build_aim_docs(copy, metadata, SOURCE)
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path / "c", "appendix_3.html",
        '<p class="p"><strong class="ph b">Appendix 3. Abbreviations/Acronyms</strong></p>', "",
    )
    with pytest.raises(parser.ParseError, match="does not open with its 'Appendix N.'"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_appendix_table(docs):
    apx = docs["appendix-3"]
    assert apx["heading"] == "Abbreviations/Acronyms"
    assert apx["content"][0] == {"type": "text", "text": "Appendix 3. Abbreviations/Acronyms"}
    table = _find(apx["content"], "table")[0]
    assert table["number"] is None and table["title"] is None
    assert [c["blocks"][0]["text"] for c in table["header_rows"][0]] == [
        "Abbreviation/Acronym",
        "Meaning",
    ]
    assert [c["blocks"][0]["text"] for c in table["rows"][0]] == [
        "AAWU",
        "Alaskan Aviation Weather Unit",
    ]
    assert len(table["rows"]) == 5


def _find(blocks: list[dict], kind: str) -> list[dict]:
    found: list[dict] = []
    for block in blocks:
        if block["type"] == kind:
            found.append(block)
        if block["type"] == "list":
            for item in block["items"]:
                found.extend(_find(item["blocks"], kind))
        elif block["type"] == "note":
            found.extend(_find(block["blocks"], kind))
        elif block["type"] == "table":
            for rows in (block["header_rows"], block["rows"], block["foot_rows"]):
                for row in rows:
                    for cell in row:
                        found.extend(_find(cell["blocks"], kind))
    return found


# ---------------------------------------------------------------------------
# Hashing, determinism, references
# ---------------------------------------------------------------------------


def test_canonical_hashes_verify_and_exclude_provenance(docs):
    for doc in docs.values():
        assert model.canonical_hash(doc) == doc["canonical_hash"]
    para = _paragraph(docs, "4-1-1")
    assert model.canonical_hash(para) == para["canonical_hash"]
    altered = json.loads(json.dumps(para))
    altered["source"]["retrieved_at"] = "2030-01-01T00:00:00Z"
    altered["source"]["url"] = "https://example.invalid/"
    assert model.canonical_hash(altered) == para["canonical_hash"]
    altered["content"][0]["text"] += " More."
    assert model.canonical_hash(altered) != para["canonical_hash"]


def test_layout_locations_do_not_affect_hashes(docs):
    """Renamed pages, anchors, or image files with unchanged wording hash identically."""
    para = json.loads(json.dumps(_paragraph(docs, "4-1-15")))
    para["explicit_references"][0]["source"]["href"] = "renamed.html#4-1-15"
    figure = _find(para["content"], "figure")[0]
    figure["image"]["source"]["src"] = "images/renamed.svg"
    assert model.canonical_hash(para) == _paragraph(docs, "4-1-15")["canonical_hash"]
    figure["image"]["sha256"] = "sha256:" + "f" * 64
    assert model.canonical_hash(para) != _paragraph(docs, "4-1-15")["canonical_hash"]


def test_parse_is_deterministic(snapshot):
    snapshot_dir, metadata = snapshot
    first = parser.build_aim_docs(snapshot_dir, metadata, SOURCE)
    second = parser.build_aim_docs(snapshot_dir, metadata, SOURCE)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_reference_resolution_shapes(monkeypatch):
    known = {"aim-4-1-9", "aim-5-4", "aim-appendix-4", "aim-chapter-2"}
    refs = [
        {"href": "chap4_section_1.html#4-1-9", "text": "4-1-9"},
        {"href": "chap4_section_1.html#4-1-9", "text": "4-1-9"},  # duplicate collapses
        {"href": "chap5_section_4.html#chap5_section_4", "text": "Section 4"},
        {"href": "./appendix_4.html", "text": "Appendix 4"},
        {"href": "./chap_2.html", "text": "Chapter 2"},
        {"href": "https://www.faa.gov/uas", "text": "www.faa.gov/uas"},
        {"href": "mailto:9-AJV-P-HQ-Correspondence@faa.gov", "text": "email"},
    ]
    assert parser._resolve_refs(refs, known, "aim-4-1-9") == [
        {"text": "4-1-9", "target": "aim-4-1-9", "source": {"href": "chap4_section_1.html#4-1-9"}},
        {
            "text": "Section 4",
            "target": "aim-5-4",
            "source": {"href": "chap5_section_4.html#chap5_section_4"},
        },
        {"text": "Appendix 4", "target": "aim-appendix-4", "source": {"href": "./appendix_4.html"}},
        {"text": "Chapter 2", "target": "aim-chapter-2", "source": {"href": "./chap_2.html"}},
        {
            "text": "www.faa.gov/uas",
            "target": None,
            "url": "https://www.faa.gov/uas",
            "source": {"href": "https://www.faa.gov/uas"},
        },
        {
            "text": "email",
            "target": None,
            "url": "mailto:9-AJV-P-HQ-Correspondence@faa.gov",
            "source": {"href": "mailto:9-AJV-P-HQ-Correspondence@faa.gov"},
        },
    ]
    # A relative link outside the corpus grammar is malformed source.
    with pytest.raises(parser.ParseError, match="neither an AIM page nor an external URL"):
        parser._resolve_refs([{"href": "../pcg_html/glossary-a.html", "text": "x"}], known)


def test_dangling_in_corpus_reference_fails_by_default(monkeypatch):
    monkeypatch.setattr(parser, "REQUIRE_RESOLVED_REFERENCES", True)  # the shipped default
    dangling = [{"href": "chap9_section_9.html#9-9-9", "text": "9-9-9"}]
    with pytest.raises(parser.ParseError, match="which this edition does not contain"):
        parser._resolve_refs(dangling, {"aim-4-1-9"}, "aim-4-1-9")
    monkeypatch.setattr(parser, "REQUIRE_RESOLVED_REFERENCES", False)
    assert parser._resolve_refs(dangling, {"aim-4-1-9"})[0]["target"] is None


def test_partial_corpus_fails_strict_reference_gate(snapshot, monkeypatch):
    """The fixture corpus cites paragraphs it does not carry (e.g. 5-4-3)."""
    monkeypatch.setattr(parser, "REQUIRE_RESOLVED_REFERENCES", True)
    with pytest.raises(parser.ParseError, match="aim-5-4-3.*does not contain"):
        parser.build_aim_docs(*snapshot, SOURCE)


def test_external_link_destination_is_hashed_content(docs):
    """An external link changed under unchanged display text is a content change."""
    para = json.loads(json.dumps(_paragraph(docs, "4-1-1")))
    (ref,) = parser._resolve_refs(
        [{"href": "https://www.faa.gov/uas", "text": "www.faa.gov/uas"}], set(), para["id"]
    )
    para["explicit_references"] = [ref]
    with_link = model.canonical_hash(para)
    para["explicit_references"][0]["url"] = "https://www.faa.gov/elsewhere"
    assert model.canonical_hash(para) != with_link
    # The raw href alone is provenance and does not move the hash.
    para["explicit_references"][0]["url"] = "https://www.faa.gov/uas"
    para["explicit_references"][0]["source"]["href"] = "https://www.faa.gov/elsewhere"
    assert model.canonical_hash(para) == with_link


def test_duplicate_section_title_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap4_section_1.html",
        '<h2 class="section-title text-center">Section 1. Services Available to Pilots</h2>',
        '<h2 class="section-title text-center">Section 1. Services Available to Pilots</h2>'
        '<h2 class="section-title text-center">Section 1. Services Available to Pilots</h2>',
    )
    with pytest.raises(parser.ParseError, match="exactly one chapter title and one section title"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_inconsistent_paragraph_links_are_rejected():
    """A link whose page and fragment disagree cannot be resolved either way."""
    with pytest.raises(parser.ParseError, match="outside its own page"):
        parser._resolve_refs(
            [{"href": "chap4_section_1.html#5-1-1", "text": "5-1-1"}], {"aim-5-1-1"}
        )
    assert parser._paragraph_href("chap4_section_1.html#4-1-9") == "4-1-9"
    assert parser._paragraph_href("chap4_section_1.html#chap4_section_1") is None


def test_inconsistent_paragraph_link_in_text_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap4_section_1.html",
        'href="chap5_section_4.html#5-4-3"', 'href="chap5_section_4.html#6-4-3"',
    )
    with pytest.raises(parser.ParseError, match="outside its own page"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_inconsistent_paragraph_link_in_chapter_contents_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap_4.html",
        'href="./chap4_section_1.html#4-1-2">4-1-2.', 'href="./chap4_section_1.html#5-1-2">4-1-2.',
    )
    with pytest.raises(parser.ParseError, match="chap_4.html: paragraph link .* own page"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_normalize_text():
    assert parser.normalize_text("  a   b c ") == "a b c"
    assert parser.normalize_text("a \n \n b") == "a\nb"
    assert parser.normalize_text("\n a \n") == "a"
    assert parser.collapse_text("Radio\n\tCommunications  Phraseology") == (
        "Radio Communications Phraseology"
    )


def test_stable_ids():
    assert model.paragraph_id("4-1-9") == "aim-4-1-9"
    assert model.section_id(4, 1) == "aim-4-1"
    assert model.chapter_id(4) == "aim-chapter-4"
    assert model.appendix_id(3) == "aim-appendix-3"
    with pytest.raises(ValueError):
        model.paragraph_id("4-1")


# ---------------------------------------------------------------------------
# Fail-closed gates
# ---------------------------------------------------------------------------


def _mutated_snapshot(snapshot, tmp_path, page: str, old: str, new: str) -> tuple[Path, dict]:
    snapshot_dir, metadata = snapshot
    copy = tmp_path / "snapshot"
    shutil.copytree(snapshot_dir, copy)
    path = copy / "pages" / page
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return copy, metadata


def test_unknown_element_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap4_section_1.html",
        '<p class="p">Centers are established',
        '<blockquote>x</blockquote><p class="p">Centers are established',
    )
    with pytest.raises(parser.ParseError, match="unexpected .* element <blockquote>"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_truncated_archived_page_fails(snapshot, tmp_path):
    snapshot_dir, metadata = snapshot
    copy = tmp_path / "snapshot"
    shutil.copytree(snapshot_dir, copy)
    path = copy / "pages" / "chap4_section_1.html"
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: text.index('<h4 class="paragraph-title" id="4-1-9">')], encoding="utf-8")
    with pytest.raises(parser.ParseError, match="malformed or truncated HTML"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_unknown_inline_element_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap4_section_1.html",
        "Centers are established", "<kbd>Centers</kbd> are established",
    )
    with pytest.raises(parser.ParseError, match="unexpected inline element <kbd>"):
        parser.build_aim_docs(copy, metadata, SOURCE)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('<ol class="ol level-one">', '<ol class="ol level-one" start="3">', "'start'"),
        ('<ol class="ol level-one">', '<ol class="ol level-one" reversed>', "'reversed'"),
        (
            '<li class="li" id="chap4_section_1__c_recording_and_monitoring-li_1">',
            '<li class="li" value="4" id="chap4_section_1__c_recording_and_monitoring-li_1">',
            "'value'",
        ),
    ],
)
def test_explicit_list_numbering_controls_fail(snapshot, tmp_path, old, new, message):
    copy, metadata = _mutated_snapshot(snapshot, tmp_path, "chap4_section_1.html", old, new)
    with pytest.raises(parser.ParseError, match=f"unsupported numbering control {message}"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_paragraph_id_mismatch_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap4_section_1.html", 'id="4-1-4">4-1-4.', 'id="4-1-3">4-1-4.'
    )
    with pytest.raises(parser.ParseError, match="id attribute"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_paragraph_out_of_order_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap4_section_1.html", 'id="4-1-4">4-1-4.', 'id="4-1-1">4-1-1.'
    )
    with pytest.raises(parser.ParseError, match="out of order"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_chapter_contents_disagreement_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap_4.html",
        "4-1-2. Control Towers", "4-1-2. Control Tower",
    )
    with pytest.raises(parser.ParseError, match="differs from chapter contents"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_chapter_page_rejects_unparsed_prose(snapshot, tmp_path):
    """Chapter pages get the same lossless treatment: added prose fails the parse."""
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap_4.html",
        '<div class="chapter-sections">',
        '<div class="chapter-sections"><p class="p">Effective until further notice.</p>',
    )
    with pytest.raises(parser.ParseError, match="unexpected element <p>"):
        parser.build_aim_docs(copy, metadata, SOURCE)
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path / "b", "chap_4.html",
        '<div class="chapter-sections">',
        '<div class="chapter-sections">Effective until further notice.',
    )
    with pytest.raises(parser.ParseError, match="lossless-capture"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_index_page_rejects_unparsed_content(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "index.html",
        '<div class="publication-description">',
        '<div class="notice"><p>Superseded.</p></div><div class="publication-description">',
    )
    with pytest.raises(parser.ParseError, match="unexpected element <div>"):
        parser.build_aim_docs(copy, metadata, SOURCE)
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path / "b", "index.html",
        '<div class="publication-description">',
        'Superseded.<div class="publication-description">',
    )
    with pytest.raises(parser.ParseError, match="text directly inside the index article"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_index_page_image_is_parsed_against_archive(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "index.html",
        '<div class="publication-description">',
        '<div class="publication-description"><p><img class="image" '
        'src="./images/aimapd1_floating0.png" alt="Cover"></p>',
    )
    docs = parser.build_aim_docs(copy, metadata, SOURCE)
    assert docs["publication"]["description"][0] == {
        "type": "image",
        "alt": "Cover",
        "sha256": _fixture_sha("aimapd1_floating0.png"),
        "source": {"src": "images/aimapd1_floating0.png"},
    }
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path / "b", "index.html",
        '<div class="publication-description">',
        '<div class="publication-description"><p><img class="image" '
        'src="./images/missing.png" alt="Cover"></p>',
    )
    with pytest.raises(parser.ParseError, match="missing from the archived snapshot"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_index_wording_change_changes_publication_hash(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "index.html", "official guide", "official guidance"
    )
    before = parser.build_aim_docs(*snapshot, SOURCE)["publication"]["canonical_hash"]
    after = parser.build_aim_docs(copy, metadata, SOURCE)["publication"]["canonical_hash"]
    assert before != after


def test_chapter_page_records_section_labels(snapshot):
    snapshot_dir, _ = snapshot
    html = (snapshot_dir / "pages" / "chap_0.html").read_text(encoding="utf-8")
    page = parser.parse_chapter_page("chap_0.html", html)
    (section,) = page.sections
    assert section.label == "Section 1."
    assert section.page == "chap0_section_0.html"
    assert section.entries == [] and section.extra == ["Explanation of Changes"]


def test_non_paragraph_contents_entry_must_duplicate_section_entry(snapshot, tmp_path):
    """Chapter 0's self-listing carries no wording of its own; any drift fails."""
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap_0.html",
        '<li><a href="./chap0_section_0.html">Explanation of Changes</a></li>',
        '<li><a href="./chap0_section_0.html">Explanation of Changes (Change 3)</a></li>',
    )
    with pytest.raises(parser.ParseError, match="nor a duplicate of its section entry"):
        parser.build_aim_docs(copy, metadata, SOURCE)
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path / "b", "chap_0.html",
        '<li><a href="./chap0_section_0.html">Explanation of Changes</a></li>',
        '<li><a href="./chap4_section_1.html">Explanation of Changes</a></li>',
    )
    with pytest.raises(parser.ParseError, match="nor a duplicate of its section entry"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_toc_label_is_preserved_and_validated(docs, snapshot, tmp_path):
    assert docs["chapter-04"]["sections"][0]["toc_label"] == "Section 1."
    assert docs["chapter-00"]["sections"][0]["toc_label"] == "Section 1."  # documented mismatch
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap_4.html",
        '<span class="section-number">Section 1. </span>',
        '<span class="section-number">Section 9. </span>',
    )
    with pytest.raises(parser.ParseError, match="disagrees with its linked page"):
        parser.build_aim_docs(copy, metadata, SOURCE)
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path / "b", "chap_0.html",
        '<span class="section-number">Section 1. </span>',
        '<span class="section-number">Section 2. </span>',
    )
    with pytest.raises(parser.ParseError, match="disagrees with its linked page"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_chapter_contents_missing_paragraph_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap_4.html",
        '<li><a href="./chap4_section_1.html#4-1-4">4-1-4. Recording and Monitoring</a></li>', "",
    )
    with pytest.raises(parser.ParseError, match="chapter contents list paragraphs"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_section_title_mismatch_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "chap4_section_1.html",
        "Section 1. Services Available to Pilots</h2>",
        "Section 2. Services Available to Pilots</h2>",
    )
    with pytest.raises(parser.ParseError, match="section title"):
        parser.build_aim_docs(copy, metadata, SOURCE)


def test_missing_figure_in_archive_fails(snapshot, tmp_path):
    snapshot_dir, metadata = snapshot
    trimmed = json.loads(json.dumps(metadata))
    del trimmed["files"]["figures/aim0401_fig80_recovered.svg"]
    with pytest.raises(parser.ParseError, match="missing from the archived snapshot"):
        parser.build_aim_docs(snapshot_dir, trimmed, SOURCE)


def _renamed_snapshot(snapshot, tmp_path, old: str, new: str, *, keep_old: bool = False):
    """Copy the snapshot with page ``old`` also/instead available as ``new``."""
    snapshot_dir, metadata = snapshot
    copy = tmp_path / "snapshot"
    shutil.copytree(snapshot_dir, copy)
    pages = copy / "pages"
    shutil.copyfile(pages / old, pages / new)
    meta = json.loads(json.dumps(metadata))
    meta["files"][f"pages/{new}"] = meta["files"][f"pages/{old}"]
    if not keep_old:
        (pages / old).unlink()
        del meta["files"][f"pages/{old}"]
    return copy, meta


def test_chapter_provenance_keeps_archived_page_name(snapshot, tmp_path):
    copy, meta = _renamed_snapshot(snapshot, tmp_path, "chap_4.html", "chap_04.html")
    docs = parser.build_aim_docs(copy, meta, SOURCE)
    assert docs["chapter-04"]["source"]["url"].endswith("/aim_html/chap_04.html")
    # Layout-only rename: content hashes are unchanged.
    original = parser.build_aim_docs(*snapshot, SOURCE)
    assert docs["chapter-04"]["canonical_hash"] == original["chapter-04"]["canonical_hash"]


def test_duplicate_page_identity_in_snapshot_fails(snapshot, tmp_path):
    copy, meta = _renamed_snapshot(snapshot, tmp_path, "chap_4.html", "chap_04.html", keep_old=True)
    with pytest.raises(parser.ParseError, match="denote the same chapter"):
        parser.build_aim_docs(copy, meta, SOURCE)


def test_missing_baseline_chapter_in_snapshot_fails(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(aim_source, "REQUIRED_CHAPTERS", frozenset({0, 4, 11}))
    with pytest.raises(parser.ParseError, match=r"missing baseline chapters \[11\]"):
        parser.build_aim_docs(*snapshot, SOURCE)


def test_lossless_capture_catches_dropped_text(snapshot, monkeypatch):
    snapshot_dir, metadata = snapshot
    real = parser._paragraph_blocks

    def lossy(p, page):
        blocks = real(p, page)
        return [b for b in blocks if not (b["type"] == "text" and b["text"].startswith("Centers"))]

    monkeypatch.setattr(parser, "_paragraph_blocks", lossy)
    with pytest.raises(parser.ParseError, match="lossless-capture"):
        parser.build_aim_docs(snapshot_dir, metadata, SOURCE)
