"""Pilot/Controller Glossary term recognition for AIM text (plan §12.2, Tier 2).

The PCG is the AIM's own glossary, so an AIM note may list the glossary
terms its official text uses. This is a *lexical* relationship, not an
explicit citation, so it renders in its own ``## Glossary Terms`` section,
never among the Tier 1 ``## Explicit Cross-References``, and it is gated
so that ordinary words the glossary also happens to define (``AIRCRAFT``,
``OVER``, ``IF``) never become links.

Deterministic rules — no dictionary, no heuristics that depend on the host:

- A term's ``[ICAO]``-style bracket qualifier is dropped, and a trailing
  parenthetical acronym is split off: ``AIRSPACE FLOW PROGRAM (AFP)`` →
  phrase ``AIRSPACE FLOW PROGRAM`` + acronym ``AFP``. A parenthetical
  counts as an acronym only with evidence — it reads as an initialism of
  the phrase (``is_initialism``) or it is itself one of the PCG's
  ``See``-only abbreviation entries; phraseology placeholders (``HEAVY
  (AIRCRAFT)``, ``… (FIX)``) are not aliases.
- **Phrases** (two or more words, or hyphen/slash-joined) match
  case-insensitively at word boundaries: ``flight plan`` → ``FLIGHT PLAN``.
- **Acronyms** — a parenthetical acronym, or an all-caps term (hyphens
  and slashes allowed: ``ADS-B``) whose glossary entry is nothing but
  ``See`` references (``ATC- See AIR TRAFFIC CONTROL``; the PCG's own
  abbreviation entries) — match case-sensitively and only at three or more
  characters (``AC``, ``IF``, ``GS`` are too ambiguous even in capitals).
  The acronym test runs before the phrase test, so a hyphenated acronym is
  never demoted to a case-insensitive phrase.
- **Single-word defined terms** (``AIRCRAFT``, ``TRANSPONDER``) do not match
  unless the committed gate allows them.
- The **gate** (``data/links/pcg-glossary-gate.json``, human-maintained,
  plan §12.3) denies entries whose capitalized form still collides with
  ordinary AIM usage (``CAT`` — approach categories, not clear-air
  turbulence; ``CFR``; ``CENTER`` in phraseology) and allows single-word
  aviation nouns (``NOTAM``, ``TRANSPONDER``, ``WAYPOINT``). Every gate
  entry must name an existing term; a stale entry fails the build.
- Longest alias wins at a position (``AIR TRAFFIC CONTROL`` is one term,
  not also ``AIR TRAFFIC``); Unicode hyphens and apostrophes are folded to
  ASCII on both sides so ``VFR‐ON‐TOP`` matches ``VFR-on-top``.
- An alias two terms would share goes to the term with the strongest
  claim — its text *is* the alias (``AERODROME``), else is the alias once
  its bracket qualifier is dropped (``AERODROME [ICAO]``, ``ADS [ICAO]``),
  else a parenthetical (``… (ADS)``); a tie at the top rank drops the alias
  as ambiguous.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.generate import BuildError

GATE_FILENAME = "pcg-glossary-gate.json"
MIN_ACRONYM_LEN = 3

_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*")
_PAREN_ACRONYM_RE = re.compile(r"^(?P<phrase>.*\S)\s*\((?P<acronym>[A-Z0-9][A-Z0-9/\-]+)\)\s*$")
_ACRONYM_RE = re.compile(r"^[A-Z0-9][A-Z0-9/\-]*$")
_MULTIWORD_RE = re.compile(r"[\s/\-]")
_FOLD = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
                       "’": "'", "‘": "'", "“": '"', "”": '"'})
_BOUNDARY_BEFORE = r"(?<![A-Za-z0-9])"
_BOUNDARY_AFTER = r"(?![A-Za-z0-9])"


def fold(text: str) -> str:
    return text.translate(_FOLD)


def normalize_label(text: str) -> str:
    """A term or entry label with brackets dropped, typography folded and
    the entry's trailing dash (any dash, ``SAW–``) removed."""
    return fold(_BRACKET_RE.sub(" ", text)).strip().rstrip("-").strip()


def term_aliases(term: str) -> tuple[str, str | None]:
    """(base text, parenthetical acronym or None) for a PCG term string."""
    base = normalize_label(term)
    if (m := _PAREN_ACRONYM_RE.match(base)) is not None:
        return m.group("phrase").strip(), m.group("acronym")
    return base, None


