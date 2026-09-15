"""Curated exam-prep data (plan §39.3–§39.4): the ACS element map and the
Private Pilot study guide, with the build-time gates that keep them honest.

Two committed files under ``data/enrichment/`` feed everything in Phase 10b:

- ``acs-map.json`` (§39.3) — where the vault covers each ACS Knowledge and
  Risk element, or why it cannot (``out_of_corpus``). Entries are keyed by
  element code; a sub-element inherits its parent's entry and any element
  inherits its Task's entry, so a maneuver Task whose knowledge lives in
  the handbooks is one line. **Every** Knowledge and Risk element must
  resolve to an entry with at least one stem or a reason — the map can be
  incomplete only loudly, which is what makes the coverage report true.
- ``ppl-study.json`` (§39.4) — one entry per covered FAR section or AIM
  paragraph: a gist, why it matters, key numbers, traps, plain-English
  questions, ACS codes and a training stage. The curator's own words,
  rendered as labelled study aids, never as official text (§32.1, §32.3).
  The **verbatim-numbers gate** is the part the pipeline can verify: every
  ``numbers[].quote`` must occur verbatim in the official text of the note
  it cites, inside the paragraph ``where`` names when given.

Both loaders fail loudly on any malformed field (§32.13); a missing file
simply means the feature is absent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.generate import BuildError
from far_aim.models import acs as acs_model

ACS_MAP_SOURCE = "data/enrichment/acs-map.json"
STUDY_SOURCE = "data/enrichment/ppl-study.json"
REVIEW_STATES = ("unreviewed", "reviewed")
_WS_RE = re.compile(r"\s+")
_LABEL_PATH_RE = re.compile(r"^(\([A-Za-z0-9]+\))+$")
_LABEL_RE = re.compile(r"\([A-Za-z0-9]+\)")
_TASK_CODE_RE = re.compile(r"^(?P<acs>[A-Z]{2,3})\.(?P<area>[IVX]+)\.(?P<task>[A-Z])$")


def task_area(code: str) -> str:
    """``PA.I.A`` → ``I``; raises on anything that is not a Task code."""
    match = _TASK_CODE_RE.match(code)
    _require(match is not None, f"not an ACS Task code: {code!r}")
    assert match is not None
    return match.group("area")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def _string_list(value: object, what: str) -> tuple[str, ...]:
    _require(isinstance(value, list), f"{what}: expected a list of strings")
    assert isinstance(value, list)
    for item in value:
        _require(
            isinstance(item, str) and item.strip() == item and item, f"{what}: bad item {item!r}"
        )
    _require(len(set(value)) == len(value), f"{what}: duplicate items")
    return tuple(value)


# ---------------------------------------------------------------------------
# ACS element map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MapEntry:
    far: tuple[str, ...] = ()
    aim: tuple[str, ...] = ()
    pcg: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()
    out_of_corpus: str | None = None

    @property
    def stems(self) -> tuple[str, ...]:
        return (*self.far, *self.aim, *self.pcg, *self.concepts)

    @classmethod
    def parse(cls, raw: object, what: str) -> MapEntry:
        _require(isinstance(raw, dict), f"{what}: expected an object")
        assert isinstance(raw, dict)
        allowed = {"far", "aim", "pcg", "concepts", "out_of_corpus", "_comment"}
        unknown = sorted(set(raw) - allowed)
        _require(not unknown, f"{what}: unknown field(s) {unknown}")
        reason = raw.get("out_of_corpus")
        if reason is not None:
            _require(
                isinstance(reason, str) and reason.strip(),
                f"{what}: out_of_corpus must be a reason",
            )
        entry = cls(
            far=_string_list(raw.get("far", []), f"{what}.far"),
            aim=_string_list(raw.get("aim", []), f"{what}.aim"),
            pcg=_string_list(raw.get("pcg", []), f"{what}.pcg"),
            concepts=_string_list(raw.get("concepts", []), f"{what}.concepts"),
            out_of_corpus=reason,
        )
        _require(
            bool(entry.stems) != (reason is not None),
            f"{what}: give either stems or an out_of_corpus reason, not both and not neither",
        )
        return entry


@dataclass(frozen=True)
class Coverage:
    """How the map covers one Area's Knowledge and Risk elements."""

    roman: str
    title: str
    elements: int
    mapped: int
    out_of_corpus: int


