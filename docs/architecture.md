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
- `far_aim.sources.ecfr` — eCFR Title 14 discovery + acquisition (Phase 1):
  version discovery via `titles.json`, streamed point-in-time XML download
  with retry/backoff, source-integrity gate (well-formedness, root element,
  size/section-count floors), raw archive + checksum, manifest update
- `far_aim.sources.common` — acquisition helpers shared by every fetcher:
  polite HTTP client, retries honoring `Retry-After`, directory fsync, the
  inter-process source lock, checksums
- `far_aim.sources.faa_publications` — FAA publications landing page →
  current AIM/PCG edition (label, change number, effective date, HTML URL)
- `far_aim.sources.aim` — AIM acquisition (Phase 4): index-driven page set,
  figure download, source-integrity gate, tree-hashed snapshot archive
  (`pages/`, `figures/`, `metadata.json`), manifest update
- `far_aim.sources.pcg` — PCG acquisition (Phase 5): index-driven letter-page
  set, source-integrity gate, tree-hashed snapshot archive (`pages/`,
  `metadata.json`), manifest update — same transactional machinery as the AIM
- `far_aim.htmltree` — minimal deterministic HTML DOM (stdlib `html.parser`)
  used by the FAA parsers
- `far_aim.models.cfr` / `far_aim.models.aim` / `far_aim.models.pcg` —
  stable IDs + canonical content hashing (Phases 2, 4, 5)
- `far_aim.parsers.ecfr` — eCFR XML → canonical part JSON (Phase 2)
- `far_aim.parsers.aim` — FAA AIM HTML → canonical chapter/appendix JSON
  (Phase 4): allowlisted grammar, chapter-contents cross-check, per-page
  lossless capture, explicit-reference resolution
- `far_aim.parsers.pcg` — FAA PCG HTML → canonical letter/publication JSON
  (Phase 5): allowlisted grammar over the glossary's legacy markup,
  text-derived stable term IDs, per-page lossless capture, See/Refer
  cross-reference resolution
- `far_aim.generate` — canonical JSON → Obsidian vault (Phases 3–5): naming
  policy, deterministic frontmatter, block renderers (`markdown` for the
  CFR, `aim_markdown` for the AIM), note builders (`notes`, `aim_notes`,
  `pcg_notes`), and the plan/verify/sync build with figure assets (see
  vault.md)
- `far_aim.links.citations` — deterministic in-text CFR citation extraction
  (Phase 3, plan §12.1 Tier 1)
- `far_aim.cli` — one publish/recover/verify path for every normalized
  layer, parametrized by `LayerSpec`
- Planned: `normalize/`, `diff/`, `validate/`