def is_initialism(acronym: str, phrase: str) -> bool:
    """True when ``acronym`` reads as an abbreviation of ``phrase``.

    Walking the phrase's words in order, each acronym letter must be the
    initial of the next used word or a later letter inside the word being
    used (``TRACON`` ← Terminal Radar Approach CONtrol, ``STOL`` ← Short
    TakeOff and Landing, ``CAS`` ← Calibrated AirSpeed); words may be
    skipped whole. A placeholder such as ``HEAVY (AIRCRAFT)`` or ``… (FIX)``
    cannot be read this way.
    """
    letters = [ch for ch in acronym.upper() if ch.isalnum()]
    words = [w.upper() for w in re.split(r"[\s/\-]+", phrase) if w]
    if not letters or not words:
        return False

    @functools.cache
    def rec(wi: int, j: int, li: int) -> bool:
        if li == len(letters):
            return True
        if wi == len(words):
            return False
        word = words[wi]
        if j == 0:  # decide whether to use this word; using it takes its initial
            if rec(wi + 1, 0, li):
                return True
            return word[0] == letters[li] and rec(wi, 1, li + 1)
        if j == len(word):
            return rec(wi + 1, 0, li)
        if word[j] == letters[li] and rec(wi, j + 1, li + 1):
            return True
        return rec(wi, j + 1, li)

    return rec(0, 0, 0)


def is_alias_entry(term_doc: dict, base: str) -> bool:
    """True when the entry's content is only ``See`` references (an abbreviation)."""
    body = [
        block
        for block in term_doc["content"]
        if not (block["type"] == "entry" and normalize_label(block["text"]) == base)
    ]
    return bool(body) and all(
        block["type"] == "reference" and block.get("kind") == "see" for block in body
    )


