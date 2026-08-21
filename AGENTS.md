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

## Current status

Phase 0 (foundation) complete: package scaffold, CLI skeleton, source manifest,
tests, CI. Next: Phase 1 (eCFR Title 14 acquisition), then Phase 2 (parsing and
canonical model). Do not start AIM/PCG/AI-enrichment work before the eCFR
canonical layer is trustworthy (plan §31).
