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
- **Phase 6 (in progress)** — cross-source links. AIM → FAR: every AIM
  note's `## Explicit Cross-References` also lists the FAR sections and
  parts its official text cites (`14 CFR section 91.171`, `14 CFR part 91`,
  `Part 107 operations`), linked only when the FAR note exists — 309 links
  from 138 AIM notes to 90 sections and 31 parts. FAR → FAR parts: section
  and appendix notes also link the parts they cite (`part 121 or part 135
  of this chapter`; CFR-qualified forms only) — 1,315 links from 704 notes to
  133 part indexes. PCG → FAR/AIM: glossary `Refer to` rows link the part,
  section or AIM index they name (185 links). AIM → PCG: every AIM note
  ends with `## Glossary Terms`, the Pilot/Controller Glossary terms its
  text uses, under a deterministic, human-gated recognizer
  (`data/links/pcg-glossary-gate.json`) — 4,677 links from 406 notes to 740
  terms. The FAR deliberately gets no glossary links (its vocabulary is
  defined by 14 CFR Part 1).

- **Phase 7** — Obsidian quality layer: generated `Home.md` entry point plus
  the committed curated study layer (`Collections/Private Pilot/`, `Topics/`).
- **Phase 8** — automated maintenance: `far-aim update` and the daily
  `upstream-sync` Action that opens a reviewable PR on any upstream change.
- **Phase 9** — enrichment (`far-aim enrich`, `build-vault`): a curated
  concept graph (46 Private Pilot concepts with prerequisites, rendered
  under `vault/Concepts/` with a Concept Map and Mermaid prerequisite
  diagram) and deterministic "related" suggestions between FAR sections
  and AIM paragraphs (pure-Python TF-IDF, ~24,100 links, human deny-list),
  rendered as a clearly labelled `## Related (derived)` section. Fully
  separable from the authoritative layers — see docs/enrichment.md.

## Using the vault

Open `vault/` as an Obsidian vault and start at `Home.md`: it explains what
each folder holds (generated `FAR/`, `AIM/`, `PCG/`, `Concepts/` versus the
curated `Collections/`, `Topics/`, `Study/`), where to start studying, how
to read a note, how to search by citation or heading, and how to write your
own notes without them being overwritten by a rebuild.

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
.venv/bin/far-aim enrich             # derive data/enrichment/related.json (Phase 9)
.venv/bin/far-aim build-vault        # render vault/FAR, AIM, PCG and Concepts
.venv/bin/far-aim validate           # verify manifest, canonical layers, vault
.venv/bin/far-aim update             # the whole sequence, only when upstream changed
```

## Layout

- `src/far_aim/` — pipeline package (CLI, config, manifest, sources, parsers, generator)
- `tests/` — pytest suite (`tests/fixtures/` for source fixtures)
- `data/manifests/` — committed source-state registry; `data/links/` — committed,
  human-maintained link curation (the PCG glossary gate); `data/raw/` is a gitignored cache
- `vault/` — generated Obsidian vault (`FAR/`, `AIM/` incl. figure assets, `PCG/`)
- `plans/` — project specification
- `docs/` — architecture and policy notes

## License

The pipeline code, tests, documentation, and the curated vault notes
(`vault/Collections/`, `vault/Topics/`, `vault/Study/`, `vault/Home.md`) are
released under the [MIT License](LICENSE).

The generated notes under `vault/FAR/`, `vault/AIM/` and `vault/PCG/` reproduce
the text and figures of the eCFR Title 14, the FAA Aeronautical Information
Manual and the Pilot/Controller Glossary. These are works of the United States
Government and are in the public domain (17 U.S.C. § 105); the MIT License
does not apply to them and grants no rights over them. They are provided for
reference only — the official publications remain the authoritative source,
and `vault/Source Status.md` records which edition each note was built from.
