# Data Model

Full specification: plan §7 (canonical model), §15 (manifest).

## Canonical objects (Phase 2+)

Normalized units (CFR sections, AIM paragraphs, PCG terms) are JSON objects
with:

- **stable IDs** (`cfr-14-91.155`, `aim-4-1-9`, `pcg-controlled-airspace`) —
  never derived from mutable titles or filenames
- **provenance** (`source` block: provider, accepted version/effective date,
  URL, raw checksum)
- **`canonical_hash`** — computed over content **excluding** volatile
  provenance fields (`retrieved_at`, URLs), so markup-only re-fetches are
  detectable as content-identical (plan §14.4)

## Source manifest (implemented)

`data/manifests/sources.json`, schema version 1:

```json
{
  "schema_version": 1,
  "sources": {
    "ecfr_title_14": {
      "last_checked_at": "2026-08-20T10:05:13Z",
      "accepted_version": "2026-08-19",
      "raw_hash": "...",
      "canonical_hash": "..."
    },
    "aim": {
      "last_checked_at": "...",
      "effective_date": "2026-07-09",
      "change": 3,
      "raw_hash": "...",
      "canonical_hash": "..."
    },
    "pcg": { "...": "same shape as aim" }
  }
}
```

Rules:

- The manifest is the **only** home for volatile timestamps; generated notes
  never carry them (plan §3.4).
- Serialization is deterministic: sorted keys, 2-space indent, trailing
  newline, `None` fields omitted (`far_aim.manifest`).
- Unknown fields and wrong types fail validation loudly (`ManifestError`).
