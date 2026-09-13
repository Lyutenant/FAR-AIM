"""Unit tests for the change gates (plan §38): ledger, thresholds, AIM change note."""

from __future__ import annotations

from pathlib import Path

import pytest

from far_aim import changes
from far_aim.changes import (
    AIM,
    FAR,
    PCG,
    ChangeNote,
    CorpusThresholds,
    Ledger,
    Move,
    NoteRecord,
    Threshold,
)
from far_aim.parsers import aim as aim_parser

AIM_HTML = Path(__file__).parent / "fixtures" / "aim" / "aim_html"


# ---------------------------------------------------------------------------
# Reading notes
# ---------------------------------------------------------------------------


def _note(
    kind: str,
    canonical_hash: str,
    body: str,
    *,
    extra: str = "",
    generated: bool = True,
    version: str = 'source_version: "2026-09-03"\n',
) -> bytes:
    return (
        "---\n"
        f'id: "x"\ntype: "{kind}"\ncitation: "cite"\n'
        f"{version}"
        f'canonical_hash: "{canonical_hash}"\n'
        f"generated: {'true' if generated else 'false'}\n"
        'title: "T"\naliases:\n  - "a"\n  - "b"\n'
        f"{extra}---\n\n# T\n\n## Official Text\n\n{body}\n\n## Explicit Cross-References\n\nx\n"
    ).encode()


def test_read_frontmatter_understands_the_emitted_value_space():
    fm = changes.read_frontmatter(_note("regulation", "sha256:1", "text", extra="change: 3\n"))
    assert fm == {
        "id": "x",
        "type": "regulation",
        "citation": "cite",
        "source_version": "2026-09-03",
        "canonical_hash": "sha256:1",
        "generated": True,
        "title": "T",
        "aliases": ["a", "b"],
        "change": 3,
    }
    assert changes.read_frontmatter(b"no frontmatter") is None
    assert changes.read_frontmatter(b"---\nkey: value\n---\n") is None  # plain scalars not emitted
    assert changes.read_frontmatter(b"---\nkey: 1\n") is None  # unterminated


def test_official_text_is_the_section_body_only():
    data = _note("regulation", "sha256:1", "- (a) body\n    - (1) nested")
    assert changes.official_text(data) == b"\n\n- (a) body\n    - (1) nested\n\n"
    assert changes.official_text(b"---\n---\n# T\n\n## Contents\n\nx\n") is None


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, data: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_build_ledgers_classifies_every_case(tmp_path):
    vault = tmp_path / "vault"
    far = "FAR/Part 091"
    _write(vault, f"{far}/91.1.md", _note("regulation", "sha256:same", "unchanged"))
    _write(vault, f"{far}/91.3.md", _note("regulation", "sha256:same", "prov"))
    _write(vault, f"{far}/91.5.md", _note("regulation", "sha256:old", "old text"))
    _write(vault, f"{far}/91.7.md", _note("regulation", "sha256:gone", "gone for good"))
    _write(vault, f"{far}/91.9.md", _note("regulation", "sha256:mv", "moving text"))
    _write(vault, f"{far}/Part 91.md", _note("index", "sha256:idx", "index"))
    _write(vault, "FAR/Title 14.md", _note("index", "sha256:title", "title"))
    _write(vault, f"{far}/My Notes.md", b"---\ntype: curated\n---\nmine\n")
    _write(vault, f"{far}/Reserved.md", _note("regulation", "sha256:r", "x", generated=False))

    planned = {
        vault / far / "91.1.md": _note("regulation", "sha256:same", "unchanged"),
        vault / far / "91.3.md": _note(
            "regulation", "sha256:same", "prov", version='source_version: "2026-09-17"\n'
        ),
        vault / far / "91.5.md": _note("regulation", "sha256:new", "new text"),
        vault / far / "91.11.md": _note("regulation", "sha256:mv", "moving text"),
        vault / far / "91.13.md": _note("regulation", "sha256:add", "brand new"),
        vault / far / "Part 91.md": _note("index", "sha256:idx2", "index"),
        vault / "FAR" / "Title 14.md": _note(
            "index", "sha256:t2", "title", version='source_version: "2026-09-17"\n'
        ),
    }
    ledgers = changes.build_ledgers(vault, planned)
    assert set(ledgers) == {FAR}  # AIM/PCG have nothing on disk → no ledger
    ledger = ledgers[FAR]
    assert ledger.published == 5 and ledger.planned == 5
    assert [r.path.name for r in ledger.unchanged] == ["91.1.md"]
    assert [r.path.name for r in ledger.provenance_only] == ["91.3.md"]
    assert [r.path.name for r in ledger.content_changed] == ["91.5.md"]
    assert [r.path.name for r in ledger.added] == ["91.13.md"]
    assert [r.path.name for r in ledger.removed] == ["91.7.md"]
    assert [(m.before.path.name, m.after.path.name) for m in ledger.moved] == [
        ("91.9.md", "91.11.md")
    ]
    assert ledger.edition_before == "2026-09-03" and ledger.edition_after == "2026-09-17"
    assert ledger.edition_changed
    assert ledger.summary() == (
        "content-changed 1, provenance-only 1, added 1, removed 1, moved 1"
    )


