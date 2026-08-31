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


# ---------------------------------------------------------------------------
# Phase 6: AIM → FAR. Quoted strings are transcribed verbatim from the
# accepted AIM 2026-07-09 Change 3 snapshot.
# ---------------------------------------------------------------------------


def test_aim_cfr_section_word_anchor():
    text = (
        "unless the installed equipment has not been tested and calibrated as "
        "required by 14 CFR section 91.217. If deactivation is required, turn off"
    )
    assert citations.extract_citations(text) == ["91.217"]


def test_aim_cfr_sections_list_and_capitalizations():
    assert citations.extract_citations(
        "are found in 14 CFR sections 91.215, 91.225, and 99.13."
    ) == ["91.215", "91.225", "99.13"]
    assert citations.extract_citations(
        "14 CFR Section 91.125 and 14 CFR Section 91.129."
    ) == ["91.125", "91.129"]
    assert citations.extract_citations("14 CFR SECTION 91.155. WHENEVER") == ["91.155"]


def test_aim_cfr_sign_anchor_variants():
    text = (
        "The airspace described in (e) above is specified in 14 CFR § 91.225 for "
        "ADS-B Out requirements. However, 14 CFR § 91.215 does not include this"
    )
    assert citations.extract_citations(text) == ["91.225", "91.215"]
    assert citations.extract_citations("in 14 CFR §§ 61.66, 91.1065, 121.441, Appendix F") == [
        "61.66",
        "91.1065",
        "121.441",
    ]
    # The FAA's "91.113b" (for 91.113(b)) is captured as written and left for
    # ``resolve`` to drop — never rewritten into a guess.
    assert citations.extract_citations("in accordance with 14CFR §91.113b. TIS-B") == ["91.113b"]
    assert citations.resolve(["91.113b"], {"91.113"}) == []


def test_aim_repeated_section_word_continues_list():
    # AIM 3-2-2, verbatim: the section word recurs mid-list.
    text = (
        "all persons must operate their aircraft under IFR. (See 14 CFR section "
        "71.33, sections 91.167 through 91.193, sections 91.215 through 91.217, "
        "and sections 91.225 through 91.227.)"
    )
    assert citations.extract_citations(text) == [
        "71.33",
        "91.167",
        "91.193",
        "91.215",
        "91.217",
        "91.225",
        "91.227",
    ]
    assert citations.extract_citations("§ 91.3, Section 91.5") == ["91.3", "91.5"]


def test_aim_bare_section_word_not_linked():
    assert citations.extract_citations("prescribed by section 91.185(c)(2) until") == []


def test_part_citations_cfr_anchored():
    assert citations.extract_part_citations("14 CFR part 121 or equivalent criteria.") == ["121"]
    assert citations.extract_part_citations("14 CFR part 91.") == ["91"]
    assert citations.extract_part_citations(
        "120-74A, Parts 91, 121, 125, and 135 Flightcrew Procedures"
    ) == ["91", "121", "125", "135"]
    assert citations.extract_part_citations("Appendix C (14 CFR, part 25 and 29) is") == [
        "25",
        "29",
    ]
    assert citations.extract_part_citations("14 CFR part 91, Appendix D, Section 3") == ["91"]


def test_part_citations_bare_and_subpart_shorthand():
    assert citations.extract_part_citations(
        "Parts 91K, 121, 125, 129, and 135 operators"
    ) == ["91", "121", "125", "129", "135"]
    assert citations.extract_part_citations("for example, part 121, part 91, etc.") == [
        "121",
        "91",
    ]
    assert citations.extract_part_citations(
        "Title 14 of the Code of Federal Regulations, part 97, and are"
    ) == ["97"]
    assert citations.extract_part_citations("Part 107 remote pilots and operators") == ["107"]


def test_part_citations_never_read_sections_or_titles_as_parts():
    assert citations.extract_part_citations("14 CFR part 91.155 applies") == []
    assert citations.extract_part_citations("under part 830 and 14 CFR part 91") == ["830", "91"]
    assert citations.extract_part_citations("no reference at all") == []


def test_part_citations_other_title_banned_textwide():
    text = "in accordance with 49 CFR part 1542. A SIDA can include part 1542 areas and part 91"
    assert citations.extract_part_citations(text) == ["91"]
    assert citations.extract_other_title_parts(text) == {"1542"}
    assert citations.extract_part_citations("49 CFR Part 830 and 14 CFR part 91") == ["91"]


def test_resolve_parts_filters_dedups_orders():
    assert citations.resolve_parts(["135", "91", "1542", "91"], {"91", "135", "1"}) == [
        "91",
        "135",
    ]
