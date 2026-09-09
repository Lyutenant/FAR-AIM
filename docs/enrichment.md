# Enrichment layer (Phase 9)

Design of record: plan §36. This note is the operator's view: what the
files are, what the commands do, and how to review or curate the output.

Everything here is a study aid layered *beside* the authoritative corpora.
It never touches canonical JSON, never adds words to official text, and can
be removed by deleting `data/enrichment/` and rebuilding (plan §32.12).

## Files

| Path | Owner | Purpose |
|---|---|---|
| `data/enrichment/concepts.json` | human (Tier 3, plan §12.3) | study concepts, their prerequisites, and the notes that define them |
| `data/enrichment/related.json` | `far-aim enrich` (Tier 4, plan §12.4) | per-note "related" suggestions from a deterministic similarity provider |
| `data/enrichment/related-review.json` | human | deny list for individual suggestions, with reasons |
| `vault/Concepts/` | generator | one note per concept plus `Concept Map.md`; generator-owned like `FAR/`, `AIM/`, `PCG/` |

All three data files are committed. `related.json` is machine-written but
committed on purpose: it is reviewable in a pull request (one line per
unit), it pins the derived relations independently of provider drift, and
a future non-reproducible provider (embeddings) can use the same slot.

## `far-aim enrich`

Computes `related.json` from the verified eCFR layer and, when parsed, the
AIM layer:

- **Units**: every FAR section and every AIM paragraph (heading + official
  text). Appendices, container notes and glossary terms are not units.
- **Provider** `lexical-tfidf` v1: pure-Python TF-IDF cosine similarity —
  ASCII tokens, stopwords and regulatory scaffolding dropped, simple plural
  folding, terms in fewer than 2 or more than 15 % of units ignored. Up to
  5 FAR and 5 AIM targets per unit with cosine ≥ 0.30, scores rounded to 4
  places, ties broken by id. ~35 s for the full corpus, no dependencies,
  bit-identical across runs.
- **Provenance**: the file records the canonical hashes it was computed
  from. The generator refuses a file whose inputs do not match the accepted
  layers (`stale; run far-aim enrich`), so derived links can never lag the
  content silently (plan §32.13).
- **Idempotent**: unchanged output is not rewritten. The review overlay is
  validated (a deny entry naming a unit id that does not exist is an error;
  a denied pair the provider no longer suggests is a warning) and never
  modified, except that a missing overlay is created empty on first run.

`far-aim update` runs `enrich` between `parse` and `diff`, so every
accepted upstream change refreshes the suggestions before the vault is
rebuilt; the resulting vault diff is where changed suggestions get
reviewed in the sync PR. `validate` does not recompute similarity — it
verifies the file's shape, provenance and targets, and byte-compares the
rendered vault like every other layer.

### Reviewing a suggestion

To suppress one derived link, add a deny entry:

```json
{"unit": "cfr-14-91.155", "target": "cfr-14-135.205", "reason": "Part 135 rule; confuses students"}
```

`unit` and `target` are canonical ids (`cfr-14-91.155`, `aim-3-1-4`). Then
`far-aim build-vault`; the suggestion disappears from that note's
`## Related (derived)` section. `related.json` itself is not edited by hand.

### How it renders

Each FAR section note and AIM paragraph note with suggestions ends with:

```markdown
## Related (derived)

> [!info] Derived links
> Suggested by lexical similarity (`lexical-tfidf` v1), not by an explicit
> reference. Review in `data/enrichment/related-review.json`.

- [[3-1-4|AIM 3-1-4 — Basic VFR Weather Minimums]]
- [[103.23|§ 103.23 — Flight visibility and cloud clearance requirements]]
```

A target the same note already lists under `## Explicit Cross-References`
is omitted (explicit outranks inferred, plan §32.11). Scores stay in the
JSON; printing them would churn every note on tiny content edits.

## Concept graph

`concepts.json` holds a list of concept records:

```json
{
  "id": "vfr-weather-minimums",
  "title": "VFR Visibility and Cloud Clearance",
  "area": "Weather and Flight Rules",
  "description": "One or two curator-written sentences. Never regulatory text.",
  "prerequisites": ["airspace-classification"],
  "far": ["91.155", "Part 91"],
  "aim": ["3-1-4"],
  "pcg": ["VISUAL FLIGHT RULES"],
  "see_also": ["VFR Weather Minimums"]
}
```

Rules enforced by `build-vault` (all fail the build):

- `id` is a slug, unique; `title` is the note filename stem and must not
  case-fold-collide with any generated stem, heading alias, concept or
  curated note (a concept named "Basic VFR Weather Minimums" would
  silently strip that alias from § 91.155 and AIM 3-1-4 — so it is rejected).
  Stems follow the vault naming policy: no commas, no trailing dot.
- `prerequisites` name concept ids and must form a DAG.
- `far`, `aim`, `pcg` name generated stems (`91.155`, `Part 91`, `3-1-4`,
  `AIM 3-2`, `AIM`, `VISUAL FLIGHT RULES`) that the accepted editions
  define. When an upstream edition drops a section a concept cites, the
  daily sync fails until the concept is fixed — a concept must never point
  nowhere.
- `see_also` names any vault stem, including curated notes on disk under
  `Collections/`, `Topics/` and `Study/`.
- `description` is the curator's own words, rendered under a "Curated
  concept" callout. It is not official text and must not be presented as
  such (plan §32.1, §32.3).

Rendering: `Concepts/<Title>.md` (`type: concept`, `generated: true`) with
the callout, the description, `## Prerequisites`, `## Builds on this`
(derived reverse edges), `## Regulations`, `## AIM guidance`,
`## Glossary`, `## See also`. `Concepts/Concept Map.md` lists the starting
points, groups concepts by area (in file order), gives a deterministic
study order (prerequisites first, then alphabetical), and draws the
prerequisite graph as a Mermaid diagram Obsidian renders natively. `Home`
links the Concept Map when a graph is present.

Authoritative notes are not modified by the concept layer; Obsidian's
backlinks pane on § 91.155 already shows which concepts cite it. The
Private Pilot collection pages link the concepts that cover them under a
`## Concepts` heading.

The initial graph (46 concepts, Private Pilot scope) was drafted with the
same scrutiny as the collections: every reference verified against the
accepted editions at build time, descriptions written as study prompts,
not summaries of the rules.

## Separability (plan §32.12), checked

- Delete `data/enrichment/` and rebuild: `Concepts/` is pruned as stale,
  every `## Related (derived)` section disappears, and nothing else in the
  vault changes (`tests/test_enrichment.py`).
- A vault built without the layer is byte-identical to one built with it
  once those sections are removed — the enrichment never edits official
  text or frontmatter of authoritative notes.
- No enrichment code runs inside the parsers, and the canonical hashes the
  manifest pins are unaffected by anything under `data/enrichment/`.

## Follow-up: embedding provider

The record format is provider-neutral (`provider.id`/`version` per file).
An embedding-based provider (a local sentence-transformers model or a paid
API) would write the same file with a different provider id; because such
output is not reproducible on the CI runner, the file would be regenerated
manually and committed, with `enrich` validating rather than recomputing
it. The lexical provider stays the default because the daily sync must
rebuild the layer deterministically without models or keys.