def test_move_pairing_is_greedy_and_one_to_one(tmp_path):
    vault = tmp_path / "vault"
    aim = "AIM/Chapter 04"
    a = 'chapter: 4\nsection: 1\nparagraph: "4-1-10"\neffective_date: "2026-07-09"\nchange: 3\n'
    b = a.replace("4-1-10", "4-1-11")
    _write(vault, f"{aim}/4-1-10.md", _note("aim", "sha256:a", "alpha", extra=a, version=""))
    _write(vault, f"{aim}/4-1-11.md", _note("aim", "sha256:b", "alpha", extra=b, version=""))
    _write(vault, "AIM/AIM.md", _note("index", "sha256:i", "i", extra=a, version=""))
    c = a.replace("4-1-10", "4-1-12")
    d = a.replace("4-1-10", "4-1-13")
    planned = {
        vault / aim / "4-1-12.md": _note("aim", "sha256:c", "alpha", extra=c, version=""),
        vault / aim / "4-1-13.md": _note("aim", "sha256:d", "beta", extra=d, version=""),
        vault / "AIM" / "AIM.md": _note("index", "sha256:i", "i", extra=a, version=""),
    }
    ledger = changes.build_ledgers(vault, planned)[AIM]
    # Two identical removed texts, one identical added text: the first
    # removed (citation order) takes the pair, the second stays removed.
    assert [(m.before.path.name, m.after.path.name) for m in ledger.moved] == [
        ("4-1-10.md", "4-1-12.md")
    ]
    assert [r.path.name for r in ledger.removed] == ["4-1-11.md"]
    assert [r.path.name for r in ledger.added] == ["4-1-13.md"]
    assert not ledger.edition_changed


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def test_threshold_trips_above_max_of_floor_and_share():
    t = Threshold(0.01, 25)
    assert t.limit(6544) == pytest.approx(65.44)
    assert t.limit(100) == 25.0  # the floor dominates small corpora
    assert not t.exceeded(65, 6544) and t.exceeded(66, 6544)
    assert not t.exceeded(25, 100) and t.exceeded(26, 100)


def _rec(name: str, kind: str = "regulation", **fm) -> NoteRecord:
    return NoteRecord(
        path=Path("/v") / name,
        frontmatter={"type": kind, "generated": True, "citation": name, **fm},
        official_text=b"x",
    )


def _ledger(corpus: str, published: int, **lists) -> Ledger:
    ledger = Ledger(
        corpus=corpus,
        published=published,
        planned=published,
        index_before=lists.pop("index_before", None),
        index_after=lists.pop("index_after", None),
    )
    for key, value in lists.items():
        setattr(ledger, key, value)
    return ledger


TINY = CorpusThresholds(Threshold(0.5, 1), Threshold(0.5, 1), Threshold(0.5, 1))


