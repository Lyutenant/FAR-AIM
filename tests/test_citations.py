"""Phase 3 tests: deterministic in-text CFR citation extraction (plan §12.1).

Quoted strings are transcribed verbatim from the accepted 2026-08-19
snapshot so the recognizer is exercised on real regulatory phrasing.
"""

from __future__ import annotations

from far_aim.links import citations


def test_simple_section_reference():
    text = (
        "Except as provided in paragraph (b) of this section and § 91.157, no "
        "person may operate an aircraft under VFR"
    )
    assert citations.extract_citations(text) == ["91.157"]


def test_double_section_range_endpoints_only():
    # Endpoints are linked; the range is not expanded.
    assert citations.extract_citations("§§ 91.101 through 91.135 apply") == ["91.101", "91.135"]


def test_cfr_prefixed_list_with_paragraph_suffixes():
    # Part 252's cross-reference note, verbatim.
    text = (
        "For smoking rules affecting these carriers, see 14 CFR 121.317(c), "
        "121.571(a)(1)(i), 129.29, 135.117(a)(1), and 135.127(a)."
    )
    assert citations.extract_citations(text) == [
        "121.317",
        "121.571",
        "129.29",
        "135.117",
        "135.127",
    ]


def test_other_title_sign_reference_rejected():
    assert citations.extract_citations("as defined in 49 CFR § 571.209") == []


def test_other_title_bare_reference_rejected():
    assert citations.extract_citations("must be reported under 49 CFR 830.5") == []


def test_other_title_does_not_poison_later_references():
    text = "see 49 CFR 571.209 and also § 91.205 of this chapter"
    assert citations.extract_citations(text) == ["91.205"]


def test_comma_list_after_section_sign():
    assert citations.extract_citations("in §§ 61.65, 61.127, and 61.129") == [
        "61.65",
        "61.127",
        "61.129",
    ]


def test_reserved_range_token():
    assert citations.extract_citations("§§ 91.27-91.99 are reserved") == ["91.27-91.99"]


def test_bare_part_reference_not_linked():
    assert citations.extract_citations("certificated under part 121 of this chapter") == []


def test_no_reference_no_match():
    assert citations.extract_citations("cloud clearance of 1,000 feet above") == []


def test_resolve_filters_dedups_orders_and_excludes_self():
    known = {"91.155", "91.157", "91.20"}
    tokens = ["91.157", "999.999", "91.20", "91.157", "91.155"]
    assert citations.resolve(tokens, known, exclude="91.155") == ["91.20", "91.157"]


def test_resolve_natural_order():
    known = {"91.20", "91.155", "91.3"}
    assert citations.resolve(["91.155", "91.3", "91.20"], known) == ["91.3", "91.20", "91.155"]


def test_collect_text_covers_nested_blocks():
    blocks = [
        {
            "type": "paragraph",
            "label": "(a)",
            "designator": "a",
            "subject": "Scope.",
            "text": "See § 1.1.",
            "children": [
                {
                    "type": "definition",
                    "term": "Term",
                    "text": "means a thing in § 2.2.",
                    "children": [],
                }
            ],
        },
        {
            "type": "table",
            "caption": "Caption § 3.3",
            "header_rows": [[{"header": True, "text": "H § 4.4"}]],
            "rows": [[{"text": "cell § 5.5"}]],
            "foot_rows": [],
        },
        {
            "type": "note",
            "heading": "Note:",
            "blocks": [{"type": "text", "style": "plain", "text": "§ 6.6"}],
        },
        {"type": "extract", "blocks": [{"type": "text", "style": "plain", "text": "§ 7.7"}]},
    ]
    joined = " ".join(citations.collect_text(blocks))
    tokens = citations.extract_citations(joined)
    assert tokens == ["1.1", "2.2", "3.3", "4.4", "5.5", "6.6", "7.7"]


def test_bare_sign_banned_when_text_attributes_token_to_other_title():
    # Real phrasing from § 152.111: the title is named only later in the
    # sentence, so the bare § must not be read as Title 14.
    text = (
        "Discrimination is prohibited under § 21.7 of the Regulations of the "
        "Office of the Secretary of Transportation (49 CFR 21.7)."
    )
    assert citations.extract_citations(text) == []
    assert citations.extract_other_title_citations(text) == {"21.7"}


def test_ban_does_not_leak_to_other_tokens():
    text = "see § 91.205, and § 21.7 of the DOT regulations (49 CFR 21.7)"
    assert citations.extract_citations(text) == ["91.205"]
