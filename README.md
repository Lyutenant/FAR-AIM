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
- **Phase 5** — PCG (`far-aim fetch pcg`, `far-aim parse pcg`, `build-vault`):
  the current Pilot/Controller Glossary HTML edition, archived and parsed
  losslessly (23 letters, 1,559 terms), rendered under `vault/PCG/` — one
  note per term, named by the term itself, with See/Refer cross-references
  resolved into wikilinks.
- Next: **Phase 6** — cross-source links (FAR ↔ AIM ↔ PCG).

## Operational notes

- **FAA raw snapshots must be archived outside this repository** (plan §6.2:
  the FAA offers no point-in-time access, so a lost snapshot cannot be
  reconstructed; only its checksums are committed). `far-aim fetch aim`
  prints this reminder on every acceptance. Currently awaiting external
  archive: `data/raw/aim/2026-07-09-change-3/` (~61 MB — pages, figures,
  metadata) and `data/raw/pcg/2026-07-09-change-3/` (~1.1 MB — pages,
  metadata); tree hashes recorded in `data/manifests/sources.json`.
- eCFR snapshots need no external archive: any accepted issue is exactly
  reconstructible from the eCFR point-in-time API given the manifest's
  (version, checksum) pair.

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
.venv/bin/far-aim fetch pcg          # fetch + archive current PCG HTML edition
.venv/bin/far-aim parse pcg          # canonical JSON for the accepted PCG
.venv/bin/far-aim build-vault        # render vault/FAR, vault/AIM and vault/PCG
.venv/bin/far-aim validate           # verify manifest, canonical layers, vault
```

## Layout

- `src/far_aim/` — pipeline package (CLI, config, manifest, sources, parsers, generator)
- `tests/` — pytest suite (`tests/fixtures/` for source fixtures)
- `data/manifests/` — committed source-state registry; `data/raw/` is a gitignored cache
- `vault/` — generated Obsidian vault (`FAR/`, `AIM/` incl. figure assets, `PCG/`)
- `plans/` — project specification
- `docs/` — architecture and policy notes