def test_mass_change_defects_count_only_unannounced_when_announced_given():
    ledger = _ledger(FAR, 4, content_changed=[_rec("a"), _rec("b"), _rec("c")])
    assert changes.mass_change_defects(ledger, TINY) == [
        "FAR: 3 content-changed notes, above 2 = max(floor 1, 50% of 4)"
    ]
    announced = {Path("/v/a"), Path("/v/b")}
    assert changes.mass_change_defects(ledger, TINY, announced=announced) == []
    ledger.removed = [_rec("r1"), _rec("r2"), _rec("r3")]
    ledger.added = [_rec("n1"), _rec("n2"), _rec("n3")]
    defects = changes.mass_change_defects(ledger, TINY, announced=announced)
    assert [d.split(",")[0] for d in defects] == ["FAR: 3 removed notes", "FAR: 3 added notes"]


def test_content_free_edition_trips_for_aim_and_pcg_but_not_far():
    before = {"effective_date": "2026-07-09", "change": 3}
    after = {"effective_date": "2027-01-01", "change": 4}
    for corpus in (AIM, PCG):
        ledger = _ledger(corpus, 10, index_before=before, index_after=after)
        ledger.provenance_only = [_rec("p")]
        (defect,) = changes.mass_change_defects(ledger, TINY)
        assert "2026-07-09-change-3 → 2027-01-01-change-4 changes no note's content" in defect
        ledger.moved = [Move(_rec("p"), _rec("q"))]
        assert changes.mass_change_defects(ledger, TINY) == []
    far = _ledger(
        FAR, 10, index_before={"source_version": "a"}, index_after={"source_version": "b"}
    )
    assert changes.mass_change_defects(far, TINY) == []


def test_empty_output_is_a_defect_only_for_published_corpora():
    assert changes.empty_output_defect(_ledger(PCG, 0)) is None
    ledger = _ledger(PCG, 5)
    ledger.planned = 0
    assert "never published" in (changes.empty_output_defect(ledger) or "")


# ---------------------------------------------------------------------------
# AIM Explanation of Changes
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def change_3_section() -> dict:
    page = aim_parser.parse_section_page(
        "chap0_section_0.html", (AIM_HTML / "chap0_section_0.html").read_text("utf-8"), {}
    )
    return {"section": page.section, "content": page.content}


def test_change_note_recognized_on_the_change_3_fixture(change_3_section):
    note = changes.read_aim_change_note(change_3_section)
    assert isinstance(note, ChangeNote)
    assert note.effective_date == "2026-07-09"
    assert note.announced == ("4-7-4", "5-1-1", "5-4-5", "5-1-2", "5-5-5", "7-1-6", "10-1-4")
    assert note.categories == ("Editorial Changes", "Entire Publication")
    assert set(note.mentioned) == {
        ("paragraph", "5-4-5"),
        ("paragraph", "5-1-1"),  # written 5–1–1 with en dashes upstream
        ("section", "7-3"),  # "Chapter 7, Section 3"
        ("paragraph", "5-1-17"),
        ("appendix", "4"),  # "TBL 4-16 in Appendix 4" (a two-part table number resolves nowhere)
        ("appendix", "3"),
        ("paragraph", "5-2-9"),
        ("section", "4-3"),  # FIG 4-3-1
    }


def test_change_note_grammar_defects():
    assert "no 'Effective:'" in changes.read_aim_change_note(
        {"section": 0, "content": [{"type": "text", "text": "a. 4-1-1. X"}]}
    )
    assert "no lettered change entries" in changes.read_aim_change_note(
        {"section": 0, "content": [{"type": "text", "text": "Effective: July 9, 2026"}]}
    )
    assert "unparseable effective date" in changes.read_aim_change_note(
        {"section": 0, "content": [{"type": "text", "text": "Effective: Smarch 1, 2026"}]}
    )
    note = changes.read_aim_change_note(
        {
            "section": 0,
            "content": [
                {"type": "heading", "level": 2, "text": "EXPLANATION OF CHANGES"},
                {"type": "text", "text": "Effective: 1/1/27"},
                {"type": "text", "text": "a. Entire Publication"},
                {"type": "text", "text": "Reformatted."},
            ],
        }
    )
    assert isinstance(note, ChangeNote)
    assert note.effective_date == "2027-01-01" and note.announced == ()
    assert note.categories == ("Entire Publication",)


