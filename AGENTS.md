# Agent Instructions — far-aim

Any AI agent working in this repository must follow these instructions. The full
specification is `plans/FAR_AIM_Obsidian_Project_Plan.md`; where this file and the
plan conflict, the plan wins.

## Workflow rules

- **Never commit, stage, or push unless the user explicitly authorizes it.**
- **Always run Python via the project venv** (`.venv/bin/python`, `.venv/bin/pip`).
  Never the system default.
- Before finishing any change: `ruff check src tests` and `pytest` must pass.
- Every parser bug fixed must gain a fixture/regression test.

## Invariants — do not violate (plan §32)

1. Never use an LLM-generated paraphrase as authoritative FAR/AIM content.
2. Never silently omit source material because it is hard to parse.
3. Never modify official wording to make Markdown prettier.
4. Never make Markdown the only canonical representation — canonical JSON is the
   source of truth; Markdown is compiled output.
5. Never overwrite curated/user-authored notes during regeneration.
6. Never treat a raw HTML/XML layout change as automatically equivalent to a
   regulatory content change (compare canonical hashes).
7. Never auto-merge a massive unexplained upstream diff.
8. Every authoritative note must have provenance (source + accepted version).
9. Every parser bug fixed gains a regression test.
10. No-change rebuilds must be idempotent (zero Git diff). Generated notes carry
    no volatile timestamps; those live only in `data/manifests/sources.json`.
11. Explicit authoritative relationships outrank inferred semantic relationships.
12. AI enrichment must remain clearly separable from authoritative data.
13. When uncertain, fail validation and preserve the last known-good output.

## Design decisions already made (do not relitigate)

- Generated notes are named by stable citation only (`91.155.md`, `4-1-9.md`);
  headings never appear in filenames (plan §9 naming policy).
- `data/raw/` is a gitignored local cache; accepted FAA raw snapshots must be
  durably archived outside this repo; eCFR is reconstructible from its
  point-in-time API (plan §6.2).
- `canonical_hash` is computed excluding volatile provenance fields (plan §7).
- Parsing is deterministic code; an LLM is never the parser of record (plan §20.2).
- `data/normalized/` is a gitignored local layer: it is deterministically
  reconstructible from the archived snapshot via `far-aim parse ecfr`, and its
  integrity is pinned by the committed manifest `canonical_hash`.

## Current status

Phase 0 (foundation) and Phase 1 (eCFR Title 14 acquisition) complete:
`far-aim fetch ecfr` discovers the current issue date, downloads and validates
the point-in-time XML, archives it in the gitignored raw cache with checksum +
metadata, and updates the manifest; repeat fetches are verified no-ops.

Phase 2 (eCFR parsing and canonical model) complete: `far-aim parse ecfr`
parses the entire accepted Title 14 snapshot — all 226 parts, 6,363
sections, matching the fetch validator's section count — into deterministic
canonical JSON (`data/normalized/ecfr/part-NNNN.json`), with per-part
lossless-capture verification, unique stable IDs, and the title-level
`canonical_hash` recorded in the manifest on full-title parses.
`far-aim validate` re-verifies the normalized layer against the manifest.
The model, parser grammar (paragraph re-nesting, definitions, re-entry,
gap tolerance), and validation gates are documented in docs/data-model.md,
docs/validation.md, and `far_aim.parsers.ecfr`'s module docstring.

