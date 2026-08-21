# Validation

Full specification: plan §17 (requirements), §25 (failure modes).
Principle: **fail closed** — never silently publish incomplete or malformed
regulatory material; on failure, preserve the last known-good output.

## Categories

1. **Source integrity** — download succeeded, expected content type,
   non-empty, version metadata parsed, checksum stored, no truncation.
2. **Structural integrity** — expected Title 14 hierarchy; unique section
   IDs; coherent AIM chapter/section/paragraph tree; uniquely addressable
   PCG terms.
3. **Text integrity** — the parser/generator must not lose or invent
   authoritative text; exact-paragraph fixture tests for representative
   sections.
4. **Link integrity** — generated wikilinks resolve; no duplicate IDs, no
   filename collisions; alias uniqueness enforced **globally** across all
   corpora.
5. **Change integrity** — guardrails against mass changes (e.g., >X% of AIM
   disappearing, thousands of sections changing on a "tiny" update, empty
   parser output). AIM renumbering cascades are classified as moves, not
   mass deletions. Thresholds should be data-informed.
6. **Determinism** — generation runs twice; the second run must produce
   zero diff.
7. **FAA change-note cross-check** — compare detected AIM changes against
   the FAA's published change explanation; flag major discrepancies.

## Current implementation status (Phase 0)

`far-aim validate` validates the source-manifest schema. Categories above
land with their corresponding phases (parsers: Phase 2/4/5; links: Phase 6;
change/determinism gates: Phase 8).
