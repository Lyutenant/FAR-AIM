"""CFR defined-term recognition for FAR text (plan §12.2, Tier 2; §37).

Title 14 defines its vocabulary in *definitions sections*: Part 1 for the
whole of Chapter I (§ 1.1 opens "As used in this chapter, unless the
context requires otherwise", § 1.2 "In this chapter"), and a part's or
subpart's own section for narrower scopes ("For the purpose of this part"
— § 61.1; "As used in this subpart" — § 91.851; "For the purpose of this
subchapter" — § 110.2). A FAR note whose official text uses one of the
terms defined *for it* may list the term, linked to the definition itself
— the FAR-side analogue of the AIM's ``## Glossary Terms``
(``links.glossary``), and the reason FAR notes carry no PCG links (the two
vocabularies differ). This is a lexical relationship, not an FAA citation,
so it renders in its own ``## Defined Terms`` section, never among the
Tier 1 ``## Explicit Cross-References``.

Deterministic rules — no dictionary, nothing that depends on the host:

- **Sources and scope.** A section that holds ``definition`` blocks is a
  source when its heading says so (``Definitions.``, ``Applicability and
  definitions.``) or its lead-in states a scope. The scope is what the
  lead-in says — the narrowest of ``this chapter`` / ``this subchapter``
  / ``this part`` / ``this subpart`` in the text that introduces the
  first definition (the paragraph holding it, else the text before it);
  a source scoped to ``this section`` defines terms for itself only and
  is skipped. Without a phrase, a section in subpart A or in no subpart
  speaks for its part, any other for its subpart (§ 5.3, § 121.7).
- **Applicability.** A document lists terms from every source whose
  scope contains it, except itself. Chapters II and III have no Part 1;
  their parts' own sections apply the same way.
- **Precedence.** At an alias two sources define, the narrower scope
  wins — § 139.5's *Airport* over § 1.1's in Part 139, exactly § 1.1's
  "unless the context requires otherwise". Within one scope, an alias
  goes to the definition whose label *is* the alias (``RNAV`` is
  § 1.2's, not the parenthetical of § 1.1's *Area navigation (RNAV)*),
  else it is dropped as ambiguous. The longest alias wins at a position
  (``IFR conditions`` is one term, not also ``IFR``).
- **Targets are block links.** Every ``definition`` block the FAR
  renderer emits carries an Obsidian block id (``^def-night``) derived
  from its term, so a link lands on the definition, not on a 195-item
  note: ``[[1.1#^def-night|Night]]``. Ids are unique within a note; two
  definitions with the same term text (§ 1.1 lists *Synthetic vision*
  twice because the eCFR italicizes only the head of *Synthetic vision
  system*) are told apart by the lowercase words that precede the
  definition's verb (``system means …``), and an unresolved duplicate is
  numbered and never linked.
- **Labels.** A term's trailing punctuation (``Approved,``, ``Type:``,
  ``Fireproof—``) is display noise; the label is the term without it.
- **Aliases.** A plain label is its own alias. A label with a
  parenthetical yields the label without it (``Decision altitude``,
  ``Air Traffic Service route``) and, in the CFR's ``Term (ABBR)``
  convention, the parenthetical standing for the words before it
  (``DA``, ``ATS route``, ``NDB``/``ADF``). An alias without lowercase
  letters is an **abbreviation**: it matches only in capitals and only
  at three or more characters (``IFR``, ``TCAS II``; ``DA``, ``V1`` are
  too short). Any other alias is a **phrase** and matches
  case-insensitively at word boundaries, singular or plural — § 1.3(a)
  provides that "words importing the singular include the plural" —
  so ``airports``, ``persons`` and ``categories`` resolve too. Single
  defined words *do* match (unlike PCG entries): a definitions section
  is its scope's controlled vocabulary, so ``night``, ``aircraft`` and
  ``operate`` mean what it says unless the gate says otherwise.
- **The gate** (``data/links/part1-definitions-gate.json``,
  human-maintained, plan §12.3) denies terms whose ordinary regulatory
  usage outruns their definition (``Instrument`` is mostly "instrument
  rating"; ``Type:``/``Class:``/``Category:`` also name airspace and
  operation categories; ``FAA`` is on every page). An entry names the
  verbatim term text and, optionally, the one source section it applies
  to (else every source defining that text). Every entry carries a
  reason and must name a term some source defines; a stale entry fails
  the build.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.generate import BuildError
from far_aim.links.glossary import _BOUNDARY_AFTER, _BOUNDARY_BEFORE, fold

SCOPE_CHAPTER, SCOPE_SUBCHAPTER, SCOPE_PART, SCOPE_SUBPART = range(4)
"""Scope levels, widest first; a narrower level outranks a wider one."""

_SCOPE_LEVELS = {
    "chapter": SCOPE_CHAPTER,
    "subchapter": SCOPE_SUBCHAPTER,
    "part": SCOPE_PART,
    "subpart": SCOPE_SUBPART,
}
_SCOPE_PHRASE_RE = re.compile(r"\bthis (section|subpart|part|subchapter|chapter)\b", re.I)
_DEFINITIONS_HEADING_RE = re.compile(r"definition", re.I)

GATE_FILENAME = "part1-definitions-gate.json"
BLOCK_ID_PREFIX = "def-"
MIN_ABBREVIATION_LEN = 3
SECTION_HEADING = "## Defined Terms"

_TRAILING_PUNCT_RE = re.compile(r"[\s,.:;\-]+$")
_PAREN_RE = re.compile(r"^(?P<pre>.*?)\s*\((?P<inner>[^()]+)\)(?P<post>.*)$")
_ABBREVIATION_RE = re.compile(r"^[A-Z0-9][A-Z0-9/\- ]*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SPACE_RE = re.compile(r"\s+")
# Lowercase words a duplicated term's text opens with, before its verb: the
# rest of a head the eCFR only partly italicized ("system means …").
_VERBS = r"(?:means|is|are|has|includes)\b"
_LEAD_WORDS_RE = re.compile(rf"^((?:(?!{_VERBS})[a-z][a-z/\-]*\s+){{1,4}}){_VERBS}")


def definition_label(term: str) -> str:
    """Display label of a definition: typography folded, trailing punctuation dropped."""
    return _TRAILING_PUNCT_RE.sub("", fold(term)).strip()


def iter_definitions(blocks: Iterable[dict]) -> Iterator[dict]:
    """Every ``definition`` block of a content tree, in document (pre-)order."""
    for block in blocks:
        if block.get("type") == "definition":
            yield block
        for key in ("children", "blocks"):
            nested = block.get(key)
            if nested:
                yield from iter_definitions(nested)


def definition_labels(defs: list[dict]) -> list[str]:
    """Labels for definition blocks, duplicates told apart by their leading words.

    Only a label that collides is extended, so the ordinary case is exactly
    :func:`definition_label`; a collision the text cannot resolve stays a
    duplicate (the caller numbers its block id and links neither).
    """
    labels = [definition_label(block["term"]) for block in defs]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label.casefold()] = counts.get(label.casefold(), 0) + 1
    out: list[str] = []
    for block, label in zip(defs, labels, strict=True):
        if counts[label.casefold()] > 1:
            m = _LEAD_WORDS_RE.match(fold(block.get("text") or ""))
            if m is not None:
                label = f"{label} {m.group(1).strip()}"
        out.append(label)
    return out


def slugify(label: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    return _SLUG_RE.sub("-", ascii_.lower()).strip("-")


def block_ids(content: list[dict]) -> dict[int, str]:
    """Obsidian block id for every definition block, keyed by ``id(block)``.

    Unique within the document: a repeated slug is numbered (``-2``) so the
    note never carries two identical ids. Keyed by object identity because
    canonical blocks are never mutated (plan §32.4).
    """
    defs = list(iter_definitions(content))
    ids: dict[int, str] = {}
    used: dict[str, int] = {}
    for block, label in zip(defs, definition_labels(defs), strict=True):
        slug = BLOCK_ID_PREFIX + (slugify(label) or "term")
        n = used.get(slug, 0) + 1
        used[slug] = n
        ids[id(block)] = slug if n == 1 else f"{slug}-{n}"
    return ids


def lead_in_text(blocks: list[dict]) -> str:
    """The text that introduces the first definition of a content tree.

    The paragraph that directly holds the first definition supplies its
    text (§ 61.1(b) "For the purpose of this part:"), plus any text of its
    earlier children; a definition at the top level is introduced by the
    text blocks before it (§ 1.1 "As used in this chapter, …"). A
    definition inside an extract, note or footnote has no lead-in.
    """
    texts: list[str] = []
    for block in blocks:
        if block.get("type") == "definition":
            break
        children = block.get("children") or []
        if any(True for _ in iter_definitions(children)):
            return " ".join([block.get("text") or "", lead_in_text(children)]).strip()
        if any(True for _ in iter_definitions(block.get("blocks") or [])):
            return ""
        if block.get("type") in ("text", "paragraph") and block.get("text"):
            texts.append(block["text"])
    return " ".join(texts)


@dataclass(frozen=True)
class Source:
    """A definitions section and the scope its terms are defined for."""

    section: dict
    level: int

    @property
    def number(self) -> str:
        return self.section["section"]

    def covers(self, scope: tuple[str | None, str | None, str, str | None]) -> bool:
        """Whether a document with ``(chapter, subchapter, part, subpart)`` is in scope."""
        chapter, subchapter, part, subpart = scope
        sec = self.section
        if self.level == SCOPE_CHAPTER:
            return chapter == sec.get("chapter")
        if self.level == SCOPE_SUBCHAPTER:
            return chapter == sec.get("chapter") and subchapter == sec.get("subchapter")
        if self.level == SCOPE_PART:
            return part == sec["part"]
        return part == sec["part"] and subpart == sec.get("subpart")


def scope_level(section: dict) -> int | None:
    """The scope a definitions section's own words give it, or None to skip it.

    The narrowest ``this …`` phrase of the lead-in decides; ``this
    section`` means the terms serve that section alone (nothing to link).
    Without a phrase, a section whose heading names definitions speaks for
    its part when it sits in subpart A or in no subpart, else for its
    subpart; a section with neither phrase nor heading is not a source.
    """
    lead_in = lead_in_text(section["content"])
    phrases = {m.group(1).lower() for m in _SCOPE_PHRASE_RE.finditer(lead_in)}
    if "section" in phrases:
        return None
    if phrases:
        return max(_SCOPE_LEVELS[p] for p in phrases)
    if not _DEFINITIONS_HEADING_RE.search(section.get("heading") or ""):
        return None
    if section.get("subpart") in (None, "A"):
        return SCOPE_PART
    return SCOPE_SUBPART


def document_scope(doc: dict, part_doc: dict) -> tuple[str | None, str | None, str, str | None]:
    """``(chapter, subchapter, part, subpart)`` of a section or appendix; an
    appendix carries no chapter of its own and takes its part's."""
    return (
        doc.get("chapter") or part_doc.get("chapter"),
        doc.get("subchapter") or part_doc.get("subchapter"),
        doc["part"],
        doc.get("subpart"),
    )


