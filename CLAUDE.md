# FAR/AIM Knowledge Vault

Builds and maintains an Obsidian vault from authoritative U.S. aviation sources
(eCFR Title 14, FAA AIM, Pilot/Controller Glossary) via a deterministic pipeline:
raw source → canonical JSON → generated Markdown.

The full specification is `plans/FAR_AIM_Obsidian_Project_Plan.md`. Read it before
making design decisions — it defines the architecture, data model, phased plan,
and hard invariants (§32).

Workflow rules, invariants, and settled design decisions are maintained in one
place and imported here:

@AGENTS.md

## Layout

- `plans/` — project specification
- `src/far_aim/` — pipeline package (CLI, config, manifest; parsers arrive in later phases)
- `tests/` — pytest suite; `tests/fixtures/` for source fixtures
- `docs/` — architecture, data-model, source-policy, validation notes
- `data/manifests/sources.json` — committed source registry; `data/links/` — committed
  link curation (PCG glossary gate); `data/raw/` is a gitignored cache
- `.venv/` — Python virtual environment (gitignored)
- Coming in later phases: `vault/` (generated Obsidian vault)
