"""Phase 5 tests: FAA PCG HTML → canonical PCG JSON.

Exact-text assertions are transcribed from the accepted Basic-with-Change-3
edition (effective 2026-07-09) and guard against silent wording drift
(plan §17.3, §32.3). The fixture corpus and its provenance are described in
``test_pcg_source.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from far_aim.models import pcg as model
from far_aim.parsers import pcg as parser
from far_aim.sources import pcg as pcg_source
from tests.test_pcg_source import FIXTURE_LETTERS, VERSION, fetch_fixture

SOURCE = {
    "provider": "faa",
    "publication": "pcg",
    "source_version": VERSION,
    "edition_label": "Basic with Change 1, 2 and 3",
    "effective_date": "2026-07-09",
    "change": 3,
    "url": "https://www.faa.gov/air_traffic/publications/atpubs/pcg_html/index.html",
    "retrieved_at": "2026-08-30T00:00:00Z",
    "raw_checksum": "sha256:" + "0" * 64,
}


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> tuple[Path, dict]:
    config, result = fetch_fixture(tmp_path_factory.mktemp("pcg-snapshot"))
    metadata = pcg_source.load_metadata(result.snapshot_dir)
    assert metadata is not None
    return result.snapshot_dir, metadata


@pytest.fixture(autouse=True)
def fixture_baseline(monkeypatch):
    """The fixture corpus is a deliberate subset: relax the completeness gates."""
    monkeypatch.setattr(pcg_source, "REQUIRED_LETTERS", FIXTURE_LETTERS)
    monkeypatch.setattr(parser, "REQUIRE_RESOLVED_REFERENCES", False)


@pytest.fixture(scope="module")
def docs(snapshot) -> dict[str, dict]:
    snapshot_dir, metadata = snapshot
    with (
        mock.patch.object(pcg_source, "REQUIRED_LETTERS", FIXTURE_LETTERS),
        mock.patch.object(parser, "REQUIRE_RESOLVED_REFERENCES", False),
    ):
        return parser.build_pcg_docs(snapshot_dir, metadata, SOURCE)


def _term(docs: dict[str, dict], term_id: str) -> dict:
    for doc in docs.values():
        if doc["document_type"] != model.DOCUMENT_TYPE_LETTER:
            continue
        for term in doc["terms"]:
            if term["id"] == term_id:
                return term
    raise AssertionError(term_id)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_document_set(docs):
    assert sorted(docs) == [
        "letter-b", "letter-k", "letter-n", "letter-o", "letter-t", "publication",
    ]
    publication = docs["publication"]
    assert publication["id"] == "pcg"
    assert publication["document_type"] == model.DOCUMENT_TYPE_PUBLICATION
    assert publication["title"] == "Pilot/Controller Glossary"
    assert publication["purpose"][0] == {"type": "text", "text": "PURPOSE"}
    purpose_list = publication["purpose"][1]
    assert purpose_list["type"] == "list" and purpose_list["style"] == "a"
    assert purpose_list["items"][0]["text"].startswith(
        "This Glossary was compiled to promote a common understanding"
    )
    assert [b["text"] for b in publication["summary"]] == [
        "Effective: 7/9/26",
        "Change: 3",
        "FAA National Headquarters (FOB-10B) Publications & Administration (AJV-P12)",
        "DEPARTMENT OF TRANSPORTATION",
        "FEDERAL AVIATION ADMINISTRATION",
    ]
    assert publication["source"]["url"].endswith("/pcg_html/index.html")
    letter = docs["letter-k"]
    assert letter["id"] == "pcg-letter-k"
    assert letter["letter"] == "K"
    assert parser.count_terms(letter) == 1
    assert parser.count_terms(publication) == 0
    assert letter["source"]["url"].endswith("/pcg_html/glossary-k.html")


def test_term_identity_and_source_anchor(docs):
    term = _term(docs, "pcg-known-traffic")
    assert term["document_type"] == model.DOCUMENT_TYPE_TERM
    assert term["letter"] == "K"
    assert term["term"] == "KNOWN TRAFFIC"
    assert term["source"]["url"].endswith("/pcg_html/glossary-k.html#KNOWN_TRAFFIC")


def test_plain_definition_verbatim(docs):
    term = _term(docs, "pcg-known-traffic")
    assert term["content"][0] == {
        "type": "entry",
        "text": (
            "KNOWN TRAFFIC- With respect to ATC clearances, means aircraft whose altitude, "
            "position, and intentions are known to ATC."
        ),
    }


def test_stub_entry_with_empty_definition_and_see_reference(docs):
    term = _term(docs, "pcg-ndb")
    assert term["content"][0] == {"type": "entry", "text": "NDB-"}
    ref = term["content"][1]
    assert ref["type"] == "reference" and ref["kind"] == "see" and ref["form"] == "row"
    assert ref["text"] == "NONDIRECTIONAL BEACON"
    assert ref["target"] == "pcg-nondirectional-beacon"
    assert ref["source"]["href"].startswith("glossary-n.html#")


def test_dfn_less_entry_with_blank_dfn(docs):
    """BRAKING ACTION carries an empty <dfn>; the term splits from the text."""
    term = _term(docs, "pcg-braking-action-good-good-to-medium-medium-medium-to-poor-poor-or-nil")
    assert term["term"] == (
        "BRAKING ACTION (GOOD, GOOD TO MEDIUM, MEDIUM, MEDIUM TO POOR, POOR, OR NIL)"
    )
    assert term["content"][0]["text"].endswith(
        "good, good to medium, medium, medium to poor, poor, or nil."
    )


def test_class21_entry_without_entry_class(docs):
    """NAVSPEC is published without the term-entry class (CLASS_21)."""
    term = _term(docs, "pcg-navspec")
    assert term["term"] == "NAVSPEC"
    assert term["content"][0] == {"type": "entry", "text": "NAVSPEC-"}
    ref = term["content"][1]
    assert ref["form"] == "parenthetical" and ref["kind"] == "see"
    assert ref["raw"] == "(See NAVIGATION SPECIFICATION [ICAO].)"
    assert ref["text"] == "NAVIGATION SPECIFICATION [ICAO]"
    assert ref["target"] == "pcg-navigation-specification-icao"


def test_or_joined_duplicate_definitions_merge(docs):
    term = _term(docs, "pcg-outer-fix")
    kinds = [(b["type"], b.get("text", "")[:30]) for b in term["content"]]
    assert kinds[0][0] == "entry" and kinds[0][1].startswith("OUTER FIX- A general term")
    assert {"type": "text", "text": "OR"} in term["content"]
    entries = [b for b in term["content"] if b["type"] == "entry"]
    assert len(entries) == 2
    assert entries[1]["text"].startswith("OUTER FIX- An adapted fix along the converted route")


def test_sub_lists_and_labeled_continuations(docs):
    term = _term(docs, "pcg-obstacle-free-zone")
    lists = [b for b in term["content"] if b["type"] == "list"]
    assert lists[0]["style"] == "a"
    assert lists[0]["items"][0]["text"].startswith("Runway OFZ.")
    assert any(lst["style"] == "1" for lst in lists)
    texts = [b["text"] for b in term["content"] if b["type"] == "text"]
    assert "(a) 400 feet, or" in texts
    assert "(c) 120 feet for other runways serving small airplanes with approach speeds of " \
           "less than 50 knots." in texts


def test_traffic_pattern_notes_and_references(docs):
    term = _term(docs, "pcg-traffic-pattern")
    notes = [b for b in term["content"] if b["type"] == "note"]
    assert notes[0]["label"] == "NOTE-"
    assert notes[0]["text"].startswith("ATC may instruct a pilot to report a “2‐mile left base”")
    assert notes[1]["label"] == "REFERENCE-"
    assert notes[1]["text"] == (
        "Pilot's Handbook of Aeronautical Knowledge, FAA-H-8083-25, Chapter 14, "
        "Airport Operations, Traffic Patterns."
    )
    refs = [b for b in term["content"] if b["type"] == "reference"]
    see = [r for r in refs if r["kind"] == "see"]
    assert [r["text"] for r in see] == [
        "STRAIGHT‐IN APPROACH VFR",
        "TAXI PATTERNS",
        "ICAO term AERODROME TRAFFIC CIRCUIT",
    ]
    refer = [r for r in refs if r["kind"] == "refer"]
    assert refer[0]["text"] == "14 CFR part 91"
    assert refer[0].get("url") is None
    # The external AIM row: decorative glyphs stripped, destination kept.
    assert refer[1]["text"] == "AIM"
    assert refer[1]["url"].endswith("/atpubs/aim_html/")
    sub = [b for b in term["content"] if b["type"] == "list"]
    assert sub[0]["items"][0]["text"].startswith("Upwind Leg-")


def test_aside_note_with_label(docs):
    term = _term(docs, "pcg-navaid-classes")
    note = next(b for b in term["content"] if b["type"] == "note")
    assert note["label"] == "Note:"
    assert note["text"].startswith("The normal service range")


def test_external_cfr_link_is_content(docs):
    term = _term(docs, "pcg-operations-over-people-oop")
    refs = [b for b in term["content"] if b["type"] == "reference"]
    cfr = next(r for r in refs if r.get("url", "").startswith("https://www.ecfr.gov"))
    assert cfr["text"] == "14 CFR part 107"


# ---------------------------------------------------------------------------
# Term extraction unit rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("KNOWN TRAFFIC- With respect to ATC clearances.", "KNOWN TRAFFIC"),
        ("DOWNLINK– CPDLC message sent from the flight deck to ATC.", "DOWNLINK"),
        (
            "AUTOMATIC DEPENDENT SURVEILLANCE- REBROADCAST (ADS-R)- A datalink translation.",
            "AUTOMATIC DEPENDENT SURVEILLANCE- REBROADCAST (ADS-R)",
        ),
        ("AUTOMATED SERVICES–Services delivered via an automated system.", "AUTOMATED SERVICES"),
        (
            "AREA NAVIGATION (RNAV) APPROACH CONFIGURATION:",
            "AREA NAVIGATION (RNAV) APPROACH CONFIGURATION",
        ),
        ("E‐MSAW-", "E‐MSAW"),
    ],
)
def test_split_term(text, expected):
    term = parser.split_term(text)
    assert term is not None
    assert parser._TRAILING_SEP_RE.sub("", term).strip() == expected


def test_split_term_bare_stub():
    assert parser.split_term("RC") is None


# Verbatim entries from the accepted edition whose <dfn> closes mid-term
# (definitions shortened; retained wording untouched), plus a well-formed
# control entry whose definition starts "-A …" like the malformed dash shape.
_MALFORMED_DFN_PAGE = """<html><body><main class="pcg-main">
<article class="pcg-content">
<p id="pcg-a.html.1" class="CLASS_12" align="CENTER"><b><font size="+4">A</font></b>
</p>
<p class="CLASS_14 glossary-term-definition glossary-term-entry" align="JUSTIFY" \
id="ADVANCED_AIR_MOBILITY_AAM"><dfn class="term-name">ADVANCED AIR MOBILITY (AAM)</dfn>-A \
transportation system that transports people and property by air between two points in the NAS.
</p>
<p class="CLASS_14 glossary-term-definition glossary-term-entry" align="JUSTIFY" \
id="AUTOMATIC_DEPENDENT_SURVEILLANCE"><dfn class="term-name">AUTOMATIC DEPENDENT \
SURVEILLANCE</dfn>-BROADCAST (ADS‐B)- A surveillance system in which an aircraft or vehicle \
to be detected is fitted with cooperative equipment in the form of a data link transmitter.
</p>
<p class="CLASS_14 glossary-term-definition glossary-term-entry" align="JUSTIFY" \
id="AUTOMATIC_DEPENDENT_SURVEILLANCE-BROADCAST_IN_ADS"><dfn class="term-name">AUTOMATIC \
DEPENDENT SURVEILLANCE-BROADCAST IN (ADS</dfn>-B In)- Aircraft avionics capable of receiving \
ADS-B Out transmissions directly from other aircraft.
</p>
</article></main></body></html>"""


def test_truncated_dfn_terms_recover_from_entry_text():
    letter, terms, _ = parser.parse_letter_page("glossary-a.html", _MALFORMED_DFN_PAGE)
    assert letter == "A"
    assert [t["term"] for t in terms] == [
        "ADVANCED AIR MOBILITY (AAM)",  # "-A transportation…" is a definition, not truncation
        "AUTOMATIC DEPENDENT SURVEILLANCE-BROADCAST (ADS‐B)",
        "AUTOMATIC DEPENDENT SURVEILLANCE-BROADCAST IN (ADS-B In)",
    ]
    # The verbatim entry text is untouched by the recovery.
    assert terms[1]["content"][0]["text"].startswith(
        "AUTOMATIC DEPENDENT SURVEILLANCE-BROADCAST (ADS‐B)- A surveillance system"
    )


def test_unrecoverable_truncated_dfn_fails():
    """If the recovered term does not extend the truncated dfn, the parse fails."""
    page = _MALFORMED_DFN_PAGE.replace(
        "SURVEILLANCE</dfn>", "SURVEILLANCE- Whatever</dfn>"
    )
    with pytest.raises(parser.ParseError, match="malformed dfn boundary"):
        parser.parse_letter_page("glossary-a.html", page)


def test_continuation_detection():
    assert parser.is_continuation("Types of icing are")
    assert parser.is_continuation("Intensity of icing")
    assert not parser.is_continuation("EXPECT (ALTITUDE) AT (TIME) or (FIX)")
    assert not parser.is_continuation("ICAO Three-Letter Designator (3LD)")


def test_stable_ids():
    assert model.term_id("CONTROLLED AIRSPACE") == "pcg-controlled-airspace"
    assert model.term_id("ACC [ICAO]") == "pcg-acc-icao"
    assert model.term_id("ADS-B") == "pcg-ads-b"
    assert model.letter_id("a") == "pcg-letter-a"
    with pytest.raises(ValueError):
        model.letter_id("aa")
    with pytest.raises(ValueError):
        model.term_id("—")


# ---------------------------------------------------------------------------
# Hashing, determinism, references
# ---------------------------------------------------------------------------


def test_canonical_hashes_verify_and_exclude_provenance(docs):
    for doc in docs.values():
        assert model.canonical_hash(doc) == doc["canonical_hash"]
    term = _term(docs, "pcg-known-traffic")
    altered = json.loads(json.dumps(term))
    altered["source"]["retrieved_at"] = "2030-01-01T00:00:00Z"
    altered["source"]["url"] = "https://example.invalid/"
    assert model.canonical_hash(altered) == term["canonical_hash"]
    altered["content"][0]["text"] += " More."
    assert model.canonical_hash(altered) != term["canonical_hash"]


def test_reference_href_is_provenance_url_is_content(docs):
    term = _term(docs, "pcg-traffic-pattern")
    refer = [b for b in term["content"] if b["type"] == "reference" and b.get("url")]
    altered = json.loads(json.dumps(term))
    for block in altered["content"]:
        if block["type"] == "reference" and block.get("source", {}).get("href"):
            block["source"]["href"] = "renamed.html#X"
    assert model.canonical_hash(altered) == term["canonical_hash"]
    altered2 = json.loads(json.dumps(term))
    for block in altered2["content"]:
        if block["type"] == "reference" and block.get("url"):
            block["url"] = "https://elsewhere.invalid/"
    assert refer and model.canonical_hash(altered2) != term["canonical_hash"]


def test_parse_is_deterministic(snapshot):
    snapshot_dir, metadata = snapshot
    first = parser.build_pcg_docs(snapshot_dir, metadata, SOURCE)
    second = parser.build_pcg_docs(snapshot_dir, metadata, SOURCE)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_dangling_linked_reference_fails_by_default(snapshot, monkeypatch):
    """The fixture corpus links terms on letters it does not carry."""
    monkeypatch.setattr(parser, "REQUIRE_RESOLVED_REFERENCES", True)  # the shipped default
    with pytest.raises(parser.ParseError, match="does not contain"):
        parser.build_pcg_docs(*snapshot, SOURCE)


def test_lookup_rules():
    index = parser._TermIndex()
    index.add("ADVISORY CIRCULAR (AC)", "pcg-advisory-circular-ac")
    index.add("INSTRUMENT FLIGHT RULES (IFR)", "pcg-ifr-full")
    index.add("INSTRUMENT FLIGHT RULES [ICAO]", "pcg-ifr-icao")
    index.add("WIDE‐AREA AUGMENTATION SYSTEM (WAAS)", "pcg-waas")
    assert index.lookup("ADVISORY CIRCULAR") == "pcg-advisory-circular-ac"
    assert index.lookup("ADVISORY CIRCULAR (AC).") == "pcg-advisory-circular-ac"
    # The [ICAO] tag stays significant: the bare name maps to the FAA term.
    assert index.lookup("INSTRUMENT FLIGHT RULES") == "pcg-ifr-full"
    assert index.lookup("ICAO term INSTRUMENT FLIGHT RULES") == "pcg-ifr-icao"
    # Unicode/ASCII hyphen variants unify.
    assert index.lookup("WIDE-AREA AUGMENTATION SYSTEM (WAAS)") == "pcg-waas"
    assert index.lookup("NO SUCH TERM") is None


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
    entry_open = '<p class="CLASS_14 glossary-term-definition glossary-term-entry"'
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "glossary-k.html",
        entry_open, f"<blockquote>x</blockquote>{entry_open}",
    )
    with pytest.raises(parser.ParseError, match="unexpected element <blockquote>"):
        parser.build_pcg_docs(copy, metadata, SOURCE)


def test_letter_heading_mismatch_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "glossary-k.html",
        '<font size="+4">K</font>', '<font size="+4">Q</font>',
    )
    with pytest.raises(parser.ParseError, match="letter heading"):
        parser.build_pcg_docs(copy, metadata, SOURCE)


def test_lossless_capture_catches_dropped_text(snapshot, monkeypatch):
    real = parser._entry_term

    def lossy(p, page):
        term, text = real(p, page)
        if text.startswith("KNOWN TRAFFIC"):
            return term, "KNOWN TRAFFIC-"
        return term, text

    monkeypatch.setattr(parser, "_entry_term", lossy)
    with pytest.raises(parser.ParseError, match="lossless-capture"):
        parser.build_pcg_docs(*snapshot, SOURCE)


def test_unparseable_parenthetical_reference_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "glossary-n.html",
        "(See NAVIGATION SPECIFICATION [ICAO].)", "See NAVIGATION SPECIFICATION.",
    )
    with pytest.raises(parser.ParseError, match="unparseable parenthetical"):
        parser.build_pcg_docs(copy, metadata, SOURCE)


def test_numbering_controls_fail(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "glossary-o.html",
        '<ol type="a" class="glossary-sub-list">',
        '<ol type="a" start="3" class="glossary-sub-list">',
    )
    with pytest.raises(parser.ParseError, match="unsupported numbering control 'start'"):
        parser.build_pcg_docs(copy, metadata, SOURCE)


def test_truncated_archived_page_fails(snapshot, tmp_path):
    snapshot_dir, metadata = snapshot
    copy = tmp_path / "snapshot"
    shutil.copytree(snapshot_dir, copy)
    path = copy / "pages" / "glossary-o.html"
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: text.index('id="OUTER_FIX"')], encoding="utf-8")
    with pytest.raises(parser.ParseError, match="malformed or truncated HTML"):
        parser.build_pcg_docs(copy, metadata, SOURCE)


def test_content_before_first_term_fails(snapshot, tmp_path):
    copy, metadata = _mutated_snapshot(
        snapshot, tmp_path, "glossary-k.html",
        '<p class="CLASS_14 glossary-term-definition glossary-term-entry"',
        '<aside class="glossary-note" role="note"><span class="note-content">x</span></aside>'
        '<p class="CLASS_14 glossary-term-definition glossary-term-entry"',
    )
    with pytest.raises(parser.ParseError, match="before the first term entry"):
        parser.build_pcg_docs(copy, metadata, SOURCE)