def discover_sources(docs: dict[str, dict]) -> list[Source]:
    """Every definitions source of the canonical layer, in part/section order."""
    found: list[Source] = []

    def walk(children: list[dict]) -> None:
        for child in children:
            if child.get("document_type") == "cfr_section":
                if any(True for _ in iter_definitions(child.get("content") or [])):
                    level = scope_level(child)
                    if level is not None:
                        found.append(Source(child, level))
            else:
                # Subparts nest documents under ``children``, subject groups
                # under ``sections`` (``models.cfr``).
                walk(child.get("children") or child.get("sections") or [])

    for _, part in sorted(docs.items(), key=lambda item: _part_sort_key(item[0])):
        walk(part.get("children") or [])
    return found


def _part_sort_key(part: str) -> tuple[int, str]:
    digits = "".join(ch for ch in part if ch.isdigit())
    return (int(digits) if digits else 0, part)


@dataclass(frozen=True)
class DefinitionsGate:
    """The committed deny decisions: ``(verbatim term text, source section or
    None for every source)`` → reason."""

    deny: dict[tuple[str, str | None], str] = field(default_factory=dict)

    def denies(self, term: str, section: str) -> bool:
        return (term, None) in self.deny or (term, section) in self.deny

    @classmethod
    def load(cls, path: Path) -> DefinitionsGate:
        """Load the committed gate; a missing or malformed file fails (plan §32.13)."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BuildError(
                f"definitions gate {path} is missing; restore the committed file "
                "(it curates FAR → Part 1 defined-term links)"
            ) from exc
        except (OSError, ValueError) as exc:
            raise BuildError(f"cannot read definitions gate {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise BuildError(f"definitions gate {path}: top level must be an object")
        unknown = sorted(set(raw) - {"_comment", "deny"})
        if unknown:
            raise BuildError(f"definitions gate {path}: unknown section(s) {', '.join(unknown)}")
        if "deny" not in raw:
            raise BuildError(f"definitions gate {path}: missing section deny")
        entries = raw["deny"]
        if not isinstance(entries, list):
            raise BuildError(f"definitions gate {path}: deny must be a list")
        deny: dict[tuple[str, str | None], str] = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("term"), str)
                or not entry["term"].strip()
                or not isinstance(entry.get("reason"), str)
                or not entry["reason"].strip()
                or not isinstance(entry.get("section", ""), str)
                or set(entry) - {"term", "reason", "section"}
            ):
                raise BuildError(
                    f"definitions gate {path}: deny entries need non-empty string term and "
                    "reason, optionally a section, nothing else"
                )
            key = (entry["term"], entry.get("section") or None)
            if key in deny:
                raise BuildError(f"definitions gate {path}: duplicate deny {entry['term']!r}")
            deny[key] = entry["reason"]
        return cls(deny)


@dataclass(frozen=True)
class DefinitionTarget:
    section: str
    block_id: str
    label: str

    @property
    def key(self) -> str:
        return f"{self.section}#^{self.block_id}"


def alias_variants(label: str) -> list[tuple[str, int]]:
    """(alias, ownership rank) pairs for a label; rank 2 = the label itself,
    1 = the label minus its parenthetical, 0 = derived from the parenthetical."""
    m = _PAREN_RE.match(label)
    if m is None:
        return [(label, 2)]
    pre, inner, post = m.group("pre"), m.group("inner").strip(), m.group("post")
    out: list[tuple[str, int]] = []
    without = _SPACE_RE.sub(" ", f"{pre} {post}").strip()
    if without:
        out.append((without, 1))
    if inner:
        out.append((_SPACE_RE.sub(" ", f"{inner} {post}").strip(), 0))
    return out


def _is_abbreviation(alias: str) -> bool:
    return _ABBREVIATION_RE.match(alias) is not None


def _phrase_pattern(alias: str) -> str:
    """Escaped phrase with § 1.3's plural tolerance on its last word."""
    if alias.endswith("y"):
        return re.escape(alias[:-1]) + "(?:y|ies)"
    return re.escape(alias) + "(?:e?s)?"


