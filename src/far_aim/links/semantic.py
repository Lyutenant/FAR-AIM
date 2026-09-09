"""Tier 4 derived links: deterministic lexical similarity (plan §12.4, §36.3).

The provider of record is ``lexical-tfidf`` v1: a pure-Python TF-IDF cosine
similarity over the official text of every FAR section and AIM paragraph.
It has no dependencies and is bit-for-bit reproducible — every sum is
accumulated in a fixed order (sorted terms, unit order within a posting
list), so ``far-aim enrich`` twice is a byte-level no-op and CI can rebuild
the layer inside the daily upstream sync.

What this module is *not*: an authority. Its output is a suggestion layer,
rendered under a clearly labelled ``## Related (derived)`` heading, filtered
by a human review overlay, and outranked by explicit cross-references
(plan §32.11, §32.12). The record format is provider-neutral so an
embedding-based provider can replace this one later without touching the
generator.

Numeric determinism note: ``math.log`` is the only libm call. A last-ulp
difference between platforms could in principle flip a score at a 4-decimal
rounding boundary; the ranking key (rounded score, then id) makes such a
flip a one-entry reorder, never a crash, and the daily CI rebuild would
surface it as a vault diff.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from far_aim.generate import BuildError

PROVIDER_ID = "lexical-tfidf"
PROVIDER_VERSION = 1
SCHEMA_VERSION = 1

CORPUS_FAR = "far"
CORPUS_AIM = "aim"

MAX_PER_CORPUS = 5
MIN_SCORE = 0.30
MIN_DF = 2
MAX_DF_RATIO = 0.15
SCORE_PLACES = 4

_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")

# Plain English function words plus the regulatory scaffolding that appears
# in nearly every section ("section", "paragraph", "shall", "means"). The
# max-df cut catches most of these anyway; listing them keeps the cut from
# depending on corpus size.
_STOPWORD_TEXT = """
    the and for are but not you all any can had her was one our out day get
    has him his how man new now old see two way who boy did its let put say
    she too use that with this from they been have were will would could
    should shall may must might than then them there these those their what
    when where which while whom whose why also into upon over under such
    each other only more most less both either neither nor per via within
    without through during before after above below between among about off
    down some none same own here because however whether unless until since
    every yet does doing done being able just like made make makes many much
    very well etc thereof therein thereto hereby herein wherein whereby
    otherwise pursuant accordance applicable including include includes
    provided except following prescribed required requirement requirements
    specified purpose purposes term terms means meaning subject regarding
    section sections paragraph paragraphs subpart subparts part parts chapter
    chapters title cfr appendix subparagraph clause item items reserved
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


@dataclass(frozen=True)
class Unit:
    """One similarity unit: a note-bearing canonical document's text."""

    id: str
    corpus: str
    text: str


