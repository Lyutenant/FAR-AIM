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

## Exam-prep layer (Phase 10b–10d; plan §39.3–§39.5)

Two more committed files under `data/enrichment/`, loaded like the concept
graph (malformed ⇒ build error; absent ⇒ nothing rendered) and verified
against the built layers in `plan_vault` (`links.study`):

- **`acs-map.json`** — where the vault covers each ACS Knowledge and Risk
  element, or why it cannot. `tasks[PA.I.A]` is a Task's default for every
  K/R element; `elements[PA.I.A.K1]` overrides one element; a sub-element
  inherits its parent's entry. Each entry lists `far` (section, part or
  appendix stems), `aim` (paragraph stems), `pcg` (term stems) and
  `concepts` (concept titles), or an `out_of_corpus` reason (the FAA
  handbooks, the POH/AFM). Gates: every named code must exist in the
  accepted ACS, every stem must be a generated note, and **every**
  Knowledge/Risk element (archived placeholders excepted) must resolve to
  stems or a reason — so the coverage table on `Private Pilot Prep` is
  the honest statement of what the vault can and cannot teach. Rendered as
  `## Where to study (curated)` on each ACS Task note.
- **`ppl-study.json`** — one entry per covered FAR section or AIM
  paragraph, keyed by stem: `gist`, `why`, `numbers` (`value`, verbatim
  `quote`, optional CFR paragraph `where` such as `(a)(1)`), `traps`,
  `questions` (`q`, `a`, `cite` stems), `mnemonics` (labelled as
  training-community devices), `acs` codes, a `stage` (declared in
  `stages`) and `review` (`unreviewed` until a human checks the wording).
  A top-level `oral` object (Phase 10d) maps an ACS Task code to a list of
  scenarios — `scenario`, `answer`, `cite` stems, optional `find_it` hint
  — the curator's checkride-style questions; a Task the ACS lacks or an
  unresolved cite fails the build.
  Gates: stems and citations must be FAR sections or AIM paragraphs the
  vault holds, codes must exist, at least one question per entry, and the
  **verbatim-numbers gate**: every `quote` must occur in the official text
  of the cited note — inside the paragraph `where` names for FAR sections
  (AIM entries quote the whole paragraph) — or the build fails. The
  pipeline cannot verify a gist, but it can verify that every number a
  gist rests on is the number the rule says.

Rendering (`generate/prep_notes.py`): a `[!study]` callout after the Source
callout of every covered FAR section and AIM paragraph note (gist, why,
numbers, traps, mnemonics, ACS block links, stage — styled purple by the
CSS snippet); and the generator-owned `vault/Prep/Private Pilot/`:
`Private Pilot Prep` (coverage per Area, entries by stage, review counts),
`Part 61 Map` and `Part 91 Map` (subparts, section ranges, studied
sections with gists — the number pattern pilots navigate by), `Numbers
Sheet` (every quoted threshold by Area, linked to its paragraph) and
`Where Do I Look` (questions → citations by ACS Task, then citation →
gist) and `Reading Path` (every entry in training order — the `stages`
in declared order, each with its description, its entries sorted FAR
sections then AIM paragraphs by citation, and the ACS Tasks those
entries' codes touch). Phase 10d adds `Oral Prep/Oral Prep <Roman>` —
one note per Area with scenarios, listing every Task of the Area with its
Knowledge and Risk elements verbatim as block links, then each scenario
with its answer (labelled a study aid), hint and citations — and `ACS
Checklist` (every Task's Knowledge and Risk elements as `- [ ]` items
under a warning callout: copy it into `Study/` before ticking, this copy
is regenerated). `Study/` remains the reader's untouched folder; Prep is
rebuilt.

The Anki export (plan §39.5; `prep_notes.build_anki`) is written to
`vault/Prep/Private Pilot/anki/private-pilot.txt`: header lines
`#separator:tab`, `#html:true`, `#guid column:1`, `#notetype column:2`,
`#deck column:3`, `#tags column:6`, then the ownership comment
`#far_aim_generated_export:1` (Anki ignores comment lines; the generator
uses it to recognise its own file, since the export has no frontmatter).
Rows are guid, note type, deck, front, back, tags. Card kinds, all from
the two curated files and using only Anki's built-in note types: citation
↔ gist (`Basic (and reversed card)`), each question → answer plus the
citations it names (`Basic`), a `Cloze` per verified number — the value
is blanked inside the gist when it occurs there, otherwise its leading
numeral, otherwise the card is `<citation>: {{c1::value}}` — with the
verbatim quote and paragraph as the extra field, and each mapped ACS
Knowledge/Risk element → the notes that cover it, or the out-of-corpus
reason (`Basic`). Decks are `Private Pilot::<Roman>. <Area title>` (an
entry's first ACS code decides; uncoded entries go to `::General`); tags
are `far-aim`, `ppl::<stage>`, `cite::<stem>` or `acs::<Task>`. The GUID
is the first 16 hex digits of sha256 over `far-aim|<kind>|<stem>|<index>`,
so a re-import of a rebuilt file updates card text in place and keeps
review history; fields are HTML-escaped with newlines as `<br>` so no
record separator can appear inside a field. Review state lives in Anki,
never in the vault (§32.10).

Content (Phase 10c, 2026-09-14): the guide covers every FAR section and
AIM paragraph the Private Pilot collection links that the ACS map also
references — 206 entries (60 FAR sections, 146 AIM paragraphs), 552
verbatim-verified numbers, 218 questions, 119 traps, 25 mnemonics;
stages pre-solo 71, solo-xc 91, checkride 44. Of the 235 FAR/AIM stems
the ACS map names, 205 have an entry; the rest are part-index stems
(`Part 39`, `Part 43 Appendix A`, …, which are not sections) and 22
map-only stems no collection page links (§§ 21.175, 21.181, 21.197,
21.199, 43.9, 68.5, 91.139, 91.185, 91.319, 91.509; AIM 1-1-1, 1-2-1,
1-2-4, 2-1-1, 2-3-1, 2-3-15, 3-3-1, 3-3-2, 5-2-4, 5-6-10, 7-1-25,
7-6-9) — candidates for a later batch. Every entry is Claude-drafted and
`review: unreviewed`; the gate proves the quotes, not the gists. Quotes
are copied from a dump of the canonical text (never typed from memory)
because the AIM uses U+2010 hyphens (`two‐way`) and curly quotes that a
retyped quote silently misses; a truncated dump line is never quoted
past its visible end.

Content (Phase 10d, 2026-09-14): 125 oral scenarios under 42 Tasks —
every single-engine land Task of Areas I–IX, XI and XII; the seaplane and
multiengine Tasks are left empty and appear in their Area's note as "No
scenarios yet" (Area X has no note). The Anki file holds 1,740 cards.
Scenarios are Claude-drafted like the entries; they are never presented
as FAA questions, and each ends at the official text it cites.

Separability (§32.12): deleting the two files removes exactly the Prep
root, the callouts and the `Where to study` sections (`tests/test_study.py`).

## Follow-up: embedding provider

The record format is provider-neutral (`provider.id`/`version` per file).
An embedding-based provider (a local sentence-transformers model or a paid
API) would write the same file with a different provider id; because such
output is not reproducible on the CI runner, the file would be regenerated
manually and committed, with `enrich` validating rather than recomputing
it. The lexical provider stays the default because the daily sync must
rebuild the layer deterministically without models or keys.