def _singular_candidates(matched: str) -> Iterator[str]:
    yield matched
    if matched.endswith("ies"):
        yield matched[:-3] + "y"
    if matched.endswith("es"):
        yield matched[:-2]
    if matched.endswith("s"):
        yield matched[:-1]


@dataclass(frozen=True)
class DefinitionIndex:
    """Compiled matcher over the Part 1 definitions: alias → target key."""

    targets: dict[str, DefinitionTarget]
    insensitive: dict[str, str]
    sensitive: dict[str, str]
    _rx: re.Pattern[str] | None

    @classmethod
    def build(cls, sources: Iterable[Source], gate: DefinitionsGate) -> DefinitionIndex:
        """Compile the sources that apply to one document.

        A narrower source outranks a wider one at any alias both define;
        within a level the ownership rank decides, and a tie is dropped.
        """
        targets: dict[str, DefinitionTarget] = {}
        # case-folded alias → [(key, (level, rank), sensitive?, alias)]
        table: dict[str, list[tuple[str, tuple[int, int], bool, str]]] = {}
        for source in sources:
            sec = source.section
            defs = list(iter_definitions(sec["content"]))
            ids = block_ids(sec["content"])
            labels = definition_labels(defs)
            counts: dict[str, int] = {}
            for label in labels:
                counts[label.casefold()] = counts.get(label.casefold(), 0) + 1
            for block, label in zip(defs, labels, strict=True):
                target = DefinitionTarget(sec["section"], ids[id(block)], label)
                targets[target.key] = target
                if gate.denies(block["term"], sec["section"]) or counts[label.casefold()] > 1:
                    continue  # denied, or an unresolved duplicate: never linked
                for alias, rank in alias_variants(label):
                    sensitive = _is_abbreviation(alias)
                    if sensitive and len(alias) < MIN_ABBREVIATION_LEN:
                        continue
                    table.setdefault(alias.lower(), []).append(
                        (target.key, (source.level, rank), sensitive, alias)
                    )

        ins: dict[str, str] = {}
        sen: dict[str, str] = {}
        for owners in table.values():
            top = max(rank for _, rank, _, _ in owners)
            winners = [o for o in owners if o[1] == top]
            if len({o[0] for o in winners}) != 1:
                continue  # ambiguous between definitions: dropped
            key, _, sensitive, alias = min(winners, key=lambda o: (o[2], o[3]))
            if sensitive:
                sen[alias] = key
            else:
                ins[alias.lower()] = key
        pieces = [(alias, True) for alias in ins] + [(alias, False) for alias in sen]
        pieces.sort(key=lambda item: (-len(item[0]), item[0], item[1]))
        if not pieces:
            return cls(targets, ins, sen, None)
        body = "|".join(
            f"(?i:{_phrase_pattern(alias)})" if insensitive_ else re.escape(alias)
            for alias, insensitive_ in pieces
        )
        rx = re.compile(_BOUNDARY_BEFORE + "(?:" + body + ")" + _BOUNDARY_AFTER)
        return cls(targets, ins, sen, rx)

    def lookup(self, matched: str) -> str | None:
        """Target key for matched text: exact (abbreviation) first, then a phrase
        in any of its singular forms."""
        if matched in self.sensitive:
            return self.sensitive[matched]
        for candidate in _singular_candidates(matched.lower()):
            key = self.insensitive.get(candidate)
            if key is not None:
                return key
        return None

    def find(self, texts: Iterable[str]) -> set[str]:
        """Keys of every defined term used in ``texts``."""
        found: set[str] = set()
        if self._rx is None:
            return found
        for text in texts:
            for m in self._rx.finditer(fold(text)):
                key = self.lookup(m.group(0))
                if key is not None:
                    found.add(key)
        return found


