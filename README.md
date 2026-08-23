# far-aim

A deterministic pipeline that builds and continuously maintains an Obsidian
vault from authoritative U.S. aviation sources:

- **FAR** — Title 14 of the eCFR (official eCFR API)
- **AIM** — FAA Aeronautical Information Manual (FAA HTML edition)
- **PCG** — Pilot/Controller Glossary (FAA HTML edition)

Architecture: `raw source → canonical JSON → generated Markdown`. The canonical
JSON layer, not Markdown, is the source of truth. Authoritative text is never
paraphrased or AI-generated. Full specification:
[`plans/FAR_AIM_Obsidian_Project_Plan.md`](plans/FAR_AIM_Obsidian_Project_Plan.md).

## Status

**Phase 1** — eCFR Title 14 acquisition: `far-aim fetch ecfr` discovers the
current issue via the eCFR versioner API, streams the point-in-time XML into
the raw cache with checksum + metadata, validates it (well-formedness,
truncation floors), and records the accepted version in the source manifest.
Re-fetching an unchanged issue is a verified no-op. `far-aim check --remote`
polls upstream read-only. Parsing (Phase 2) is next.

## Development

```bash
python3 -m venv .venv                 # requires Python 3.12+
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                      # run tests
.venv/bin/ruff check src tests       # lint
.venv/bin/far-aim check              # show source registry state
.venv/bin/far-aim check --remote     # also poll upstream for newer versions
.venv/bin/far-aim fetch ecfr         # fetch + archive current Title 14
```

## Layout

- `src/far_aim/` — pipeline package (CLI, config, manifest; parsers arrive in later phases)
- `tests/` — pytest suite (`tests/fixtures/` for source fixtures)
- `data/manifests/` — committed source-state registry; `data/raw/` is a gitignored cache
- `vault/` — generated Obsidian vault (Phase 3+)
- `plans/` — project specification
- `docs/` — architecture and policy notes
