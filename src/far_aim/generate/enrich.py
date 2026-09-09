"""The enrichment layer as the generator sees it (plan §36).

Everything under ``data/enrichment/`` arrives here as one frozen
:class:`EnrichmentLayer`, loaded once by the CLI and threaded through
``plan_vault`` exactly like the AIM and PCG layers. Two render helpers live
here too: the ``## Related (derived)`` section for authoritative notes
(Tier 4) and the unit collector ``far-aim enrich`` feeds to the provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from far_aim.links import semantic
from far_aim.links.concepts import ConceptGraph
from far_aim.links.semantic import RelatedIndex

# id → (stem, display) for every unit the provider may suggest.
Targets = dict[str, tuple[str, str]]

RELATED_HEADING = "## Related (derived)"
_WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]|#]+)")


@dataclass(frozen=True)
class EnrichmentLayer:
    """Concept graph (Tier 3), review-filtered related links (Tier 4), and the
    curated note stems on disk that concept notes may link to."""

    concepts: ConceptGraph | None = None
    related: RelatedIndex | None = None
    # stem → vault-relative path parts of every curated note on disk
    # (``Collections/``, ``Topics/``, ``Study/``); ``see_also`` may name them.
    curated_notes: dict[str, tuple[str, ...]] = field(default_factory=dict)


def linked_stems(chunks: list[str]) -> set[str]:
    """Wikilink targets already emitted in ``chunks`` (explicit references)."""
    return {
        target for chunk in chunks for target in _WIKILINK_TARGET_RE.findall(chunk)
    }


def related_chunks(
    unit_id: str,
    related: RelatedIndex | None,
    targets: Targets,
    *,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """``## Related (derived)`` for one authoritative note, or nothing.

    Suggestions whose stem is already an explicit cross-reference of the same
    note are dropped (plan §32.11: explicit outranks inferred). Order is the
    provider's ranking; the score stays in ``related.json``.
    """
    if related is None:
        return []
    items: list[str] = []
    for target in related.targets(unit_id):
        stem, display = targets[target]
        if stem in exclude:
            continue
        items.append(f"- [[{stem}|{_link_display(display)}]]")
    if not items:
        return []
    callout = "\n".join(
        [
            "> [!info] Derived links",
            f"> Suggested by lexical similarity (`{related.provider_id}` "
            f"v{related.provider_version}), not by an explicit reference. "
            "Review in `data/enrichment/related-review.json`.",
        ]
    )
    return [RELATED_HEADING, callout, "\n".join(items)]


def _link_display(text: str) -> str:
    return text.replace("[", "").replace("]", "").replace("|", "/")


def unit_text(heading: str, texts: list[str]) -> str:
    """Provider input for one unit: heading first, then the official text."""
    return "\n".join([heading, *texts])


# Re-exported so callers that already import this module need not also
# import the provider package for its ``Unit`` type.
Unit = semantic.Unit