class DefinitionLinks:
    """The corpus's definition sources plus the gate; hands each document
    the compiled index of the sources that cover it (cached per source set)."""

    def __init__(self, sources: list[Source], gate: DefinitionsGate) -> None:
        self.sources = sources
        self.gate = gate
        self._cache: dict[tuple[str, ...], DefinitionIndex] = {}
        known_terms: set[tuple[str, str]] = {
            (block["term"], source.number)
            for source in sources
            for block in iter_definitions(source.section["content"])
        }
        for term, section in gate.deny:
            if section is None:
                if not any(t == term for t, _ in known_terms):
                    raise BuildError(f"definitions gate deny names unknown term {term!r}")
            elif (term, section) not in known_terms:
                raise BuildError(
                    f"definitions gate deny names term {term!r} that § {section} does not define"
                )

    @classmethod
    def build(
        cls, docs: dict[str, dict], gate: DefinitionsGate | None = None
    ) -> DefinitionLinks | None:
        """The links layer for a canonical layer, or None when it has no source."""
        sources = discover_sources(docs)
        if not sources:
            return None
        return cls(sources, gate or DefinitionsGate())

    def index_for(self, doc: dict, part_doc: dict) -> DefinitionIndex | None:
        """The index of every source covering ``doc`` other than ``doc`` itself."""
        scope = document_scope(doc, part_doc)
        covering = [
            source
            for source in self.sources
            if source.covers(scope) and source.section.get("id") != doc.get("id")
        ]
        if not covering:
            return None
        key = tuple(source.number for source in covering)
        index = self._cache.get(key)
        if index is None:
            index = self._cache[key] = DefinitionIndex.build(covering, self.gate)
        return index
