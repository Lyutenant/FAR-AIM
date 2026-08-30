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
Next: Phase 5 (Pilot/Controller Glossary). Do not start AI-enrichment work
before Phase 9 (plan §31).