@dataclass(frozen=True)
class AcsMap:
    tasks: dict[str, MapEntry]
    elements: dict[str, MapEntry]

    @classmethod
    def load(cls, path: Path) -> AcsMap:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BuildError(f"cannot read {path}: {exc}") from exc
        return cls.from_data(data, where=str(path))

    @classmethod
    def from_data(cls, data: object, *, where: str) -> AcsMap:
        _require(isinstance(data, dict), f"{where}: expected a JSON object")
        assert isinstance(data, dict)
        _require(data.get("schema") == 1, f"{where}: unsupported schema {data.get('schema')!r}")
        unknown = sorted(set(data) - {"schema", "_comment", "tasks", "elements"})
        _require(not unknown, f"{where}: unknown top-level field(s) {unknown}")
        tasks: dict[str, MapEntry] = {}
        for code, raw in (data.get("tasks") or {}).items():
            _require(
                isinstance(code, str)
                and re.fullmatch(r"[A-Z]{2,3}\.[IVX]+\.[A-Z]", code) is not None,
                f"{where}: {code!r} is not a task code",
            )
            tasks[code] = MapEntry.parse(raw, f"{where}: tasks[{code}]")
        elements: dict[str, MapEntry] = {}
        for code, raw in (data.get("elements") or {}).items():
            _require(
                isinstance(code, str) and acs_model.ELEMENT_CODE_RE.match(code) is not None,
                f"{where}: {code!r} is not an element code",
            )
            elements[code] = MapEntry.parse(raw, f"{where}: elements[{code}]")
        return cls(tasks=tasks, elements=elements)

    def entry_for(self, code: str, parent: str | None, task: str) -> MapEntry | None:
        """The entry an element resolves to: its own, its parent's, then its Task's."""
        if code in self.elements:
            return self.elements[code]
        if parent is not None and parent in self.elements:
            return self.elements[parent]
        return self.tasks.get(task)

    def verify(self, acs_docs: dict[str, dict], stem_exists) -> list[Coverage]:
        """Fail on unknown codes, unresolved stems or an uncovered Knowledge/Risk
        element; return per-Area coverage for the report (plan §39.6)."""
        known_tasks: set[str] = set()
        known_elements: set[str] = set()
        coverage: list[Coverage] = []
        areas = sorted(
            (
                d
                for d in acs_docs.values()
                if d.get("document_type") == acs_model.DOCUMENT_TYPE_AREA
            ),
            key=lambda d: d["number"],
        )
        for area in areas:
            counted = mapped = outside = 0
            for task in area["tasks"]:
                known_tasks.add(task["code"])
                for kind in ("knowledge", "risk", "skills"):
                    for element in task[kind]["elements"]:
                        known_elements.add(element["code"])
                        if kind == "skills":
                            continue
                        counted += 1
                        entry = self.entry_for(element["code"], element.get("parent"), task["code"])
                        if entry is None:
                            raise BuildError(
                                f"{ACS_MAP_SOURCE}: {element['code']} ({task['code']} — "
                                f"{task['title']}) is neither mapped nor marked out_of_corpus"
                            )
                        if entry.out_of_corpus:
                            outside += 1
                        else:
                            mapped += 1
            coverage.append(
                Coverage(
                    roman=area["roman"],
                    title=area["title"],
                    elements=counted,
                    mapped=mapped,
                    out_of_corpus=outside,
                )
            )
        for code in self.tasks:
            _require(
                code in known_tasks,
                f"{ACS_MAP_SOURCE}: tasks[{code}] names a Task the ACS does not have",
            )
        for code in self.elements:
            _require(
                code in known_elements,
                f"{ACS_MAP_SOURCE}: elements[{code}] names an element the ACS does not have",
            )
        for owner, entries in (("tasks", self.tasks), ("elements", self.elements)):
            for code, entry in entries.items():
                for kind in ("far", "aim", "pcg", "concepts"):
                    for stem in getattr(entry, kind):
                        _require(
                            stem_exists(kind, stem),
                            f"{ACS_MAP_SOURCE}: {owner}[{code}].{kind} names {stem!r}, which no "
                            "generated note defines",
                        )
        return coverage


# ---------------------------------------------------------------------------
# Study guide
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Number:
    value: str
    quote: str
    where: str | None = None


@dataclass(frozen=True)
class Question:
    q: str
    a: str
    cite: tuple[str, ...]


@dataclass(frozen=True)
class OralScenario:
    """One checkride-style scenario under an ACS Task (plan §39.4.4): the
    curator's question and short answer, the notes that settle it, and a hint
    for finding the rule without the vault."""

    scenario: str
    answer: str
    cite: tuple[str, ...]
    find_it: str | None = None


@dataclass(frozen=True)
class StudyEntry:
    stem: str
    gist: str
    why: str
    numbers: tuple[Number, ...]
    traps: tuple[str, ...]
    questions: tuple[Question, ...]
    mnemonics: tuple[str, ...]
    acs: tuple[str, ...]
    stage: str
    review: str

    @property
    def is_aim(self) -> bool:
        return re.fullmatch(r"\d{1,2}-\d{1,2}-\d{1,3}", self.stem) is not None


