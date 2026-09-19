"""Change history (``far_aim.history``) and the What Changed note."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from far_aim.changes import FAR, CrossCheck, Ledger, Move, NoteRecord
from far_aim.generate import notes
from far_aim.history import (
    ChangedNote,
    History,
    MovedNote,
    Transition,
    load_history,
    transition_from_ledger,
)


def _rec(stem: str, citation: str, folder: str = "FAR/Part 001", **fm) -> NoteRecord:
    frontmatter = {"type": "regulation", "generated": True, "citation": citation, **fm}
    return NoteRecord(Path(folder) / f"{stem}.md", frontmatter, b"text")


def _far_ledger(**lists) -> Ledger:
    return Ledger(
        corpus=FAR,
        published=4,
        planned=4,
        index_before={"source_version": "2026-09-03"},
        index_after={"source_version": "2026-09-15"},
        **lists,
    )


def test_transition_from_ledger_flags_announced_notes_and_sorts_by_path():
    changed_b = _rec("1.3", "14 CFR § 1.3", id="cfr-14-1.3")
    changed_a = _rec("1.2", "14 CFR § 1.2", id="cfr-14-1.2")
    removed = _rec("1.9", "14 CFR § 1.9", id="cfr-14-1.9")
    moved = Move(_rec("3.1", "14 CFR § 3.1"), _rec("3.2", "14 CFR § 3.2", folder="FAR/Part 003"))
    ledger = _far_ledger(
        content_changed=[changed_b, changed_a],
        removed=[removed],
        added=[_rec("1.5", "14 CFR § 1.5")],
        moved=[moved],
        provenance_only=[_rec("1.1", "14 CFR § 1.1")],
    )
    check = CrossCheck(
        explained={changed_a.path, removed.path},
        announcement={"record": "eCFR amendment index", "amended": 2, "removed": 1},
    )
    t = transition_from_ledger(ledger, check, ("--accept-mass-change",))
    assert t.key == (FAR, "2026-09-03", "2026-09-15")
    assert t.content_changed == (
        ChangedNote("1.2", "14 CFR § 1.2", True),
        ChangedNote("1.3", "14 CFR § 1.3", False),
    )
    assert t.removed == (ChangedNote("1.9", "14 CFR § 1.9", True),)
    assert t.added == (ChangedNote("1.5", "14 CFR § 1.5", None),)
    assert t.moved == (MovedNote("3.2", "14 CFR § 3.2", "14 CFR § 3.1"),)
    assert t.provenance_only == 1 and t.accepted == ("--accept-mass-change",)
    # Without a change record every flag is None (thresholds counted everything).
    bare = transition_from_ledger(ledger, None)
    assert {n.announced for n in bare.content_changed} == {None}
    assert bare.announcement is None
    with pytest.raises(ValueError, match="not an edition transition"):
        transition_from_ledger(
            Ledger(FAR, 1, 1, {"source_version": "x"}, {"source_version": "x"}), None
        )


def _transition(**overrides) -> Transition:
    base = dict(
        corpus=FAR,
        before="2026-09-03",
        after="2026-09-15",
        published=3,
        planned=3,
        unchanged=0,
        provenance_only=2,
        content_changed=(ChangedNote("61.14", "14 CFR § 61.14", True),),
        announcement={"record": "eCFR amendment index", "since": "2026-09-03",
                      "until": "2026-09-15", "amended": 1, "removed": 0, "unchanged": []},
    )
    base.update(overrides)
    return Transition(**base)


def test_history_round_trips_and_save_is_idempotent(tmp_path):
    path = tmp_path / "changes" / "history.json"
    assert load_history(path).transitions == {}  # absent ⇒ empty
    history = History()
    history.record(_transition())
    history.record(_transition(corpus="AIM", before="2026-07-09-change-3",
                               after="2027-01-01-change-4", announcement=None,
                               content_changed=(ChangedNote("4-1-9", "AIM 4-1-9"),)))
    assert history.save(path) is True
    assert history.save(path) is False  # byte-identical ⇒ not rewritten
    loaded = load_history(path)
    assert loaded.transitions == history.transitions
    assert [t.corpus for t in loaded.ordered()] == [FAR, "AIM"]
    data = json.loads(path.read_bytes())
    assert data["schema"] == 1
    assert data["transitions"][0]["content_changed"] == [
        {"stem": "61.14", "citation": "14 CFR § 61.14", "announced": True}
    ]
    assert "announced" not in data["transitions"][1]["content_changed"][0]
    assert "accepted" not in data["transitions"][0]
    # Re-recording the same transition replaces it.
    history.record(_transition(provenance_only=5))
    assert len(history.transitions) == 2
    assert history.for_corpus(FAR)[0].provenance_only == 5


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(schema=2), "expected schema 1"),
        (lambda d: d["transitions"][0].update(corpus="NOTAM"), "unknown corpus"),
        (lambda d: d["transitions"][0].update(after=d["transitions"][0]["before"]),
         "not a transition"),
        (lambda d: d["transitions"].append(dict(d["transitions"][0])), "duplicates"),
        (lambda d: d["transitions"][0]["content_changed"][0].update(announced="yes"),
         "announced must be a boolean"),
        (lambda d: d["transitions"][0].update(published=-1), "non-negative integer"),
    ],
)
def test_malformed_history_fails(tmp_path, mutate, message):
    path = tmp_path / "history.json"
    history = History()
    history.record(_transition())
    data = json.loads(history.to_bytes())
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_history(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        load_history(path)


def test_what_changed_note_links_only_notes_that_still_exist():
    history = History()
    history.record(
        _transition(
            content_changed=(
                ChangedNote("61.14", "14 CFR § 61.14", True),
                ChangedNote("91.1", "14 CFR § 91.1", False),
                ChangedNote("Part 61 Appendix A", "14 CFR Part 61, Appendix A", True),
            ),
            removed=(ChangedNote("1.9", "14 CFR § 1.9", True),),
            added=(ChangedNote("1.5", "14 CFR § 1.5"),),
            moved=(MovedNote("3.2", "14 CFR § 3.2", "14 CFR § 3.1"),),
            accepted=("--accept-mass-change",),
            announcement={"record": "eCFR amendment index", "since": "2026-09-03",
                          "until": "2026-09-15", "amended": 3, "removed": 1,
                          "unchanged": ["14 CFR § 1.1"]},
        )
    )
    history.record(_transition(before="2026-08-24", after="2026-09-03", content_changed=(),
                               announcement=None))
    history.record(_transition(corpus="AIM", before="2026-07-09-change-3",
                               after="2027-01-01-change-4",
                               content_changed=(ChangedNote("4-1-9", "AIM 4-1-9", True),),
                               announcement={"record": "AIM Explanation of Changes",
                                             "effective_date": "2027-01-01", "announced": 1,
                                             "mentioned": 0, "unchanged": []}))
    stems = {"61.14", "91.1", "Part 61 Appendix A", "3.2", "4-1-9", "Source Status"}
    note = notes.build_what_changed(history, stems)
    assert note.path_parts == ("What Changed.md",)
    assert note.frontmatter[1] == ("type", "changelog")
    body = note.body
    # Corpus order, newest transition first within a corpus.
    assert body.index("## FAR") < body.index("### 2026-09-03 → 2026-09-15")
    assert body.index("### 2026-09-03 → 2026-09-15") < body.index("### 2026-08-24 → 2026-09-03")
    assert body.index("## AIM") > body.index("### 2026-08-24 → 2026-09-03")
    assert (
        "- [[61.14|14 CFR § 61.14]] — announced · [compare on eCFR]"
        "(https://www.ecfr.gov/compare/2026-09-15/to/2026-09-03/title-14/section-61.14)"
    ) in body
    assert "- [[91.1|14 CFR § 91.1]] — **not announced** · [compare on eCFR]" in body
    assert "- [[Part 61 Appendix A|14 CFR Part 61, Appendix A]] — announced\n" in body  # no compare
    assert "- 14 CFR § 1.9 — announced\n" in body  # removed: never a link
    assert "- 14 CFR § 1.5\n" in body  # added but since gone: plain text
    assert "- [[3.2|14 CFR § 3.2]] — was 14 CFR § 3.1" in body
    assert "announced but unchanged: 14 CFR § 1.1." in body
    assert "Published over a change gate with `--accept-mass-change`" in body
    assert "No change record was available for this transition" in body
    assert "No note's official text changed." in body
    assert "[2026-09-15](https://www.ecfr.gov/on/2026-09-15/title-14)" in body
    assert "Edition 2027-01-01-change-4:" in body
    assert "The Explanation of Changes (effective 2027-01-01) announces 1 paragraph(s)" in body
    assert "- [[4-1-9|AIM 4-1-9]] — announced\n" in body
    empty = notes.build_what_changed(History(), stems).body
    assert "No edition transition has been recorded yet." in empty