def _aim_rec(paragraph: str, chapter: int = 4, section: int = 1) -> NoteRecord:
    return _rec(paragraph, "aim", chapter=chapter, section=section, paragraph=paragraph)


def _aim_ledger(**lists) -> Ledger:
    return _ledger(
        AIM,
        10,
        index_before={"effective_date": "2026-07-09", "change": 3},
        index_after={"effective_date": "2027-01-01", "change": 4},
        **lists,
    )


def test_cross_check_verdicts():
    note = ChangeNote("2027-01-01", ("4-1-9", "4-1-2"), (("section", "4-3"),), ())
    # A1: stale note.
    stale = ChangeNote("2026-07-09", ("4-1-9",), (), ())
    ledger = _aim_ledger(content_changed=[_aim_rec("4-1-9")])
    (defect,) = changes.cross_check_aim(stale, ledger).defects
    assert "effective 2026-07-09 but the accepted edition is effective 2027-01-01" in defect
    # A2: announced paragraph the layer never had.
    outcome = changes.cross_check_aim(note, _aim_ledger(content_changed=[_aim_rec("4-1-9")]))
    assert any("never had: 4-1-2" in d for d in outcome.defects)
    # A2 is satisfied by a removed or moved-away paragraph.
    outcome = changes.cross_check_aim(
        note,
        _aim_ledger(content_changed=[_aim_rec("4-1-9")], removed=[_aim_rec("4-1-2")]),
    )
    assert outcome.defects == []
    # A3: nothing announced changed.
    outcome = changes.cross_check_aim(
        note, _aim_ledger(unchanged=[_aim_rec("4-1-9"), _aim_rec("4-1-2")])
    )
    assert any("none of the announced paragraphs" in d for d in outcome.defects)
    # A5/A6 report and the explained set that narrows the threshold count.
    outcome = changes.cross_check_aim(
        note,
        _aim_ledger(
            content_changed=[
                _aim_rec("4-1-9"),
                _aim_rec("4-3-5", section=3),
                _aim_rec("4-1-4"),
                _aim_rec("4-2-1", section=2),
            ],
            unchanged=[_aim_rec("4-1-2")],
        ),
    )
    assert outcome.defects == []
    # 4-1-4 shares section 4-1 with the announced 4-1-9, so it is explained
    # (a section note's hash covers its paragraphs); 4-2-1 is not.
    assert outcome.explained == {Path("/v/4-1-9"), Path("/v/4-3-5"), Path("/v/4-1-4")}
    assert outcome.report == [
        "  Explanation of Changes (effective 2027-01-01): 2 announced paragraph(s), "
        "1 mention(s), 0 categories",
        "  announced or mentioned but unchanged: paragraph 4-1-2",
        "  content changes neither announced nor mentioned: 1 (4-2-1)",
    ]


def test_evaluate_runs_the_cross_check_only_on_an_edition_transition():
    ledger = _aim_ledger(content_changed=[_aim_rec("4-1-9")])
    report = changes.evaluate({AIM: ledger}, aim_docs={}, thresholds={AIM: TINY})
    assert report.change_note == [
        "AIM: the parsed layer has no chapter 0 Explanation of Changes to cross-check"
    ]
    ledger.index_after = ledger.index_before
    report = changes.evaluate({AIM: ledger}, aim_docs={}, thresholds={AIM: TINY})
    assert report.change_note == [] and report.mass == [] and report.fatal == []
    assert report.summaries == {
        AIM: "content-changed 1, provenance-only 0, added 0, removed 0, moved 0"
    }
    assert report.lines[0] == (
        "changes (AIM, vs published 2026-07-09-change-3): 10 notes → 10 planned"
    )


def test_evaluate_empty_output_is_fatal_and_skips_the_other_gates():
    ledger = _ledger(PCG, 5)
    ledger.planned = 0
    report = changes.evaluate({PCG: ledger}, aim_docs=None, thresholds={PCG: TINY})
    assert len(report.fatal) == 1 and report.mass == []