Phase 3 (FAR Obsidian generator) complete: `far-aim build-vault` renders
the verified canonical layer into the committed vault — 6,772 notes (226
part indexes, 6,363 section notes, 181 appendix notes, title index, source
status) under `vault/FAR/`, named by stable citation only, with
schema-checked frontmatter, global alias-collision resolution, and
deterministically extracted in-corpus cross-reference wikilinks (zero
broken links by construction). Rebuilds are idempotent byte-for-byte,
curated notes are never overwritten, and `far-aim validate` byte-compares
the vault against an in-memory re-render. Layout, rendering rules, and
regeneration safety are documented in docs/vault.md.
Phase 4 (AIM acquisition, parser, and vault rendering) complete:
`far-aim fetch aim` discovers the current edition from the FAA publications
page (cross-checked against the AIM index's own edition summary), downloads
every chapter/section/appendix page plus all 270 referenced figures into the
tree-hashed raw snapshot (`data/raw/aim/{date}-change-{n}/`), and records
it in the manifest; `far-aim parse aim` turns the snapshot into canonical
chapter/appendix JSON (`data/normalized/aim/`, plus `publication.json` for
the index page's front matter) — 12 chapters, 48 sections, 432 paragraphs,
5 appendices, every page (index and chapter pages included) lossless, chapter contents
cross-checked against section pages — using the same transactional
publish/recover/verify machinery as the eCFR (`cli.LayerSpec`);
`far-aim build-vault` renders `vault/AIM/` (498 notes + 270 figure assets,
embedded, stems/aliases unique across both corpora) and `far-aim validate`
byte-compares it. Model, grammar and rendering rules: docs/data-model.md,
docs/validation.md, docs/vault.md, `far_aim.parsers.aim`'s docstring.
Operational note: accepted AIM snapshots must be archived outside the repo
(plan §6.2) — the fetcher reminds but does not do it.
Phase 5 (Pilot/Controller Glossary) complete: `far-aim fetch pcg` discovers
the current edition from the FAA publications page (cross-checked against
the PCG index's own edition summary), downloads the index plus all 23
letter pages into the tree-hashed raw snapshot
(`data/raw/pcg/{date}-change-{n}/`) and records it in the manifest;
`far-aim parse pcg` turns the snapshot into canonical letter/publication
JSON (`data/normalized/pcg/`) — 23 letters, 1,559 terms (three OR-joined
duplicates merged), every page lossless, term ids derived from term text
(upstream anchors are non-unique), entry paragraphs stored verbatim,
See/Refer cross-references resolved deterministically (typos left
unresolved, never guessed) — via the same `cli.LayerSpec` machinery;
`far-aim build-vault` renders `vault/PCG/` (1,560 notes named by the term
itself, letters as folders, stems/aliases unique across all three corpora)
and `far-aim validate` byte-compares it. Model, grammar and rendering
rules: docs/data-model.md, docs/validation.md, docs/vault.md,
`far_aim.parsers.pcg`'s docstring. Accepted PCG snapshots must be archived
outside the repo (plan §6.2) like the AIM's.
Phase 6 (cross-source links) in progress — AIM → FAR done: the citation
recognizer (`far_aim.links.citations`) also understands the AIM's house
styles (`14 CFR section 91.171`, `14 CFR § 91.225`, `14CFR §91.113`) and
part references (`14 CFR part 91`, `Parts 91K, 121`, bare `Part 107`), with
other-title bans (`49 CFR part 1542`); `build-vault` appends the resolved
FAR section and part links to each AIM note's `## Explicit Cross-References`
(309 links from 138 notes; derived at render time, canonical JSON unchanged).
FAR → FAR part links done: FAR section/appendix notes list the parts their
text cites as `[[Part 121]]` after the section links, own part excluded
(1,315 links from 704 notes to 133 part indexes). Inside Title 14 text only
CFR-qualified forms count (`part 121 of this chapter`, `Part 375 of this
title`, `14 CFR part 13`; `qualified_only`) because the CFR also writes bare
`Part 1` for an ICAO Annex, an IEC standard or an appendix's own headings —
unqualified `part 119 certificate holder` stays plain; the recognizer rejects
Civil Air Regulations numbering (`CAR Part 3`, `part 4a`), dashed other-code
numbers (`part 60-1`), printed volume ranges (`parts 1 to 59`) and a
document's own subdivisions (`Part 1 of this appendix`, `part 1 of appendix
C`). Deliberately unlinked: bare `section 91.185` without an anchor,
appendix references, FAA typos (`91.113b`). PCG → FAR/AIM done: `Refer to`
rows link `[[Part 91]]`, `[[1.1|§ 1.1]]` or `[[AIM]]` (185 links). AIM → PCG
done (plan §12.2, Tier 2): every AIM note ends with `## Glossary Terms` —
separate from the Tier 1 cross-references — produced by
`far_aim.links.glossary` (phrases case-insensitive; acronyms = parenthetical
or `See`-only entries, capitals only, ≥3 letters; single defined words only
via the gate) and the committed gate `data/links/pcg-glossary-gate.json`
(deny/allow with reasons; stale entries fail the build): 4,677 links from
406 notes to 740 terms. Settled: **FAR notes get no PCG glossary links** —
FAR vocabulary is defined by 14 CFR Part 1 and PCG definitions can differ
(`NIGHT`, `CEILING`, `AIR TAXI`); a FAR → Part 1 definitions layer is the
FAR-side analogue if wanted later. Phase 6 exit criteria met (citation
parser tested, high-confidence links resolve, no mass false positives).
Phase 7 (Obsidian quality layer) complete: generated `vault/Home.md`
(kind `home`; corpus links rendered only for built layers) is the entry
point, linking the corpus indexes, `Source Status`, and three curated
entry notes whose stems (`Collections`, `Topics`, `Study` —
`generate.notes.CURATED_ENTRIES`) are registered as link targets and
reserved in the alias namespace though the generator never writes them.
The curated layer is committed and generator-invisible (no `generated:`
key; `type: curated-index | collection | topic`): `Collections/Private
Pilot/` (13 subject pages per plan §23, links/display-text only — no
restated regulatory text; two page names deviate from §23 because bare
"Airspace"/"Currency" collide with generated aliases — the full-title
test enforces curated-vs-generated name uniqueness) and
`Topics/Cross-Country Flight.md` (the § 61.1(b) definition tiers plus
the per-certificate experience rules built on them, student through
ATP).
Dataview needed no code: generated frontmatter was already typed;
sample queries and the curated-naming guidance (avoid case-fold
collisions with generated stems/aliases) are in docs/vault.md.
`.obsidian/` core config was already committed. Hierarchy presentation
(2026-09-08): official text renders as **nested Markdown list items**
(`generate.hierarchy.list_item`; one `- ` item per CFR paragraph / AIM
or PCG list item, children indented four spaces, relative never absolute,
six levels deep) instead of the earlier flat marker-led paragraphs; the
committed snippet `vault/.obsidian/snippets/far-aim-hierarchy.css`
(enabled in `appearance.json`, scoped by the frontmatter property
`cssclasses: [far-aim-text]` that every official-text note kind carries)
hides bullets, indents per level and draws guide lines in Reading View.
Settled: presentation lives only in CSS (regeneration never needed to
retune it); FAR appendices stay flat because the parser keeps them as
flat block lists; `.obsidian/` remains curated, never generator-written.
Exit criterion met: a student pilot lands on Home and browses to
definition + everything referencing it without knowing repo internals.
Phase 8 (automated maintenance) complete: `far-aim update` (formerly the
Phase 8 stub) runs discovery with the `check --remote` guards (rollback /
altered-pin / network failure ⇒ error, nothing touched), exits cleanly
with zero writes when all sources are current AND the published output is
consistent (no manifest timestamp churn; an update interrupted after
per-source acceptance is detected — pending canonical_hash, a
Source Status note that predates the manifest, a corpus index note
pinning a different canonical_hash (unpublished same-version
correction), or, whenever local
canonical layers exist, a full read-only validation failure — and
resumed, never stranded as a false "nothing to do"; a fresh layerless
checkout of a validated commit is trusted as merged), and otherwise runs
fetch×3 → parse×3 → diff → build-vault → validate,
stopping at the first failure so defects block publication (plan
§32.13); its summary reports per-source version transitions and whether
canonical content changed (provenance-only eCFR issue bumps say
"unchanged"). `--reverify` (CI's mode) re-runs the fetchers even when
versions match, so an FAA content edit that keeps the edition label
fails against the pinned raw_hash instead of going unnoticed; success or
failure, pure last_checked_at drift is restored under the source lock so
a run accepting no content stays byte-clean. `far-aim diff` (formerly
the Phase 2 stub) is the structural-diff step: it re-plans the vault
from the parsed layers and reports adds/rewrites/removals against the
vault on disk, so upstream removals are explicit in the PR log before
publication.
`.github/workflows/upstream-sync.yml` runs `update --reverify` daily:
no-change runs end green with a clean tree; downloaded raw AIM/PCG data
is uploaded as an artifact when the run failed or accepted a real change
(the ephemeral runner may hold the only copy; operator must archive
durably, plan §6.2) but not for a clean re-verification's routine
re-downloads (already archived at first acceptance); real changes re-run
ruff + full pytest and open/update an `upstream-sync` PR
(body: update summary + vault diff stat + full log) — never auto-merged
(plan §32.7). FAA page edits without an edition bump fail the daily
raw_hash re-verification by design (quarantine rides in the artifact);
resolve locally with `fetch --force`. The first scheduled run
(2026-09-05) exposed two CDN behaviours now neutralised in the AIM/PCG
fetchers (`sources.common`): Akamai's per-download script injection is
stripped from HTML before archiving/hashing (CDN instrumentation, not
FAA content), and corpus requests carry a one-off cache-busting query so
a snapshot reflects the FAA origin, not a mix of stale edge caches (the
FAA had re-uploaded 101 AIM figures at higher resolution on 2026-08-18
without an edition bump). Docs: docs/maintenance.md. Exit
criteria are pinned by the `test_update_*` tests in tests/test_cli.py.
Phase 9 (enrichment; design of record plan §36, operator notes
docs/enrichment.md) complete: everything lives under the committed
`data/enrichment/` and is loaded like the glossary gate (malformed ⇒
build error; directory absent ⇒ no enrichment, nothing else changes).
Tier 3: `concepts.json` is a human-curated concept graph (46 Private
Pilot concepts: prerequisites, the FAR/AIM/PCG stems that define each,
curator descriptions that are never regulatory text); `build-vault`
renders the generator-owned `vault/Concepts/` (one note per concept +
`Concept Map.md` with study order and Mermaid prerequisite diagram),
failing on unresolved references, cycles, or a title that collides with
any stem or heading alias. Tier 4: `far-aim enrich` derives
`related.json` (provider `lexical-tfidf` v1 in `links.semantic`:
pure-Python TF-IDF cosine over FAR sections + AIM paragraphs, ≤5 per
corpus, cosine ≥ 0.30, deterministic and idempotent, ~35 s) pinned to
the canonical hashes it was computed from; a stale file fails the
build; `related-review.json` is the human deny list. FAR section and
AIM paragraph notes end with `## Related (derived)` (explicit
cross-references are never repeated there, plan §32.11). `update` runs
`enrich` between `parse` and `diff`; the sync workflow stages
`data/enrichment` in its PR. Settled: an embedding provider is a
documented follow-up (same file format, committed output) because the
daily CI must rebuild the layer without models or keys.