@dataclass(frozen=True)
class Gate:
    """The committed allow/deny decisions, keyed by verbatim PCG term text."""

    deny: dict[str, str] = field(default_factory=dict)
    allow_words: dict[str, str] = field(default_factory=dict)
    allow_acronyms: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Gate:
        """Load the committed gate; a missing or unreadable file fails.

        The gate is authoritative curation: silently proceeding without it
        would re-link every denied false positive and drop every allowed
        term (plan §32.13 — when uncertain, fail).
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BuildError(
                f"glossary gate {path} is missing; restore the committed file "
                "(it curates AIM → PCG term links)"
            ) from exc
        except (OSError, ValueError) as exc:
            raise BuildError(f"cannot read glossary gate {path}: {exc}") from exc
        # Fail closed on shape too: a section that is missing, null, or
        # misspelled would silently drop curated decisions.
        required = ("deny", "allow_words", "allow_acronyms")
        if not isinstance(raw, dict):
            raise BuildError(f"glossary gate {path}: top level must be an object")
        unknown = sorted(set(raw) - {"_comment", *required})
        if unknown:
            raise BuildError(f"glossary gate {path}: unknown section(s) {', '.join(unknown)}")
        missing = sorted(set(required) - set(raw))
        if missing:
            raise BuildError(f"glossary gate {path}: missing section(s) {', '.join(missing)}")

        def section(name: str) -> dict[str, str]:
            entries = raw[name]
            if not isinstance(entries, list):
                raise BuildError(f"glossary gate {path}: {name} must be a list")
            out: dict[str, str] = {}
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("term"), str)
                    or not entry["term"].strip()
                    or not isinstance(entry.get("reason"), str)
                    or not entry["reason"].strip()
                ):
                    raise BuildError(
                        f"glossary gate {path}: {name} entries need non-empty string "
                        "term and reason"
                    )
                if entry["term"] in out:
                    raise BuildError(f"glossary gate {path}: duplicate {name} {entry['term']!r}")
                out[entry["term"]] = entry["reason"]
            return out

        return cls(section("deny"), section("allow_words"), section("allow_acronyms"))


@dataclass(frozen=True)
class GlossaryIndex:
    """Compiled matcher: alias → term id for each case class, one regex.

    Both classes live in one longest-first alternation (phrases wrapped in
    a scoped ``(?i:…)``), so at any position the longest alias wins across
    classes: ``ADS-B`` is never also ``ADS``, ``VFR-ON-TOP`` never ``VFR``.
    """

    insensitive: dict[str, str]
    sensitive: dict[str, str]
    _rx: re.Pattern[str] | None

    @classmethod
    def build(cls, pcg_docs: dict[str, dict], gate: Gate) -> GlossaryIndex:
        terms = [
            term_doc
            for doc in pcg_docs.values()
            for term_doc in doc.get("terms") or []
        ]
        known = {t["term"] for t in terms}
        for name, entries in (
            ("deny", gate.deny),
            ("allow_words", gate.allow_words),
            ("allow_acronyms", gate.allow_acronyms),
        ):
            for term in entries:
                if term not in known:
                    raise BuildError(f"glossary gate {name} names unknown PCG term {term!r}")
        # The PCG's own abbreviation entries (See-only, all caps): evidence
        # that a parenthetical is an acronym rather than a placeholder.
        abbreviations = {
            term_aliases(t["term"])[0]
            for t in terms
            if _ACRONYM_RE.match(term_aliases(t["term"])[0] or " ")
            and is_alias_entry(t, term_aliases(t["term"])[0])
        }
        # A denied term blocks its alias text for every term (``CAT`` stays
        # unlinked even though ``CLEAR AIR TURBULENCE (CAT)`` would re-claim
        # it); the expanded phrase itself still matches.
        denied_aliases: set[str] = set()
        for term in gate.deny:
            base, acronym = term_aliases(term)
            denied_aliases.add(base.lower())
            if acronym:
                denied_aliases.add(acronym.lower())

        # case-folded alias → [(term id, ownership rank, case-sensitive?, alias)]
        # Rank: 2 = the term text is the alias (``ADS``), 1 = it is the alias
        # once brackets are dropped (``ADS [ICAO]``), 0 = a parenthetical.
        table: dict[str, list[tuple[str, int, bool, str]]] = {}

        def claim(alias: str, term_id: str, rank: int, sensitive: bool) -> None:
            if alias.lower() in denied_aliases:
                return
            table.setdefault(alias.lower(), []).append((term_id, rank, sensitive, alias))

        def ownership(term: str, base: str) -> int:
            if base == fold(term).strip().rstrip("-").strip():
                return 2
            return 1 if base == normalize_label(term) else 0

        for term_doc in terms:
            term = term_doc["term"]
            if term in gate.deny:
                continue
            base, acronym = term_aliases(term)
            if not base:
                continue
            if acronym and not (acronym in abbreviations or is_initialism(acronym, base)):
                acronym = None  # a placeholder ("HEAVY (AIRCRAFT)"), not an acronym
            rank = ownership(term, base)
            # Acronyms first: a See-only all-caps entry stays capitals-only
            # even when hyphenated or slashed (ADS-B, ADS-C), and the gate's
            # allow_acronyms decision outranks the phrase rule.
            is_acronym = term in gate.allow_acronyms or (
                _ACRONYM_RE.match(base) is not None
                and term not in gate.allow_words
                and is_alias_entry(term_doc, base)
            )
            if is_acronym:
                if len(base) >= MIN_ACRONYM_LEN:
                    claim(base, term_doc["id"], rank, sensitive=True)
            elif _MULTIWORD_RE.search(base) or term in gate.allow_words:
                claim(base, term_doc["id"], rank, sensitive=False)
            if acronym and len(acronym) >= MIN_ACRONYM_LEN:
                claim(acronym, term_doc["id"], 0, sensitive=True)

        # One owner per alias across both case classes: the sole claimant, or
        # the unique claimant of the highest ownership rank (``AERODROME``
        # beats ``AERODROME [ICAO]`` beats a parenthetical); otherwise
        # ambiguous, dropped.
        ins: dict[str, str] = {}
        sen: dict[str, str] = {}
        for owners in table.values():
            top = max(rank for _, rank, _, _ in owners)
            winners = [o for o in owners if o[1] == top]
            if len({o[0] for o in winners}) != 1:
                continue
            term_id, _, sensitive, alias = min(winners, key=lambda o: (o[2], o[3]))
            if sensitive:
                sen[alias] = term_id
            else:
                ins[alias.lower()] = term_id
        pieces = [(alias, True) for alias in ins] + [(alias, False) for alias in sen]
        pieces.sort(key=lambda item: (-len(item[0]), item[0], item[1]))
        if not pieces:
            return cls(ins, sen, None)
        body = "|".join(
            f"(?i:{re.escape(alias)})" if insensitive_ else re.escape(alias)
            for alias, insensitive_ in pieces
        )
        return cls(ins, sen, re.compile(_BOUNDARY_BEFORE + "(?:" + body + ")" + _BOUNDARY_AFTER))

    def lookup(self, matched: str) -> str | None:
        """Term id for matched text: exact (acronym) first, then case-folded."""
        if matched in self.sensitive:
            return self.sensitive[matched]
        return self.insensitive.get(matched.lower())

    def find(self, texts: Iterable[str]) -> set[str]:
        """Ids of every glossary term used in ``texts``."""
        found: set[str] = set()
        if self._rx is None:
            return found
        for text in texts:
            for m in self._rx.finditer(fold(text)):
                term_id = self.lookup(m.group(0))
                if term_id is not None:
                    found.add(term_id)
        return found