def fold_plural(token: str) -> str:
    """Cheap, deterministic singularisation: ``minimums`` → ``minimum``."""
    if len(token) <= 4 or token.endswith(("ss", "us", "is")):
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return token[:-2]
    if token.endswith("s"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """ASCII word tokens, lower-cased, stopwords dropped, plurals folded."""
    out: list[str] = []
    for token in _TOKEN_RE.findall(text.lower()):
        if token in STOPWORDS:
            continue
        token = fold_plural(token)
        if token not in STOPWORDS:
            out.append(token)
    return out


Related = dict[str, list[tuple[str, float]]]


def compute_related(units: Iterable[Unit]) -> Related:
    """unit id → ranked (target id, score) suggestions, per plan §36.3.

    Deterministic by construction: units are processed in the order given,
    vocabulary and accumulation orders are sorted, and ties break on id.
    """
    units = list(units)
    ids = [unit.id for unit in units]
    if len(set(ids)) != len(ids):
        raise BuildError("similarity units must have unique ids")
    corpus_of = {unit.id: unit.corpus for unit in units}
    counts = [Counter(tokenize(unit.text)) for unit in units]
    df: Counter[str] = Counter()
    for count in counts:
        df.update(count.keys())
    total = len(units)
    # The floor keeps tiny corpora (fixtures, a single-part parse) usable:
    # with fewer than 1/MAX_DF_RATIO units the ratio alone would empty the
    # vocabulary. On the full corpus the ratio governs (≈1,000 units).
    max_df = max(MIN_DF, int(total * MAX_DF_RATIO))
    vocab = {term for term, freq in df.items() if MIN_DF <= freq <= max_df}

    vectors: list[dict[str, float]] = []
    postings: dict[str, list[tuple[int, float]]] = {term: [] for term in vocab}
    for index, count in enumerate(counts):
        weights = {
            term: (1.0 + math.log(freq)) * math.log(total / df[term])
            for term, freq in count.items()
            if term in vocab
        }
        norm = math.sqrt(sum(weights[term] ** 2 for term in sorted(weights)))
        if norm > 0:
            weights = {term: weight / norm for term, weight in weights.items()}
        vectors.append(weights)
        for term in sorted(weights):
            postings[term].append((index, weights[term]))

    related: Related = {}
    for index, weights in enumerate(vectors):
        scores: dict[int, float] = {}
        for term in sorted(weights):
            own = weights[term]
            for other, weight in postings[term]:
                if other != index:
                    scores[other] = scores.get(other, 0.0) + own * weight
        ranked = sorted(
            ((round(score, SCORE_PLACES), ids[other]) for other, score in scores.items()),
            key=lambda entry: (-entry[0], entry[1]),
        )
        taken: Counter[str] = Counter()
        chosen: list[tuple[str, float]] = []
        for score, target in ranked:
            if score < MIN_SCORE:
                break
            corpus = corpus_of[target]
            if taken[corpus] >= MAX_PER_CORPUS:
                continue
            taken[corpus] += 1
            chosen.append((target, score))
        related[ids[index]] = chosen
    return related


# ---------------------------------------------------------------------------
# related.json — the committed, machine-written layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelatedIndex:
    """The parsed ``related.json``: provenance plus per-unit suggestions."""

    inputs: dict[str, str]
    units: Related
    provider_id: str = PROVIDER_ID
    provider_version: int = PROVIDER_VERSION

    def targets(self, unit_id: str) -> list[str]:
        return [target for target, _ in self.units.get(unit_id, [])]

    @classmethod
    def load(cls, path: Path) -> RelatedIndex:
        """Parse and shape-check; a provider mismatch means "recompute"."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BuildError(f"cannot read related-links file {path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
            raise BuildError(f"related-links file {path}: unsupported schema")
        unknown = sorted(set(raw) - {"schema", "provider", "inputs", "units"})
        if unknown:
            raise BuildError(f"related-links file {path}: unknown key(s) {', '.join(unknown)}")
        provider = raw.get("provider")
        if (
            not isinstance(provider, dict)
            or provider.get("id") != PROVIDER_ID
            or provider.get("version") != PROVIDER_VERSION
        ):
            raise BuildError(
                f"related-links file {path} was produced by a different provider than "
                f"{PROVIDER_ID} v{PROVIDER_VERSION}; run `far-aim enrich`"
            )
        inputs = raw.get("inputs")
        if not isinstance(inputs, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in inputs.items()
        ):
            raise BuildError(f"related-links file {path}: inputs must map layer → hash")
        units_raw = raw.get("units")
        if not isinstance(units_raw, dict):
            raise BuildError(f"related-links file {path}: units must be an object")
        units: Related = {}
        for unit_id, entries in units_raw.items():
            if not isinstance(unit_id, str) or not isinstance(entries, list):
                raise BuildError(f"related-links file {path}: malformed unit {unit_id!r}")
            parsed: list[tuple[str, float]] = []
            seen: set[str] = set()
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or set(entry) != {"target", "score"}
                    or not isinstance(entry["target"], str)
                    or isinstance(entry["score"], bool)
                    or not isinstance(entry["score"], int | float)
                ):
                    raise BuildError(
                        f"related-links file {path}: malformed entry under {unit_id!r}"
                    )
                if entry["target"] in seen or entry["target"] == unit_id:
                    raise BuildError(
                        f"related-links file {path}: duplicate or self target under "
                        f"{unit_id!r}"
                    )
                seen.add(entry["target"])
                parsed.append((entry["target"], float(entry["score"])))
            units[unit_id] = parsed
        return cls(inputs=dict(inputs), units=units)

    def verify_ids(self, known: Iterable[str]) -> None:
        """Every unit and target must be a document of the accepted layers."""
        known_ids = set(known)
        for unit_id, entries in self.units.items():
            if unit_id not in known_ids:
                raise BuildError(
                    f"related-links unit {unit_id!r} is not in the canonical layers; "
                    "run `far-aim enrich`"
                )
            for target, _ in entries:
                if target not in known_ids:
                    raise BuildError(
                        f"related-links target {target!r} (from {unit_id!r}) is not in the "
                        "canonical layers; run `far-aim enrich`"
                    )


def related_bytes(inputs: dict[str, str], related: Related) -> bytes:
    """Exact on-disk bytes of ``related.json``: one line per unit so a PR diff
    shows exactly which suggestions moved."""
    lines = [
        "{",
        f'"schema": {SCHEMA_VERSION},',
        f'"provider": {json.dumps({"id": PROVIDER_ID, "version": PROVIDER_VERSION})},',
        f'"inputs": {json.dumps(dict(sorted(inputs.items())))},',
        '"units": {',
    ]
    unit_ids = sorted(related)
    for position, unit_id in enumerate(unit_ids):
        entries = [
            {"target": target, "score": score} for target, score in related[unit_id]
        ]
        comma = "," if position + 1 < len(unit_ids) else ""
        lines.append(f"{json.dumps(unit_id)}: {json.dumps(entries)}{comma}")
    lines.extend(["}", "}", ""])
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# related-review.json — the committed human overlay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Review:
    """Reviewer decisions: (unit id, target id) pairs to suppress, with reasons."""

    deny: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Review:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BuildError(
                f"related-links review file {path} is missing; restore the committed file "
                "(it curates the derived links)"
            ) from exc
        except (OSError, ValueError) as exc:
            raise BuildError(f"cannot read related-links review file {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise BuildError(f"related-links review file {path}: top level must be an object")
        unknown = sorted(set(raw) - {"_comment", "deny"})
        if unknown:
            raise BuildError(
                f"related-links review file {path}: unknown section(s) {', '.join(unknown)}"
            )
        entries = raw.get("deny")
        if not isinstance(entries, list):
            raise BuildError(f"related-links review file {path}: deny must be a list")
        deny: dict[tuple[str, str], str] = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"unit", "target", "reason"}
                or not all(isinstance(entry[key], str) and entry[key].strip() for key in entry)
            ):
                raise BuildError(
                    f"related-links review file {path}: deny entries need non-empty string "
                    "unit, target and reason"
                )
            key = (entry["unit"], entry["target"])
            if key in deny:
                raise BuildError(
                    f"related-links review file {path}: duplicate deny {key[0]!r} → {key[1]!r}"
                )
            deny[key] = entry["reason"]
        return cls(deny)

    def unknown_ids(self, known: Iterable[str]) -> list[str]:
        """Denied *unit* ids the corpus does not contain (a typo, or a note
        that no longer exists). A vanished *target* is not an error: the
        pair simply no longer surfaces and shows up as stale instead."""
        known_ids = set(known)
        return sorted({unit for unit, _ in self.deny if unit not in known_ids})

    def stale(self, related: Related) -> list[tuple[str, str]]:
        """Denied pairs the provider no longer suggests (informational)."""
        return sorted(
            (unit, target)
            for unit, target in self.deny
            if target not in {t for t, _ in related.get(unit, [])}
        )

    def apply(self, related: Related) -> Related:
        return {
            unit: [(target, score) for target, score in entries if (unit, target) not in self.deny]
            for unit, entries in related.items()
        }
