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

## Current implementation status (Phase 2)

`far-aim parse ecfr` enforces categories 2, 3 and 6 for the eCFR canonical
layer, and all 226 parts of the accepted Title 14 snapshot (6,363 sections —
matching the fetch validator's `DIV8 SECTION` count exactly) parse
losslessly:

- **Structural integrity** — the DIV hierarchy is walked with an explicit
  allowlist per container; any unhandled element or unparseable HEAD raises
  `ParseError`. The document must contain exactly one Title 14 division
  and all content must live inside it (a valid eCFR document for another
  title, or malformed XML with chapters directly under the root, is
  rejected, never republished under the wrong citation), every hierarchy
  division (title, subtitle, chapter, subchapter) must carry exactly one
  heading — those sit outside the per-part lossless check, so a missing
  one, a surplus one the hierarchy walk would otherwise skip, or a stray
  HEAD at the document root fails here instead of silently publishing
  incomplete data — and a nonempty `N` designator (attribute text is
  likewise outside the lossless comparison, so a missing designator would
  silently null the hierarchy context of every part below); section HEAD
  citations are cross-checked against the
  `N` attribute and must carry their citation marker (markerless heads are
  accepted only where the corpus omits them: reserved ranges and CAB-era
  hyphen-numbered sections), section numbers must belong to their part,
  duplicate part
  numbers are rejected, and every stable ID across the built documents
  (parts, sections, appendices) must be unique before anything is
  published.
- **Text integrity** — every part must pass a lossless-capture check (word
  multiset of the source subtree = word multiset of the built document).
  The comparison is strict on whitespace tokens first; a residual mismatch
  is accepted only when the word pieces still balance after splitting on
  the structural glyphs the parser legitimately reshapes — label
  parentheses, joining em/en dashes, range hyphens — **and** every residue
  token carrying one is a recognizable structural join (an embedded valid
  paragraph label, or a dash join anchored on the part's own designators
  or a section citation) or a fragment split off one intact. Reshaped
  prose punctuation ("fixed-wing" → "fixed wing", "(FAA)" → "FAA") anchors
  on neither and fails the gate, exactly like dropped periods, commas,
  semicolons, colons, brackets and quotes. Exact-paragraph fixture tests
  cover representative sections (91.3, 91.155, 91.175, 91.107 italic
  levels).
- **Determinism** — parsing is pure; re-running `parse ecfr` over unchanged
  sources produces byte-identical files (regression-tested).
- **Fail closed** — all requested parts are built in memory before anything
  is written; a failure writes nothing and preserves the last known-good
  normalized output. The archived snapshot is re-verified against the
  manifest `raw_hash` before parsing, and its `metadata.json` must describe
  the accepted snapshot (provider, version, URL, checksum, byte count,
  timestamp format) — metadata left behind by an interrupted forced fetch
  cannot attach false provenance. A full parse publishes the normalized
  layer **transactionally**: the new layer is staged in a sibling directory
  and swapped in with two renames, so an interruption can never leave a mix
  of old and new part files and parts removed upstream cannot survive as
  stale files. Every failure path restores the last known-good layer: a
  crash between the swap renames leaves the sole copy at `.ecfr-previous`,
  which the next publish restores (never discards) before staging; a crash
  between the swap and the manifest commit leaves an uncommitted layer at
  the canonical path with the manifest's layer still under `.ecfr-previous`
  — the next publish (full or `--part`) reconciles both against the
  recorded hash with **deep** per-document re-verification of content
  hashes *and* provenance (stored hashes are not trusted for this
  destructive decision, and canonical hashes exclude `source` blocks, so
  a content-matching layer with stale or tampered provenance — one
  `validate` rejects — cannot win the arbitration and delete the intact
  copy) and reinstates the
  committed one as the surviving fallback; when *neither* copy verifies,
  recovery refuses to discard either and fails closed for human
  inspection; a failed
  swap reinstates it in-process; a failed manifest commit rolls the swap
  back — unless the on-disk manifest shows the commit became visible before
  the failure (rename landed, directory fsync did not), in which case the
  agreeing new layer and manifest are both kept; on a first publish with no
  earlier layer, rollback removes the uncommitted output entirely; when no
  manifest hash exists to arbitrate, recovery reinstates the displaced
  pre-command layer rather than treating it as superseded. Partial
  (`--part`) parses use the same on-disk staging swap (the existing layer
  is hard-link-copied, the requested files rewritten in the copy, and the
  copy swapped in), so a crash, kill, or `KeyboardInterrupt` at any point —
  which an in-memory rollback would not survive — leaves either the old
  layer or the fully-updated one, never a mix. A partial publish first
  deep-verifies the existing layer — every part file's root *and* nested
  documents must recompute their canonical hashes and carry provenance
  matching the *accepted* snapshot — because after a fetch accepts a newer
  issue the stale layer cannot serve as the merge base (that would publish
  a mixed-version corpus), and a carried-over part with stale nested
  provenance would otherwise ride through to a layer `validate` rejects
  (nested `source` blocks are excluded from canonical hashes, so the
  staged title-hash check cannot catch them); a full parse is required
  first. Discarding a superseded `.ecfr-previous` fallback is followed by
  a parent-directory fsync on every path (full, partial, recovery):
  resurrected by a power loss, it could later win recovery arbitration
  once `canonical_hash` is unset or cleared by a newer fetch and displace
  the newer committed layer. A partial publish whose
  staged result would no longer match a recorded title hash (parser changes
  altered that part's canonical content) is refused outright — publishing
  it would leave a layer `validate` rejects while reporting success, and
  re-recording the hash would bless a mixed-provenance layer; a full parse
  is required instead. The entire parse — from
  manifest read through publication — holds the same exclusive lock as
  fetches (`data/manifests/.sources.lock`), so a concurrent fetch cannot
  accept a newer snapshot mid-parse and be overwritten by a stale
  publication. `far-aim validate` takes the same lock around its manifest
  read and layer walk, so it always observes one consistent published
  state rather than a half-committed one.

The manifest `canonical_hash` for `ecfr_title_14` — the content hash over
all part hashes — is recorded only on a successful full-title parse, never
for partial parses. `far-aim validate` then re-verifies the normalized
layer end-to-end: every document of a hashed type — the part and each
nested section and appendix, identified by `document_type` so a *deleted*
hash is a defect rather than a skipped check — must carry a
`canonical_hash` that recomputes from its own content (nested hashes are
excluded from their parent's hash, so the part check alone would miss
stale or missing section hashes) and a `source` block matching the
manifest's accepted snapshot (provider, version, URL, raw checksum,
timestamp format — canonical hashing strips provenance, so nothing else
would notice it missing or falsified; plan §32.8). Each file's root must
itself be a `cfr_part` document (a stripped or mangled root type would
dodge the type-keyed walker while its stored hash still feeds the title
hash), each filename must match the part it contains (a valid document
copied under another part's name is rejected, and no part may appear
twice), and the title hash must match the manifest — so tampered,
duplicated, or stale normalized data fails loudly before vault
generation.

## Current implementation status (Phase 4)

`far-aim fetch aim` enforces category 1 for the FAA AIM HTML edition. The
current edition is discovered from the FAA publications landing page
(`sources.faa_publications`: exactly one HTML listing for the AIM with a
parseable effective date, else discovery fails closed) and cross-checked
against the edition summary printed on the AIM index page itself — a
disagreement means the FAA is mid-update and nothing is accepted. The page
set comes from the index navigation — where any local link the page
grammar does not recognize (a renamed section, a new kind of appendix)
fails the fetch rather than being skipped — and must pass a structural gate: no
two filenames may denote the same chapter/section/appendix (`chap_4.html`
vs `chap_04.html`), chapter pages must match the chapters that have
section pages, each chapter's sections must be contiguous, and the
baseline chapters 0–11 and appendices 1–5 must all be present — a
partially rendered index that drops a whole chapter would otherwise clear
the aggregate floors. Every page must parse as strictly well-formed HTML
(every element closed in order, document ending at the root — the FAA's
DITA output is; a truncated response that still carries every marker is
rejected here rather than accepted with its tail missing) and be HTML with a main content
region (section/appendix pages a content body, chapter pages a contents
list), every figure referenced from a page's `<main>` must live under the
edition's `images/` directory (anything else is a fetch failure, not a
dropped figure) and download as an image, and data-informed floors apply
(≥ 40 pages, ≥ 300 numbered paragraphs, ≥ 150 distinct figures; the
2026-07-09 Change 3 edition has 66 pages, 432 paragraphs, 270 figures).
Content-Length agreement and retries with backoff apply as for the eCFR. The
snapshot (`pages/`, `figures/`, `metadata.json` with per-file checksums) is
archived under `data/raw/aim/{effective-date}-change-{n}/`; the manifest
`raw_hash` is a tree hash over all file checksums, and `verify_snapshot`
re-checks every file (no missing, extra, or altered files; recorded sizes,
byte total, page/figure/paragraph counts recomputed from the archived files
and held to the floors, since `metadata.json` sits outside the tree hash)
— on the freshly downloaded snapshot before it is accepted, before a
cached snapshot is trusted, before parsing, and when an accepted archive is
reconciled after an interrupted acceptance. Filenames differing only by
case are refused (they could not be archived faithfully on every
filesystem). A re-fetch of an accepted
edition whose bytes differ is quarantined (`{version}.mismatch-<hash>`) and
refused without `--force`; a superseded snapshot is preserved as
`{version}.superseded-<hash>` until the manifest commits — even when the
content hash is unchanged, since its `metadata.json` is the last known-good
provenance — and rolled back if the commit verifiably did not happen; an older edition than the accepted one is
refused without `--force`. A cached snapshot is reported as
verified only if its `metadata.json` provenance (label, index URL — outside
the tree hash) still says what the manifest accepted; otherwise the archive
was altered and the fetch fails (restore it, or `--force`). A listing whose
label or index URL the FAA has since changed (same date and change number)
is not a silent no-op either — with or without a cache — and
`check --remote` reports it as an error rather than "up to date": the
manifest keeps the snapshot's own provenance (legacy manifests are
backfilled from the verified snapshot's metadata, never from the live
listing) and the fetch fails until re-run with `--force`. Acceptance order and locking match the eCFR
fetcher. The FAA offers no point-in-time access, so the CLI reminds the
operator to archive each accepted snapshot outside the repository (plan
§6.2).

