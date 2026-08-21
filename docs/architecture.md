# Architecture

Full specification: `../plans/FAR_AIM_Obsidian_Project_Plan.md` (authoritative).

## Pipeline

```text
authoritative sources (eCFR API, FAA HTML)
    → raw source archive (data/raw/, gitignored cache; see source-policy.md)
    → deterministic parsers (src/far_aim/parsers/)
    → canonical JSON (data/normalized/, source of truth)
    → generated outputs (vault/ Obsidian Markdown; later: search, MCP)
```

Acquisition sequence: check → download → archive raw → hash → parse →
normalize → validate → diff → generate. Never website → Markdown directly.

## Key properties

- **Deterministic:** same raw snapshot + same code ⇒ byte-identical output.
  A no-change rebuild leaves Git clean; generated notes carry no volatile
  timestamps (those live only in `data/manifests/sources.json`).
- **Fail closed:** validation failure blocks publication and preserves the
  last known-good output.
- **Provenance:** every authoritative note records its source and accepted
  version.
- **Separation:** authoritative text is immutable; derived metadata and
  (later) AI enrichment are stored separately and are independently
  rebuildable.

## Package layout (grows per plan §8)

- `far_aim.cli` — command-line entry point (plan §18)
- `far_aim.config` — filesystem layout rooted at the repo root
- `far_aim.manifest` — source-state registry (plan §15)
- Planned: `sources/`, `parsers/`, `models/`, `normalize/`, `links/`,
  `generate/`, `diff/`, `validate/`