def _text(raw: object, what: str, *, allow_empty: bool = False) -> str:
    _require(isinstance(raw, str), f"{what}: expected text")
    assert isinstance(raw, str)
    _require(raw == raw.strip() and (allow_empty or raw), f"{what}: empty or untrimmed text")
    return raw


@dataclass(frozen=True)
class StudyGuide:
    stages: dict[str, str] = field(default_factory=dict)  # ordered: stage → description
    entries: dict[str, StudyEntry] = field(default_factory=dict)
    # ACS Task code → scenarios, in file order.
    oral: dict[str, tuple[OralScenario, ...]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> StudyGuide:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BuildError(f"cannot read {path}: {exc}") from exc
        return cls.from_data(data, where=str(path))

    @classmethod
    def from_data(cls, data: object, *, where: str) -> StudyGuide:
        _require(isinstance(data, dict), f"{where}: expected a JSON object")
        assert isinstance(data, dict)
        _require(data.get("schema") == 1, f"{where}: unsupported schema {data.get('schema')!r}")
        unknown = sorted(set(data) - {"schema", "_comment", "stages", "entries", "oral"})
        _require(not unknown, f"{where}: unknown top-level field(s) {unknown}")
        stages_raw = data.get("stages")
        _require(
            isinstance(stages_raw, dict) and stages_raw,
            f"{where}: stages must be a non-empty object",
        )
        assert isinstance(stages_raw, dict)
        stages = {
            _text(k, f"{where}: stage name"): _text(v, f"{where}: stages[{k}]")
            for k, v in stages_raw.items()
        }
        entries: dict[str, StudyEntry] = {}
        raw_entries = data.get("entries")
        _require(isinstance(raw_entries, dict), f"{where}: entries must be an object")
        assert isinstance(raw_entries, dict)
        for stem, raw in raw_entries.items():
            what = f"{where}: entries[{stem}]"
            _require(isinstance(raw, dict), f"{what}: expected an object")
            allowed = {
                "gist",
                "why",
                "numbers",
                "traps",
                "questions",
                "mnemonics",
                "acs",
                "stage",
                "review",
                "_comment",
            }
            unknown = sorted(set(raw) - allowed)
            _require(not unknown, f"{what}: unknown field(s) {unknown}")
            numbers: list[Number] = []
            for index, item in enumerate(raw.get("numbers", [])):
                n_what = f"{what}.numbers[{index}]"
                _require(
                    isinstance(item, dict) and set(item) <= {"value", "quote", "where"},
                    f"{n_what}: bad shape",
                )
                where_label = item.get("where")
                if where_label is not None:
                    _require(
                        isinstance(where_label, str)
                        and _LABEL_PATH_RE.match(where_label) is not None,
                        f"{n_what}: where must be a paragraph label path like (a)(1)",
                    )
                numbers.append(
                    Number(
                        value=_text(item.get("value"), f"{n_what}.value"),
                        quote=_text(item.get("quote"), f"{n_what}.quote"),
                        where=where_label,
                    )
                )
            questions: list[Question] = []
            for index, item in enumerate(raw.get("questions", [])):
                q_what = f"{what}.questions[{index}]"
                _require(
                    isinstance(item, dict) and set(item) <= {"q", "a", "cite"},
                    f"{q_what}: bad shape",
                )
                questions.append(
                    Question(
                        q=_text(item.get("q"), f"{q_what}.q"),
                        a=_text(item.get("a"), f"{q_what}.a"),
                        cite=_string_list(item.get("cite", [stem]), f"{q_what}.cite"),
                    )
                )
            _require(bool(questions), f"{what}: at least one question is required")
            stage = _text(raw.get("stage"), f"{what}.stage")
            _require(stage in stages, f"{what}: stage {stage!r} is not one of {sorted(stages)}")
            review = raw.get("review", "unreviewed")
            _require(review in REVIEW_STATES, f"{what}: review must be one of {REVIEW_STATES}")
            entries[stem] = StudyEntry(
                stem=_text(stem, f"{where}: entry stem"),
                gist=_text(raw.get("gist"), f"{what}.gist"),
                why=_text(raw.get("why"), f"{what}.why"),
                numbers=tuple(numbers),
                traps=_string_list(raw.get("traps", []), f"{what}.traps"),
                questions=tuple(questions),
                mnemonics=_string_list(raw.get("mnemonics", []), f"{what}.mnemonics"),
                acs=_string_list(raw.get("acs", []), f"{what}.acs"),
                stage=stage,
                review=review,
            )
        oral: dict[str, tuple[OralScenario, ...]] = {}
        raw_oral = data.get("oral", {})
        _require(isinstance(raw_oral, dict), f"{where}: oral must be an object keyed by Task code")
        assert isinstance(raw_oral, dict)
        for code, items in raw_oral.items():
            o_what = f"{where}: oral[{code}]"
            task_area(code)
            _require(isinstance(items, list) and items, f"{o_what}: expected a non-empty list")
            scenarios: list[OralScenario] = []
            for index, item in enumerate(items):
                s_what = f"{o_what}[{index}]"
                _require(
                    isinstance(item, dict)
                    and set(item) <= {"scenario", "answer", "cite", "find_it"},
                    f"{s_what}: bad shape",
                )
                find_it = item.get("find_it")
                scenarios.append(
                    OralScenario(
                        scenario=_text(item.get("scenario"), f"{s_what}.scenario"),
                        answer=_text(item.get("answer"), f"{s_what}.answer"),
                        cite=_string_list(item.get("cite", []), f"{s_what}.cite"),
                        find_it=None if find_it is None else _text(find_it, f"{s_what}.find_it"),
                    )
                )
            oral[code] = tuple(scenarios)
        return cls(stages=stages, entries=entries, oral=oral)

    def oral_areas(self) -> list[str]:
        """The Areas (roman numerals) that have at least one scenario, in ACS order."""
        romans = {task_area(code) for code in self.oral}
        return sorted(romans, key=acs_model.roman_to_int)

    def verify(
        self,
        *,
        stem_exists,
        official_text,
        acs_codes: Iterable[str] | None,
    ) -> None:
        """Every stem, citation and code must resolve, and every quote must be verbatim.

        ``official_text(stem, where)`` returns the official text of the cited
        note (or the paragraph ``where`` names), or None when the stem is not
        a FAR section or AIM paragraph the layers hold. ``acs_codes`` is None
        when no ACS layer is built, in which case naming codes is an error.
        """
        codes = None if acs_codes is None else set(acs_codes)
        for stem, entry in self.entries.items():
            what = f"{STUDY_SOURCE}: entries[{stem}]"
            for citation in {stem, *(c for q in entry.questions for c in q.cite)}:
                _require(
                    stem_exists("far", citation) or stem_exists("aim", citation),
                    f"{what}: {citation!r} is not a FAR section or AIM paragraph the vault holds",
                )
            for code in entry.acs:
                _require(
                    codes is not None and code in codes,
                    f"{what}: ACS code {code!r} is not in the accepted ACS"
                    + (" (no ACS layer is built)" if codes is None else ""),
                )
            for number in entry.numbers:
                text = official_text(stem, number.where)
                _require(
                    text is not None,
                    f"{what}: cannot read the official text"
                    + (f" of paragraph {number.where}" if number.where else ""),
                )
                assert text is not None
                place = f"paragraph {number.where} of {stem}" if number.where else stem
                _require(
                    normalize(number.quote) in normalize(text),
                    f"{what}: the quote {number.quote!r} does not occur verbatim in the "
                    f"official text of {place}",
                )
        tasks = None if codes is None else {c.rsplit(".", 1)[0] for c in codes}
        for code, scenarios in self.oral.items():
            what = f"{STUDY_SOURCE}: oral[{code}]"
            _require(
                tasks is not None and code in tasks,
                f"{what}: Task {code!r} is not in the accepted ACS"
                + (" (no ACS layer is built)" if tasks is None else ""),
            )
            for index, scenario in enumerate(scenarios):
                for citation in scenario.cite:
                    _require(
                        stem_exists("far", citation) or stem_exists("aim", citation),
                        f"{what}[{index}]: {citation!r} is not a FAR section or AIM paragraph "
                        "the vault holds",
                    )

    def by_stage(self) -> dict[str, list[StudyEntry]]:
        out: dict[str, list[StudyEntry]] = {stage: [] for stage in self.stages}
        for entry in self.entries.values():
            out[entry.stage].append(entry)
        return out


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def far_paragraph_text(section_doc: dict, where: str | None, collect) -> str | None:
    """Text of a FAR section, or of the paragraph a label path names.

    ``where`` is ``(a)`` / ``(b)(1)`` … — the labels the canonical model
    carries on nested ``paragraph`` blocks; ``collect`` is the FAR text
    walker (``links.citations.collect_text``).
    """
    blocks = section_doc["content"]
    if where:
        for label in _LABEL_RE.findall(where):
            match = next(
                (b for b in blocks if b.get("type") == "paragraph" and b.get("label") == label),
                None,
            )
            if match is None:
                return None
            blocks = match.get("children", [])
            target = match
        return " ".join(collect([target]))
    return " ".join(collect(blocks))