`far-aim parse aim` enforces categories 2, 3 and 6 for the AIM canonical
layer; the whole 2026-07-09 Change 3 edition (12 chapters, 48 sections, 432
paragraphs, 5 appendices) parses losslessly:

- **Structural integrity** — every page is walked with an explicit
  allowlist of elements (block *and* inline); anything else raises
  `ParseError`. Section pages must carry chapter/section titles agreeing
  with their filename, paragraph headings must match their `id` attribute
  and belong to the page's section in increasing order, appendix pages must
  open with an "Appendix N. <heading>" designation agreeing with their
  filename and title (a swapped or misnamed appendix page cannot be
  published under the wrong citation), the archived page
  set must pass the same structural gate as the index (baseline chapters and
  appendices, contiguous sections, no duplicate identities), the index page's
  own front matter (title, description, edition summary) is allowlisted and
  lossless-checked into the `aim_publication` document, and each
  chapter page's contents list (sections, section headings, paragraph numbers and
  headings) must agree exactly with the section pages — a renumbered or
  dropped paragraph fails the parse instead of publishing. Chapter pages
  are allowlisted and lossless-checked too, so unexpected prose or notices
  on them fail rather than vanish. Every figure referenced must be present
  in the archived snapshot. Stable IDs must be unique across the corpus.
