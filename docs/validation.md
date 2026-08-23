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

## Current implementation status (Phase 1)

`far-aim validate` validates the source-manifest schema.

`far-aim fetch ecfr` enforces category 1 (source integrity) for the eCFR
download before accepting a snapshot: discovery waits out (and ultimately
fails on) `meta.import_in_progress` so a half-rebuilt daily snapshot is
never accepted; then HTTP 200 + XML content type, Content-Length agreement
(a malformed header is a controlled failure, not a traceback), XML
well-formedness end-to-end, expected `<ECFR>`
root element, and data-informed truncation floors (≥ 4 MB, ≥ 4,000
`DIV8 TYPE="SECTION"` elements; the 2026-08-19 issue measures ~16 MB /
6,363 sections). A re-fetch of an already-accepted version whose bytes no
longer match the recorded checksum fails closed and quarantines the download
for inspection (`--force` overrides after human review; the superseded
snapshot is preserved under a hash-qualified name, since in-place upstream
changes make the old bytes unrecoverable from the point-in-time API).
Acceptance is ordered metadata → archive publish → manifest (atomic commit,
last), so the manifest never records a snapshot that is not durably
archived. If the commit fails, the archive is rolled back in-process to the
preserved last known-good bytes; after a hard crash the next fetch performs
the same rollback offline, *before* discovery and regardless of what version
upstream now reports (validated-but-unaccepted bytes are parked as
`title-14.xml.unaccepted-<hash>`). Snapshot-directory entries are fsynced
after each rename so the archive is durable before the manifest commits.
Fetches take an exclusive non-blocking lock (`data/manifests/.sources.lock`)
for their whole duration and write to unique temp files, so an overlapping
fetch fails closed instead of racing on the manifest or archive. Consumers
should still verify the archive against the manifest `raw_hash` before
trusting it.

Remaining categories land with their corresponding phases (parsers:
Phase 2/4/5; links: Phase 6; change/determinism gates: Phase 8).
