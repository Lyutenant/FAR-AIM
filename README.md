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

- **Phase 1** — eCFR Title 14 acquisition (`far-aim fetch ecfr`): point-in-time
  XML archived with checksum + metadata, validated, recorded in the manifest;
  re-fetching an unchanged issue is a verified no-op.
- **Phase 2** — eCFR parsing (`far-aim parse ecfr`): all 226 parts / 6,363
  sections into deterministic, lossless canonical JSON.
- **Phase 3** — FAR vault (`far-aim build-vault`): 6,772 notes under
  `vault/FAR/`, byte-idempotent, verified by `far-aim validate`.
- **Phase 4** — AIM (`far-aim fetch aim`, `far-aim parse aim`, `build-vault`):
  the current FAA HTML edition is discovered from the FAA publications page,
  archived with all 270 figures, parsed losslessly (432 paragraphs), and
  rendered under `vault/AIM/` with figures embedded as vault assets.
- Next: **Phase 5** — Pilot/Controller Glossary.

## Development

```bash
python3 -m venv .venv                 # requires Python 3.12+
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                      # run tests
.venv/bin/ruff check src tests       # lint
.venv/bin/far-aim check              # show source registry state
.venv/bin/far-aim check --remote     # also poll upstream for newer versions
.venv/bin/far-aim fetch ecfr         # fetch + archive current Title 14
.venv/bin/far-aim parse ecfr         # canonical JSON for the accepted Title 14
.venv/bin/far-aim fetch aim          # fetch + archive current AIM HTML edition + figures
.venv/bin/far-aim parse aim          # canonical JSON for the accepted AIM
.venv/bin/far-aim build-vault        # render vault/FAR and vault/AIM
.venv/bin/far-aim validate           # verify manifest, canonical layers, vault
```

## Layout

- `src/far_aim/` — pipeline package (CLI, config, manifest, sources, parsers, generator)
- `tests/` — pytest suite (`tests/fixtures/` for source fixtures)
- `data/manifests/` — committed source-state registry; `data/raw/` is a gitignored cache
- `vault/` — generated Obsidian vault (`FAR/`, `AIM/` incl. figure assets)
- `plans/` — project specification
- `docs/` — architecture and policy notes