- **Text integrity** — every page must pass the lossless-capture check
  (word multiset of the content region = word multiset of the built
  documents). Exact-text fixture tests cover representative paragraphs
  (4-1-1, 4-1-2, 4-1-4, 4-1-8, 4-1-9, 4-1-15, 4-1-20, chapter 0 and
  appendices 1 and 3).
- **Determinism / fail closed** — the publication machinery is the eCFR's,
  parametrized per corpus (`cli.LayerSpec`): the same staging swap, crash
  recovery under `.aim-previous`, deep re-verification, and manifest
  `canonical_hash` (the hash over all chapter/appendix hashes) recorded only
  after the layer is on disk. `far-aim validate` verifies the AIM layer the
  same way it verifies the eCFR layer (root types `aim_chapter`/
  `aim_appendix`, nested sections and paragraphs, provenance matching the
  manifest's accepted edition — version, effective date, change, raw
  checksum, the manifest-pinned edition label, and a per-document `url`
  that must be the document's *own* page of the pinned index directory —
  chapter/section/appendix identity and paragraph anchor derived from the
  document's type and citation, padded filenames allowed — since canonical
  hashes exclude the `source` block; filename ↔ document agreement).

`far-aim build-vault` / `far-aim validate` extend category 4 and 6 to the
AIM. Both refuse to run while the vault holds generated AIM output but the
manifest's AIM `canonical_hash` is unset — the window between `fetch aim`
accepting a newer edition and `parse aim` publishing it — because a
FAR-only build would otherwise delete the last known-good AIM notes and
figures as "stale" (plan §32.13). Otherwise: stems and aliases are unique across *both* corpora, every generated
wikilink (including `![[figure]]` embeds) resolves, the archived figures are
copied into `vault/AIM/assets/` and byte-verified against the checksums the
canonical layer recorded (the raw snapshot is the preferred source; the
existing vault copy is accepted when it hashes identically, so a lost cache
does not block validation), and rebuilds are byte-idempotent.

