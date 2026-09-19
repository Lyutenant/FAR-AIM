"""Change history: the edition transitions the pipeline has accepted.

``data/changes/history.json`` is the committed, canonical record of what
each accepted edition changed, as the change ledger (:mod:`far_aim.changes`)
classified it at the moment ``far-aim diff --record`` (which ``update``
runs) compared the newly parsed layers with the published vault. The
generated ``What Changed`` note is compiled from it (plan §32.4: Markdown
is never the only representation) and can be re-rendered from a clean
checkout.

One transition per (corpus, before, after); recording the same transition
again replaces it byte-for-byte, so a resumed ``update`` is idempotent. A
transition lists every note whose canonical hash moved (with whether the
source's own change record announced it), the notes added, removed and
moved, the provenance-only count, and a summary of that change record.
Same-version corrections (a parser fix re-rendering an edition already
published) are not transitions and are not recorded. Absent file ⇒ empty
history; a malformed file fails the build (plan §32.13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.changes import CORPORA, CrossCheck, Ledger, NoteRecord

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ChangedNote:
    stem: str
    citation: str
    # True/False when the transition had a change record to check against;
    # None when none was available (thresholds then counted every change).
    announced: bool | None = None


@dataclass(frozen=True)
class MovedNote:
    stem: str
    citation: str
    from_citation: str


@dataclass(frozen=True)
class Transition:
    corpus: str
    before: str
    after: str
    published: int
    planned: int
    unchanged: int
    provenance_only: int
    content_changed: tuple[ChangedNote, ...] = ()
    added: tuple[ChangedNote, ...] = ()
    removed: tuple[ChangedNote, ...] = ()
    moved: tuple[MovedNote, ...] = ()
    announcement: dict[str, object] | None = None
    accepted: tuple[str, ...] = ()  # override flags the operator used

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.corpus, self.before, self.after)

    @property
    def any_content_change(self) -> bool:
        return bool(self.content_changed or self.added or self.removed or self.moved)

    def to_json(self) -> dict[str, object]:
        def note(n: ChangedNote) -> dict[str, object]:
            item: dict[str, object] = {"stem": n.stem, "citation": n.citation}
            if n.announced is not None:
                item["announced"] = n.announced
            return item

        data: dict[str, object] = {
            "corpus": self.corpus,
            "before": self.before,
            "after": self.after,
            "published": self.published,
            "planned": self.planned,
            "unchanged": self.unchanged,
            "provenance_only": self.provenance_only,
            "content_changed": [note(n) for n in self.content_changed],
            "added": [note(n) for n in self.added],
            "removed": [note(n) for n in self.removed],
            "moved": [
                {"stem": m.stem, "citation": m.citation, "from": m.from_citation}
                for m in self.moved
            ],
            "announcement": self.announcement,
        }
        if self.accepted:
            data["accepted"] = list(self.accepted)
        return data


def _sorted_records(records: list[NoteRecord]) -> list[NoteRecord]:
    return sorted(records, key=lambda r: (str(r.path), r.citation))


def transition_from_ledger(
    ledger: Ledger, cross_check: CrossCheck | None, accepted: tuple[str, ...] = ()
) -> Transition:
    """The ledger of one edition transition as a history entry.

    ``cross_check`` is the outcome of the corpus's change-record cross-check
    (None when the transition had no record to check against); it decides
    the per-note ``announced`` flag and the announcement summary.
    """
    if not ledger.edition_changed:
        raise ValueError(f"{ledger.corpus}: not an edition transition")

    def flag(record: NoteRecord) -> bool | None:
        if cross_check is None:
            return None
        return record.path in cross_check.explained

    def changed(records: list[NoteRecord], *, flagged: bool) -> tuple[ChangedNote, ...]:
        return tuple(
            ChangedNote(r.path.stem, r.citation, flag(r) if flagged else None)
            for r in _sorted_records(records)
        )

    return Transition(
        corpus=ledger.corpus,
        before=str(ledger.edition_before),
        after=str(ledger.edition_after),
        published=ledger.published,
        planned=ledger.planned,
        unchanged=len(ledger.unchanged),
        provenance_only=len(ledger.provenance_only),
        content_changed=changed(ledger.content_changed, flagged=True),
        added=changed(ledger.added, flagged=False),
        removed=changed(ledger.removed, flagged=True),
        moved=tuple(
            MovedNote(m.after.path.stem, m.after.citation, m.before.citation)
            for m in sorted(ledger.moved, key=lambda m: (str(m.after.path), m.after.citation))
        ),
        announcement=dict(cross_check.announcement) if cross_check is not None else None,
        accepted=tuple(accepted),
    )


@dataclass
class History:
    transitions: dict[tuple[str, str, str], Transition] = field(default_factory=dict)

    def record(self, transition: Transition) -> None:
        self.transitions[transition.key] = transition

    def ordered(self) -> list[Transition]:
        """Corpus order, then newest edition first (versions sort as strings:
        ISO issue dates and ``{date}-change-{n}`` labels both do)."""
        order = {corpus: i for i, corpus in enumerate(CORPORA)}
        return sorted(
            self.transitions.values(),
            key=lambda t: (order.get(t.corpus, len(order)), t.after, t.before),
            reverse=False,
        )

    def for_corpus(self, corpus: str) -> list[Transition]:
        items = [t for t in self.ordered() if t.corpus == corpus]
        return sorted(items, key=lambda t: (t.after, t.before), reverse=True)

    def to_bytes(self) -> bytes:
        payload = {
            "schema": SCHEMA_VERSION,
            "transitions": [t.to_json() for t in self.ordered()],
        }
        return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def save(self, path: Path) -> bool:
        """Write the file if its bytes would change; True when written."""
        data = self.to_bytes()
        if path.exists() and path.read_bytes() == data:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return True


def _str(item: dict, key: str, where: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: {key!r} must be a non-empty string")
    return value


def _int(item: dict, key: str, where: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where}: {key!r} must be a non-negative integer")
    return value


def _notes(item: dict, key: str, where: str) -> tuple[ChangedNote, ...]:
    values = item.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{where}: {key!r} must be a list")
    notes = []
    for i, entry in enumerate(values):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: {key}[{i}] must be an object")
        announced = entry.get("announced")
        if announced is not None and not isinstance(announced, bool):
            raise ValueError(f"{where}: {key}[{i}].announced must be a boolean")
        notes.append(
            ChangedNote(
                _str(entry, "stem", f"{where}: {key}[{i}]"),
                _str(entry, "citation", f"{where}: {key}[{i}]"),
                announced,
            )
        )
    return tuple(notes)


def _transition(item: object, where: str) -> Transition:
    if not isinstance(item, dict):
        raise ValueError(f"{where}: must be an object")
    corpus = _str(item, "corpus", where)
    if corpus not in CORPORA:
        raise ValueError(f"{where}: unknown corpus {corpus!r}")
    moved_raw = item.get("moved", [])
    if not isinstance(moved_raw, list):
        raise ValueError(f"{where}: 'moved' must be a list")
    moved = []
    for i, entry in enumerate(moved_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: moved[{i}] must be an object")
        moved.append(
            MovedNote(
                _str(entry, "stem", f"{where}: moved[{i}]"),
                _str(entry, "citation", f"{where}: moved[{i}]"),
                _str(entry, "from", f"{where}: moved[{i}]"),
            )
        )
    announcement = item.get("announcement")
    if announcement is not None and not isinstance(announcement, dict):
        raise ValueError(f"{where}: 'announcement' must be an object or null")
    accepted = item.get("accepted", [])
    if not isinstance(accepted, list) or not all(isinstance(a, str) for a in accepted):
        raise ValueError(f"{where}: 'accepted' must be a list of strings")
    return Transition(
        corpus=corpus,
        before=_str(item, "before", where),
        after=_str(item, "after", where),
        published=_int(item, "published", where),
        planned=_int(item, "planned", where),
        unchanged=_int(item, "unchanged", where),
        provenance_only=_int(item, "provenance_only", where),
        content_changed=_notes(item, "content_changed", where),
        added=_notes(item, "added", where),
        removed=_notes(item, "removed", where),
        moved=tuple(moved),
        announcement=announcement,
        accepted=tuple(accepted),
    )


def load_history(path: Path) -> History:
    """The committed history; empty when the file is absent; ``ValueError`` when malformed."""
    history = History()
    if not path.exists():
        return history
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"change history {path} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"change history {path}: expected schema {SCHEMA_VERSION}")
    items = data.get("transitions")
    if not isinstance(items, list):
        raise ValueError(f"change history {path}: 'transitions' must be a list")
    for i, item in enumerate(items):
        transition = _transition(item, f"change history {path}: transitions[{i}]")
        if transition.before == transition.after:
            raise ValueError(f"change history {path}: transitions[{i}] is not a transition")
        if transition.key in history.transitions:
            raise ValueError(
                f"change history {path}: transitions[{i}] duplicates "
                f"{transition.corpus} {transition.before} → {transition.after}"
            )
        history.record(transition)
    return history
