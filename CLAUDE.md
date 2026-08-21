# FAR/AIM Knowledge Vault

Builds and maintains an Obsidian vault from authoritative U.S. aviation sources
(eCFR Title 14, FAA AIM, Pilot/Controller Glossary) via a deterministic pipeline:
raw source → canonical JSON → generated Markdown.

The full specification is `plans/FAR_AIM_Obsidian_Project_Plan.md`. Read it before
making design decisions — it defines the architecture, data model, phased plan,
and hard invariants (§32).

## Rules

1. **Never commit unless explicitly authorized by the user.** Staging, committing,
   and pushing all require authorization for each instance.
2. **Always use the project venv for Python.** Run Python as `.venv/bin/python`
   (and pip as `.venv/bin/pip`). Never use the system/miniforge default `python3`.
   The venv is Python 3.14 (plan requires 3.12+).

## Core invariants (from the plan — do not violate)

- Authoritative source text (CFR/AIM/PCG) is immutable: never paraphrase, "fix",
  summarize, reorder, or omit it. An LLM is never the parser of record —
  parsing is deterministic code.
- Canonical JSON is the source of truth; Markdown is generated output.
- Every authoritative note carries provenance (source + accepted version).
- Rebuilds with unchanged sources must produce zero diff. Generated notes carry
  no volatile timestamps (`retrieved_at` etc. live in the manifest only).
- Generated files are named by stable citation only (`91.155.md`, `4-1-9.md`);
  never embed headings in filenames.
- Never overwrite curated/user-authored notes during regeneration.
- Fail closed: on parser/validation failure, preserve last known-good output.
- `data/raw/` is a gitignored local cache; manifests and checksums are committed.

## Layout

- `plans/` — project specification
- `src/far_aim/` — pipeline package (CLI, config, manifest; parsers arrive in later phases)
- `tests/` — pytest suite; `tests/fixtures/` for source fixtures
- `docs/` — architecture, data-model, source-policy, validation notes
- `AGENTS.md` — agent invariants and settled design decisions
- `.venv/` — Python virtual environment (gitignored)
- Coming in later phases: `data/` (manifests + gitignored raw cache), `vault/`

Before finishing any change: `.venv/bin/ruff check src tests` and
`.venv/bin/pytest` must pass.