## Current implementation status (Phase 5)

`far-aim fetch pcg` enforces category 1 for the FAA PCG HTML edition with
the same structure as the AIM fetcher: discovery from the FAA publications
landing page (exactly one HTML listing), cross-check against the edition
summary printed on the PCG index page itself (two-digit years tolerated —
the FAA prints "Effective: 7/9/26"), the page set from the index's sidebar
letter navigation (an unrecognized link fails; the letter-card grid must
agree with the navigation; baseline letters a–w required — the current
glossary has no X/Y/Z sections; duplicates refused), strict well-formedness
per page, a content-region and term-entry marker check per letter page, and
data-informed floors (≥ 15 pages, ≥ 1,200 term-entry paragraphs; the
2026-07-09 Change 3 edition has 24 pages and 1,562). The snapshot
(`pages/`, `metadata.json` with per-file checksums) is archived under
`data/raw/pcg/{effective-date}-change-{n}/` with a tree-hash `raw_hash`;
`verify_snapshot`, quarantine/superseded handling, listing-pin
reconciliation, rollback refusal and acceptance ordering all match the AIM
fetcher, and the CLI prints the same external-archive reminder (plan §6.2).

`far-aim parse pcg` enforces categories 2, 3 and 6 for the PCG canonical
layer; the whole 2026-07-09 Change 3 edition (23 letters, 1,559 terms —
1,562 entry paragraphs: three terms are published twice with OR-joined
alternative definitions and merge) parses losslessly:

- **Structural integrity** — every letter page is walked with an explicit
  allowlist over the glossary's legacy markup (term entries with and
  without `dfn`, the `CLASS_21` entries missing the entry class,
  cross-reference rows, sub-lists, both note-box markups, `OR`/labelled
  continuation paragraphs); anything else raises `ParseError`. The letter
  heading must match the page's filename letter, term ids must be unique
  across the corpus (uniquely addressable terms, plan §17.2), and a linked
  cross-reference naming an anchor the edition lacks fails the parse.
- **Text integrity** — the full entry paragraph is stored verbatim, and
  every page passes the lossless-capture word-multiset check (navigation
  chrome and the decorative external-row glyphs excluded, like the AIM's
  toggle buttons). Exact-text fixture tests cover representative terms
  (KNOWN TRAFFIC, TRAFFIC PATTERN, OUTER FIX, NAVSPEC, BRAKING ACTION).
- **Determinism / fail closed** — publication uses the same `cli.LayerSpec`
  machinery (staging swap, `.pcg-previous` recovery, deep re-verification,
  manifest `canonical_hash` recorded after the layer is on disk).
  `far-aim validate` verifies the layer like the others (root types
  `pcg_publication`/`pcg_letter`, nested terms, provenance pinned to the
  manifest — including each document's own page URL; a term's anchor is
  upstream layout and not pinned, container documents allow none).

`far-aim build-vault` / `far-aim validate` extend categories 4 and 6 to the
PCG: the fetch-accepted-but-unparsed window refuses to build (as for the
AIM), stems and aliases are unique across all three corpora, every
generated wikilink resolves, and rebuilds are byte-idempotent.

Phase 6 (cross-source links) adds AIM → FAR links: the citation recognizer
(`far_aim.links.citations`) is fixture-tested on verbatim AIM phrasing
(`tests/test_citations.py`), links are emitted only for FAR notes that exist
(`tests/test_aim_generate.py`), and the same zero-broken-links and
byte-idempotency checks cover them. Remaining categories land with their
corresponding phases (change gates and the FAA change-note cross-check:
Phase 8).
