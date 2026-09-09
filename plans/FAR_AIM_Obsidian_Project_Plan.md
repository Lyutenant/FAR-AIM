# FAR/AIM Knowledge Vault — Project Plan and Build Specification

> **Working project name:** `far-aim-vault`  
> **Primary goal:** Build and continuously maintain a high-quality, version-controlled Obsidian vault from authoritative U.S. aviation regulatory and guidance sources, with a canonical structured-data layer that can later power AI, search, web, mobile, MCP, and study applications.
>
> **Status of source assumptions:** Verified against official eCFR and FAA publication pages on 2026-08-19.

---

## 1. Executive Summary

This project converts the FAR/AIM ecosystem into a structured, navigable, updateable knowledge base.

The project is **not** simply “download some pages and convert them to Markdown.” Its architecture should be:

```text
                    AUTHORITATIVE SOURCES
                           │
              ┌────────────┴────────────┐
              │                         │
           eCFR                     FAA Publications
         Title 14                 AIM + PCG (HTML)
              │                         │
              └────────────┬────────────┘
                           ▼
                    RAW SOURCE ARCHIVE
                           ▼
                DETERMINISTIC PARSERS
                           ▼
                 CANONICAL DATA MODEL
                       (JSON)
                           ▼
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         Obsidian      Search/Index    AI/MCP
          Vault          Database       Layer
```

The **canonical structured representation**, not Markdown, is the source of truth for generated content.

Markdown is a compiled output optimized for Obsidian.

AI agents may write code, propose relationships, classify material, and build study features, but must **never silently modify, paraphrase, summarize, or reconstruct authoritative source text**.

---

# 2. Project Goals

## 2.1 Primary goals

Build a system that:

1. Acquires authoritative FAR/AIM source material from government sources.
2. Preserves the source material and its hierarchy.
3. Normalizes the material into a stable internal representation.
4. Generates a well-structured Obsidian vault.
5. Adds deterministic cross-links where references are explicit.
6. Supports useful Obsidian features such as:
   - `[[wikilinks]]`
   - backlinks
   - properties/YAML frontmatter
   - aliases
   - tags
   - local graph / graph view
   - search
   - embedded notes/transclusion
   - block/heading links where useful
   - Dataview-compatible metadata
7. Detects upstream changes automatically.
8. Produces human-reviewable diffs when FAR/AIM sources change.
9. Keeps revision history in Git.
10. Makes the structured data reusable by future applications.

## 2.2 Long-term goals

The same canonical dataset should eventually be able to support:

- Obsidian vault
- web reference site
- semantic search
- RAG
- AI aviation tutor
- checkride study mode
- flashcard generation
- scenario-based questions
- citation-aware Q&A
- MCP server
- mobile application
- rating-specific study collections
- regulatory change alerts

## 2.3 Non-goals for the initial MVP

Do **not** begin by building:

- an AI chatbot
- embeddings/vector search
- a mobile app
- a web frontend
- sophisticated semantic knowledge graphs
- automatically generated regulatory explanations
- large quantities of AI-created study content

The first objective is a **trustworthy data pipeline and excellent vault**.

---

# 3. Core Engineering Principles

## 3.1 Authoritative source text is immutable

Official regulatory/guidance text must be stored exactly as parsed from the authoritative source, subject only to deterministic formatting transformations required for Markdown.

The system must distinguish:

```text
AUTHORITATIVE
- CFR text
- AIM text
- Pilot/Controller Glossary text
- headings
- citations
- tables
- notes
- official cross-references

DERIVED
- metadata
- normalized identifiers
- explicit wikilinks
- indexes
- collections
- change summaries

AI-ENRICHED
- related concepts
- likely study relevance
- explanations
- scenario questions
- common misconceptions
- semantic relationships
```

Never merge AI-generated prose into the authoritative text body.

## 3.2 Markdown is generated output, not the database

Do not make hand-edited Markdown files the only source of truth.

Preferred flow:

```text
raw HTML/XML
    ↓
canonical JSON
    ↓
Markdown
```

This allows the entire vault to be regenerated deterministically.

## 3.3 Preserve raw sources

Every accepted source version should be archived before parsing.

This allows:

- reproducibility
- debugging
- parser regression analysis
- verification
- historical reconstruction

## 3.4 Deterministic builds

Given the same raw source snapshot and code version, the pipeline should produce the same normalized data and Markdown output.

A rebuild with no source or code changes should leave Git clean.

Consequence: volatile fetch metadata (`retrieved_at`, `last_checked_at`) must **never** be embedded in generated notes — otherwise an unchanged re-fetch touches every file. Volatile timestamps live only in the source manifest (§15). Per-note provenance references the **accepted source version**, which changes only when content actually changes.

## 3.5 Traceability

Every generated authoritative note must indicate where it came from and which source version it represents.

## 3.6 Fail loudly

If parsing assumptions break, validation should fail.

Do not silently publish incomplete or malformed regulatory material.

---

# 4. Scope of the Corpus

## 4.1 FAR / regulatory corpus

For the underlying corpus, ingest **all of Title 14 of the eCFR** rather than deciding at ingestion time which regulations a private pilot “needs.”

Title 14 contains much more than the pilot-oriented material commonly printed in commercial FAR/AIM books.

That is desirable at the data layer.

Be aware of what “all of Title 14” actually includes: Chapter I (FAA — the “FARs”), Chapter II (DOT economic regulations), Chapter III (commercial space transportation), and Chapter V (NASA). Expect on the order of 15,000–20,000 sections and a similarly sized vault. Obsidian handles this note count, but global graph view and initial indexing degrade at that scale — the vault design must not depend on the global graph (see §24).

Later, expose filtered collections such as:

```text
Private Pilot
Instrument Rating
Commercial Pilot
CFI
ATP
Aircraft Maintenance
UAS
Part 135
Part 121
```

A collection is a view over the corpus, not a separate copy of the source.

## 4.2 AIM

Ingest the complete current FAA Aeronautical Information Manual from the FAA-published HTML edition.

The AIM is heavily figure-dependent (airspace diagrams, light-gun signal tables, phraseology examples). **Figures are part of the corpus.** At acquisition time, download every referenced image, archive it with the raw snapshot, store it as a vault asset, and embed it in the generated notes. Do not hotlink FAA image URLs (they rot on site redesigns) and do not silently drop figures. FAA material is public domain, so embedding is not a licensing problem.

## 4.3 Pilot/Controller Glossary

Ingest the complete current Pilot/Controller Glossary from FAA HTML.

This should be a separate corpus with stable glossary-term identifiers.

## 4.4 Later additions

Do not block the initial project on these, but design the data model so they can be added later:

- Airman Certification Standards (ACS)
- Pilot's Handbook of Aeronautical Knowledge (PHAK)
- Airplane Flying Handbook (AFH)
- Aviation Weather Handbook
- relevant Advisory Circulars
- FAA Orders
- Aeronautical Information Publication (AIP)
- FAA Safety Team material
- selected NTSB material
- selected 49 CFR / TSA provisions relevant to pilots

The eventual project may evolve from a “FAR/AIM vault” into a broader **FAA aviation knowledge platform**.

---

# 5. Authoritative Upstream Sources

## 5.1 FAR: eCFR

Use the official eCFR developer/API system as the primary current regulatory source.

Official developer documentation:

`https://www.ecfr.gov/developers/documentation/api/v1`

Useful characteristics include:

- structured eCFR content
- point-in-time/version-aware access
- Recent Changes
- Corrections
- structured hierarchy
- ability to retrieve regulatory content without scraping commercial websites

The pipeline should preserve the hierarchy:

```text
Title
└── Subtitle
    └── Chapter
        └── Subchapter
            └── Part
                ├── Subpart
                │   └── Section
                │       └── Paragraph hierarchy
                └── Appendix
```

Not every level appears everywhere — Title 14 currently has no Subtitle level — so the parser must tolerate absent intermediate levels rather than assume a fixed depth.

### Important legal/source distinction

The eCFR is the preferred source for a living/current knowledge product, but the system should preserve source metadata and should not represent itself as providing legal advice or as replacing the official published legal sources.

A future archival/verification layer may also use annual CFR/GovInfo material and Federal Register references.

## 5.2 AIM and Pilot/Controller Glossary: FAA

Use the FAA Air Traffic Plans and Publications page as the discovery/version source:

`https://www.faa.gov/air_traffic/publications/`

The FAA publication page exposes:

- current AIM edition/change
- effective date
- HTML link
- PDF link
- individual change publications
- current Pilot/Controller Glossary edition/change
- corresponding effective dates

As verified on 2026-08-19, the page listed:

- AIM Basic with Changes 1, 2 and 3 — effective 2026-07-09
- Pilot/Controller Glossary Basic with Changes 1, 2 and 3 — effective 2026-07-09

The pipeline should **discover the current HTML edition from the FAA publication page**, rather than hard-coding an assumption that a specific dated AIM URL will remain current forever.

## 5.3 HTML vs PDF

For AIM and PCG:

**Primary ingestion:** HTML  
**Secondary validation/fallback:** PDF only when useful

Do not make PDF extraction the normal ingestion path.

HTML is preferable because it preserves more of the document hierarchy and avoids OCR/PDF-layout problems.

## 5.4 Do not scrape commercial FAR/AIM publishers

Do not use ASA, Sporty's, Gleim, or other commercial FAR/AIM products as the canonical source.

Government sources should be authoritative upstream inputs.

---

# 6. Acquisition Strategy

The acquisition sequence must always be:

```text
CHECK
  ↓
DOWNLOAD
  ↓
ARCHIVE RAW
  ↓
HASH
  ↓
PARSE
  ↓
NORMALIZE
  ↓
VALIDATE
  ↓
DIFF
  ↓
GENERATE
```

Never:

```text
website → Markdown directly
```

## 6.1 Raw archive layout

Example:

```text
data/
├── raw/
│   ├── ecfr/
│   │   └── 2026-08-19/
│   │       ├── title-14.xml
│   │       └── metadata.json
│   │
│   ├── aim/
│   │   └── 2026-07-09-change-3/
│   │       ├── pages/
│   │       ├── figures/
│   │       └── metadata.json
│   │
│   └── pcg/
│       └── 2026-07-09-change-3/
│           ├── pages/
│           └── metadata.json
│
├── normalized/
│   ├── far/
│   ├── aim/
│   └── pcg/
│
└── manifests/
    └── sources.json
```

## 6.2 Raw snapshot storage policy (decided up front)

Raw snapshots are **not committed to the main Git repository**. Title 14 XML alone is on the order of 50+ MB per snapshot; committing snapshots would permanently balloon the repository. Manifests and checksums, by contrast, are always committed.

- `data/raw/` is gitignored and acts as a local cache.
- **eCFR:** raw XML is reproducibly re-fetchable via the eCFR point-in-time API, so the (source version, checksum) pair in the manifest is sufficient for exact reconstruction. No separate archive is required.
- **AIM / PCG:** the FAA provides no point-in-time access, so every *accepted* raw snapshot must be durably archived outside the main repo — a separate archive repository, Git LFS, or object storage. For FAA sources this archive is required, not optional; without it, parser regression analysis against historical editions becomes impossible.

---

# 7. Canonical Data Model

The exact implementation can evolve, but normalized objects need stable IDs and explicit provenance.

In canonical JSON, `source.retrieved_at` records when the accepted snapshot was fetched. `canonical_hash` must be computed over content **excluding** volatile provenance fields (`retrieved_at`, URLs), so that markup-only re-fetches are detectable as content-identical (§14.4). Generated Markdown carries no volatile timestamps at all (§3.4).

## 7.1 FAR section example

```json
{
  "id": "cfr-14-91.155",
  "document_type": "cfr_section",
  "title_number": 14,
  "part": "91",
  "subpart": "B",
  "section": "91.155",
  "heading": "Basic VFR weather minimums",
  "source": {
    "provider": "ecfr",
    "retrieved_at": "2026-08-19T00:00:00Z",
    "source_version": "2026-08-19",
    "url": "...",
    "raw_checksum": "..."
  },
  "paragraphs": [
    {
      "id": "cfr-14-91.155-a",
      "label": "(a)",
      "text": "...",
      "children": []
    }
  ],
  "explicit_references": [],
  "canonical_hash": "..."
}
```

## 7.2 AIM paragraph example

```json
{
  "id": "aim-4-1-9",
  "document_type": "aim_paragraph",
  "chapter": 4,
  "section": 1,
  "paragraph": "4-1-9",
  "heading": "...",
  "source": {
    "provider": "faa",
    "effective_date": "2026-07-09",
    "change": 3,
    "retrieved_at": "...",
    "url": "...",
    "raw_checksum": "..."
  },
  "content": [],
  "explicit_references": [],
  "canonical_hash": "..."
}
```

## 7.3 Glossary entry example

```json
{
  "id": "pcg-controlled-airspace",
  "document_type": "pcg_term",
  "term": "CONTROLLED AIRSPACE",
  "aliases": [],
  "definition": "...",
  "see_also": [],
  "source": {
    "provider": "faa",
    "effective_date": "2026-07-09",
    "change": 3
  }
}
```

## 7.4 Stable identifiers

Identifiers must remain stable even if:

- filenames change
- folder structure changes
- headings receive cosmetic corrections
- Obsidian display names change

Do not use a mutable title as the sole primary key.

---

# 8. Repository Structure

Recommended starting layout:

```text
far-aim-vault/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── .gitignore
│
├── docs/
│   ├── architecture.md
│   ├── data-model.md
│   ├── source-policy.md
│   └── validation.md
│
├── src/
│   └── far_aim/
│       ├── __init__.py
│       ├── cli.py
│       │
│       ├── sources/
│       │   ├── ecfr.py
│       │   ├── faa_publications.py
│       │   ├── aim.py
│       │   └── pcg.py
│       │
│       ├── parsers/
│       │   ├── ecfr.py
│       │   ├── aim.py
│       │   └── pcg.py
│       │
│       ├── models/
│       │   ├── common.py
│       │   ├── cfr.py
│       │   ├── aim.py
│       │   └── pcg.py
│       │
│       ├── normalize/
│       │   ├── cfr.py
│       │   ├── aim.py
│       │   └── pcg.py
│       │
│       ├── links/
│       │   ├── citations.py
│       │   ├── glossary.py
│       │   └── semantic.py
│       │
│       ├── generate/
│       │   ├── markdown.py
│       │   ├── indexes.py
│       │   └── manifests.py
│       │
│       ├── diff/
│       │   └── canonical.py
│       │
│       └── validate/
│           ├── source.py
│           ├── structure.py
│           ├── links.py
│           └── generated.py
│
├── tests/
│   ├── fixtures/
│   ├── test_ecfr_parser.py
│   ├── test_aim_parser.py
│   ├── test_pcg_parser.py
│   ├── test_normalization.py
│   ├── test_links.py
│   └── test_generation.py
│
├── data/
│   ├── raw/          # gitignored local cache — see storage policy (§6.2)
│   ├── normalized/
│   └── manifests/
│
├── vault/
│
└── .github/
    └── workflows/
        ├── test.yml
        └── update-sources.yml
```

The final structure may differ, but separation of concerns should remain.

---

# 9. Obsidian Vault Architecture

Suggested generated vault:

```text
vault/
├── Home.md
├── Source Status.md
│
├── FAR/
│   ├── Title 14.md
│   ├── Part 001/
│   ├── Part 021/
│   ├── Part 061/
│   │   ├── Part 61.md
│   │   ├── 61.1.md
│   │   ├── 61.3.md
│   │   └── ...
│   ├── Part 091/
│   └── ...
│
├── AIM/
│   ├── AIM.md
│   ├── Chapter 1 - Air Navigation/
│   ├── Chapter 2 - Aeronautical Lighting/
│   ├── Chapter 3 - Airspace/
│   └── ...
│
├── Pilot-Controller-Glossary/
│   ├── Index.md
│   ├── A/
│   ├── B/
│   └── ...
│
├── Collections/
│   ├── Private Pilot.md
│   ├── Instrument Rating.md
│   ├── Commercial Pilot.md
│   └── ...
│
├── Topics/
│   └── ...
│
└── Study/
    └── ...
```

### File naming policy (link stability)

Name generated authoritative notes by **stable citation only** — never embed the heading in the filename:

```text
FAR:  91.155.md
AIM:  4-1-9.md
PCG:  Controlled Airspace.md   (the term itself is the citation)
```

Rationale: curated/user-authored notes link by filename, and the generator must never rewrite them (§32). If filenames embedded headings, an upstream cosmetic heading correction would rename generated files and silently break every curated link pointing at them. Human-friendly navigation comes instead from aliases, the in-note H1, and index notes that use display text:

```markdown
[[91.155|§ 91.155 — Basic VFR weather minimums]]
```

### Generated vs curated material

Prefer clear separation.

Example:

```text
vault/
├── generated/
│   ├── FAR/
│   ├── AIM/
│   └── PCG/
│
└── curated/
    ├── Topics/
    ├── Study/
    └── Collections/
```

Alternatively use a single friendly hierarchy but include metadata such as:

```yaml
generated: true
```

The system must avoid overwriting user-authored notes.

### Vault configuration

Commit a minimal curated `.obsidian/` configuration (core settings only; no community plugins required for basic use) so the vault opens well on first launch. Gitignore volatile workspace/state files (`workspace.json`, caches).

---

# 10. Markdown Note Design

## 10.1 FAR note example

```markdown
---
id: cfr-14-91.155
type: regulation
citation: "14 CFR § 91.155"
title_number: 14
part: 91
section: "91.155"
source: ecfr
source_version: 2026-08-19
canonical_hash: "..."
generated: true
aliases:
  - Basic VFR Weather Minimums
tags:
  - far
  - regulation
---

# § 91.155 — Basic VFR weather minimums

> [!info] Source
> Current eCFR source metadata goes here.

## Official Text

...

## Explicit Cross-References

- [[...]]

## Related Glossary Terms

- [[...]]

## Derived Relationships

<!-- Keep non-authoritative/AI-derived relationships visually separate. -->
```

Note that the frontmatter contains **no volatile fetch timestamps** (`retrieved_at`, `last_checked`). Per-note provenance references only the accepted `source_version`, so an unchanged re-fetch produces zero note diffs (§3.4). Volatile timestamps belong in the manifest (§15).

## 10.2 AIM note example

```markdown
---
id: aim-4-1-9
type: aim
chapter: 4
section: 1
paragraph: "4-1-9"
effective_date: 2026-07-09
change: 3
source: faa
generated: true
---

# AIM 4-1-9 — <Heading>

## Official Text

...

## Explicit Cross-References

...
```

## 10.3 PCG note example

```markdown
---
id: pcg-controlled-airspace
type: glossary
term: Controlled Airspace
source: faa
effective_date: 2026-07-09
change: 3
generated: true
aliases:
  - CONTROLLED AIRSPACE
---

# Controlled Airspace

<official definition>

## See Also

...
```

---

# 11. Obsidian Features to Exploit

## 11.1 Wikilinks

Use citation-named targets (per the §9 file naming policy):

```markdown
[[91.155]]
```

with display text where a friendlier label helps:

```markdown
[[91.155|§ 91.155 — Basic VFR weather minimums]]
```

Wikilinks should primarily be generated from known identifiers, not fuzzy filename guessing.

## 11.2 Backlinks

Backlinks are automatic once wikilinks exist.

This is one of the core benefits of the vault.

## 11.3 Properties / YAML

Every generated note should contain structured metadata.

This enables:

- filtering
- study collections
- Dataview
- revision/status displays
- future programmatic use

## 11.4 Aliases

Use aliases for common names and citation forms.

Example:

```yaml
aliases:
  - "§ 91.155"
  - "14 CFR 91.155"
  - "Basic VFR Weather Minimums"
```

Avoid ambiguous aliases when they create collisions.

## 11.5 Tags

Use tags sparingly for stable broad categories.

Good:

```text
#far
#aim
#glossary
#regulation
```

Do not use tags as a substitute for a proper structured concept model.

## 11.6 Dataview compatibility

Metadata should allow queries such as:

```dataview
TABLE citation, title
FROM "FAR"
WHERE part = 91
SORT section ASC
```

Dataview should be treated as an optional enhancement, not a requirement for basic vault usability.

## 11.7 Graph and Local Graph

The graph becomes useful only when links represent real relationships.

Do not add thousands of low-quality AI links just to create a visually dense graph.

## 11.8 Transclusion

Generated topic/study notes may embed authoritative sections using:

```markdown
![[61.57]]
```

Prefer transclusion to copying regulatory text into many notes.

## 11.9 Heading/block links

Where technically stable, permit links to:

```text
[[91.155#(b)]]
```

or equivalent normalized anchors.

Do not make fragile sentence-level block IDs a hard dependency in the first version.

---

# 12. Linking Strategy

Links have different confidence/authority levels and must not be mixed indiscriminately.

## 12.1 Tier 1 — explicit authoritative references

Highest confidence.

Examples:

- CFR text explicitly citing another CFR section
- AIM citing a FAR
- AIM cross-reference to another AIM paragraph
- glossary “See ...” references

These should be parsed deterministically and converted into links.

## 12.2 Tier 2 — deterministic lexical links

Examples:

- recognized Pilot/Controller Glossary terms
- exact defined-term references

Use only when ambiguity can be controlled.

## 12.3 Tier 3 — curated relationships

Human-maintained or rule-based:

```text
Class D Airspace
→ §91.129
→ §91.155
→ relevant AIM paragraphs
```

Store separately from authoritative references.

## 12.4 Tier 4 — AI-suggested relationships

Examples:

- semantically related regulations
- prerequisite concepts
- “commonly confused with”
- checkride relevance

These must:

- be visibly identified as derived/AI-assisted
- never alter official text
- preferably carry confidence/provenance metadata
- be reviewable
- be removable/rebuildable independently

---

# 13. Collections and Study Views

Because the raw corpus includes all Title 14, create metadata-driven collections rather than deleting “irrelevant” regulations.

Example:

```yaml
collection: private-pilot
includes:
  - cfr-14-1
  - cfr-14-61
  - cfr-14-67
  - cfr-14-68
  - cfr-14-71
  - cfr-14-73
  - cfr-14-91
```

The actual mapping should eventually be more granular than whole parts.

Possible collections:

- Student Pilot
- Private Pilot
- Instrument Rating
- Commercial Pilot
- CFI
- ATP
- UAS
- Maintenance

Collections may be maintained manually at first and enriched later.

---

# 14. Update and Maintenance Architecture

Maintenance is a core feature.

The system should not depend on a push notification/webhook from the government source.

Use **scheduled polling + version detection + content hashing + structural diffing**.

Recommended high-level process:

```text
                     GitHub Actions
                           │
                       scheduled
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
            eCFR                 FAA Publications
              │                         │
         detect state              detect state
              │                         │
         unchanged?                 unchanged?
              │                         │
          yes → exit                 yes → exit
              │                         │
             no                        no
              └────────────┬────────────┘
                           ▼
                    download source
                           ▼
                     archive raw
                           ▼
                         parse
                           ▼
                       normalize
                           ▼
                        validate
                           ▼
                    structural diff
                           ▼
                    generate vault
                           ▼
                        test
                           ▼
                     open GitHub PR
```

## 14.1 eCFR update detection

Check eCFR regularly.

Use source metadata/version state and canonical hashes rather than assuming that every HTTP/HTML change is a regulatory change.

Track:

- last checked timestamp
- latest accepted source version
- raw checksum
- normalized/canonical checksum
- changed sections

Where possible, use eCFR Recent Changes / point-in-time capabilities to optimize discovery and validation.

## 14.2 AIM update detection

Poll the FAA publication landing page.

Extract:

- current AIM label
- effective date
- change number
- current HTML target

Compare with the stored manifest.

If unchanged, exit.

If changed:

1. download current AIM HTML corpus
2. archive it
3. parse
4. normalize
5. diff by stable AIM paragraph IDs
6. validate
7. regenerate affected outputs

Caveat: AIM paragraph numbers are not perfectly stable across editions. The FAA occasionally renumbers when inserting or removing paragraphs, shifting all subsequent numbers in a section. The differ should attempt renumber/move detection (e.g., matching paragraphs by normalized content hash) so a renumbering cascade is reported as moves rather than a mass delete-and-add, and detected cascades should be cross-checked against the FAA change notes (§17.7).

## 14.3 PCG update detection

Use the same mechanism as AIM.

Track its edition/change independently.

## 14.4 Distinguish source-format changes from content changes

This distinction is essential.

```text
raw source changed
      │
      ▼
normalize both versions
      │
      ▼
canonical hashes equal?
   ┌───────┴───────┐
  yes              no
   │                │
markup-only     content change
change          or parser issue
```

A redesign of FAA HTML should not appear as a wholesale AIM content revision.

---

# 15. Source Manifest

Maintain a machine-readable manifest at `data/manifests/sources.json`. The
manifest is committed; a fresh checkout ships with a registry whose entries are
empty until the first accepted fetch. Validation fails if the registry is
missing.

Schema (version 1, as implemented in `far_aim.manifest`):

```json
{
  "schema_version": 1,
  "sources": {
    "ecfr_title_14": {
      "last_checked_at": "2026-08-19T10:05:13Z",
      "accepted_version": "2026-08-19",
      "raw_hash": "...",
      "canonical_hash": "..."
    },
    "aim": {
      "last_checked_at": "2026-08-19T10:05:17Z",
      "effective_date": "2026-07-09",
      "change": 3,
      "raw_hash": "...",
      "canonical_hash": "..."
    },
    "pcg": {
      "last_checked_at": "2026-08-19T10:05:19Z",
      "effective_date": "2026-07-09",
      "change": 3,
      "raw_hash": "...",
      "canonical_hash": "..."
    }
  }
}
```

The `schema_version` wrapper exists so the persisted format can be migrated
deliberately; loaders reject unknown versions, unknown fields, and source sets
that do not exactly match the known registry (fail closed). Serialization is
deterministic: sorted keys, stable indentation, unset fields omitted.

Generate a human-readable Obsidian note from this manifest:

```markdown
# Source Status

| Source | Current Through |
|---|---|
| eCFR Title 14 | 2026-08-19 |
| AIM | Change 3 — 2026-07-09 |
| Pilot/Controller Glossary | Change 3 — 2026-07-09 |

Last checked: ...
```

---

# 16. Git and Revision History

Use Git as the change-history layer.

Do not stuff every historical revision into each Markdown note.

Benefits:

```bash
git log -- vault/FAR/Part-091/91.155*.md
```

and:

```bash
git diff <old-commit> <new-commit> -- vault/FAR/Part-091/
```

allow exact change inspection.

## 16.1 Automated update behavior

Initially:

**Prefer automated pull requests over automatic merges.**

Example PR:

```text
Update FAA/eCFR sources — 2026-08-19

Detected source changes:
- 14 CFR § ...
- AIM ...
- PCG ...

Validation:
✓ source downloaded
✓ parser completed
✓ canonical schema valid
✓ links valid
✓ deterministic generation
✓ no unexpected mass deletion
```

This provides a human safety gate.

eCFR amendments touching Title 14 land frequently, so a PR per detected change can create real review burden. Support batching as a configuration option from the start — for example, a weekly roll-up PR for routine eCFR changes, with immediate PRs reserved for AIM/PCG edition changes.

Later, highly trusted low-risk updates may be auto-merged if desired.

---

# 17. Validation Requirements

This project deals with regulatory and safety-related material. Validation is mandatory.

## 17.1 Source integrity

Verify:

- download succeeded
- expected content type
- non-empty response
- source version metadata parsed
- checksum stored
- no suspicious truncation

## 17.2 Structural integrity

Verify:

- expected Title 14 hierarchy exists
- Parts have expected identifiers
- section IDs are unique
- AIM chapter/section/paragraph hierarchy is coherent
- PCG terms are uniquely addressable

## 17.3 Text integrity

Critical test:

**The parser/generator must not lose or invent authoritative text.**

Use normalized text comparison where possible.

For representative fixtures, test exact paragraph output.

## 17.4 Link integrity

Verify:

- generated wikilinks resolve where expected
- no invalid citation targets
- no duplicate IDs
- no filename collisions
- no ambiguous aliases introduced automatically
- alias uniqueness enforced globally across all corpora (FAR + AIM + PCG), not per corpus — with all of Title 14 ingested, bare-number aliases can collide across chapters

## 17.5 Change integrity

Put guardrails on mass changes.

Examples:

- fail if >X% of AIM notes disappear unexpectedly
- fail if thousands of FAR sections change when source metadata suggests a tiny update
- fail if parser output suddenly becomes empty
- flag large structural churn for human review
- classify AIM renumbering cascades as moves (see §14.2) rather than treating them as mass deletions

Exact thresholds should be data-informed rather than arbitrary.

## 17.6 Determinism

Run generation twice.

The second run must produce no diff.

## 17.7 FAA change-note cross-check

When the FAA publishes an explanation/change document identifying altered AIM paragraphs, use it as an additional validation signal.

Do not assume the list is necessarily a perfect machine-readable diff, but compare it against detected changes and flag major discrepancies.

---

# 18. CLI / Developer Interface

The project should eventually support commands similar to:

```bash
far-aim check
far-aim fetch ecfr
far-aim fetch aim
far-aim fetch pcg

far-aim parse ecfr
far-aim parse aim
far-aim parse pcg

far-aim normalize
far-aim validate
far-aim diff
far-aim build-vault

far-aim update
```

A single:

```bash
far-aim update
```

should safely execute the complete check → fetch → parse → validate → diff → generate sequence.

CLI names are illustrative; consistency and testability matter more than the exact syntax.

---

# 19. Suggested Implementation Stack

A reasonable default:

- Python 3.12+
- `httpx` or `requests` for HTTP
- `lxml` / streaming XML parser for eCFR
- `BeautifulSoup` and/or `lxml.html` for FAA HTML
- typed internal models (`dataclasses`, Pydantic, or equivalent)
- JSON for canonical serialized artifacts
- PyYAML or controlled YAML serialization for frontmatter
- `pytest`
- GitHub Actions
- Git

Avoid unnecessary infrastructure in the MVP.

Do not add a graph database merely because the project has “graph-like” relationships.

The Markdown + normalized JSON layers are enough initially.

---

# 20. AI Agent Role

The project is intended to be built with an AI coding agent.

The agent should behave like a software engineer working against this specification.

## 20.1 AI is appropriate for

- implementing fetchers
- implementing parsers
- writing tests
- analyzing source structure
- refactoring
- generating schema/model code
- generating documentation
- proposing relationships
- creating optional enrichment
- diagnosing parser failures
- building GitHub Actions
- generating study features after the core is stable

## 20.2 AI must NOT be used as the parser of record

Do not send raw FAR/AIM text to an LLM and ask it to “convert this to Markdown” as the production ingestion process.

Parsing must be deterministic code.

## 20.3 AI must never rewrite official text

Never allow the agent to:

- paraphrase a regulation inside the authoritative section
- “fix” grammar
- simplify language
- omit repetitive text
- combine paragraphs
- infer missing text
- change legal wording
- replace source text with an AI summary

If the source appears malformed, preserve it and flag it.

## 20.4 AI-generated enrichment must be separable

All AI-derived data should be rebuildable/deletable independently of canonical source content.

Prefer:

```text
data/enrichment/
```

or explicit metadata fields such as:

```json
{
  "relationship_type": "semantic_related",
  "target": "cfr-14-91.157",
  "origin": "ai",
  "model": "...",
  "confidence": 0.91,
  "review_status": "unreviewed"
}
```

---

# 21. MVP Definition

The first meaningful release does **not** need AI tutoring.

The MVP is successful if:

1. It downloads authoritative Title 14 eCFR data.
2. It preserves a raw snapshot.
3. It parses the hierarchy without material loss.
4. It normalizes the corpus into stable JSON.
5. It generates navigable Markdown.
6. It downloads and parses the current FAA AIM HTML.
7. It downloads and parses the current PCG HTML.
8. It generates correct metadata/frontmatter.
9. It creates deterministic explicit cross-links.
10. It opens cleanly as an Obsidian vault.
11. Backlinks and search work naturally.
12. A source-status page tells the user how current the vault is.
13. Re-running the build with no source changes produces no diff.
14. A scheduled workflow detects upstream changes.
15. Source changes produce a reviewable Git diff/PR.
16. Automated tests catch parser breakage.

---

# 22. Phased Implementation Plan

## Phase 0 — Repository and engineering foundation

Build:

- repository structure
- Python project
- CLI skeleton
- logging
- configuration
- test framework
- source manifest format
- coding standards
- agent instructions (`AGENTS.md`)

### Exit criteria

```text
✓ tests run locally
✓ CI runs
✓ CLI skeleton works
✓ source registry exists
```

---

## Phase 1 — eCFR acquisition

Implement:

- Title 14 source discovery
- download/fetch mechanism
- source metadata
- checksums
- raw snapshot archive
- caching/idempotency
- retry/error handling

### Exit criteria

```text
✓ complete current Title 14 source can be fetched
✓ raw data preserved
✓ manifest updated
✓ repeated fetch does not create useless churn
```

---

## Phase 2 — eCFR parsing and canonical model

Implement:

```text
Title
→ Chapter
→ Subchapter
→ Part
→ Subpart
→ Section
→ nested paragraphs
→ appendices
```

Pay special attention to:

- nested paragraph labels
- tables
- notes
- authorities
- source notes
- appendices
- SFARs (Special Federal Aviation Regulations embedded inside parts, e.g., SFAR 73 in Part 61)
- reserved sections and reserved parts
- absent hierarchy levels (Title 14 has no Subtitle level; see §5.1)
- unusual markup
- citations

### Exit criteria

```text
✓ stable IDs
✓ canonical JSON generated
✓ representative exact-text tests
✓ hierarchy validated
✓ deterministic output
```

Do not proceed merely because “most sections look fine.”

---

## Phase 3 — FAR Obsidian generator

Generate:

- Part index notes
- Section notes
- metadata
- aliases
- source information
- explicit CFR cross-references
- source status note

### Exit criteria

Mechanical checks (scripted, must pass in CI):

```text
✓ zero broken generated links
✓ every Part has an index note
✓ every section note has valid, schema-checked frontmatter
✓ filenames follow the citation-only naming policy (§9)
✓ rebuild with unchanged sources produces zero diff
```

Human spot-check (open the vault in Obsidian):

```text
✓ navigation is pleasant
✓ backlinks and search behave naturally
✓ properties render correctly
```

At this point the project should already be useful.

---

## Phase 4 — AIM acquisition and parser

Implement discovery from the FAA publication page.

Fetch the current HTML corpus.

Parse:

```text
AIM
→ Chapter
→ Section
→ numbered paragraph
→ subparagraph hierarchy
→ tables/figures/references
```

Figure images are downloaded, archived with the raw snapshot, and stored as vault assets (§4.2).

### Exit criteria

```text
✓ current edition/change detected
✓ raw HTML archived
✓ figures downloaded, archived, and embedded as vault assets
✓ normalized AIM JSON
✓ representative text validation
✓ deterministic generation
```

---

## Phase 5 — Pilot/Controller Glossary

Implement:

- version discovery
- HTML acquisition
- term parsing
- aliases
- “See” references
- glossary wikilinks

### Exit criteria

```text
✓ glossary terms have stable IDs
✓ terms are searchable
✓ explicit glossary cross-references resolve
```

---

## Phase 6 — Cross-source links

Generate deterministic links:

```text
FAR ↔ FAR
AIM ↔ AIM
AIM → FAR
PCG → PCG
AIM/FAR → PCG where unambiguous and justified
```

Do not begin with broad AI semantic linking.

### Exit criteria

```text
✓ citation parser tested
✓ high-confidence links resolve
✓ no mass false-positive linking
```

---

## Phase 7 — Obsidian quality layer

Add:

- Home/index pages
- source status
- useful folder structure
- Dataview-ready metadata
- curated collection framework
- Private Pilot collection prototype
- optional CSS/snippets only if genuinely useful

### Exit criteria

A student pilot can browse and study the vault comfortably without knowing the repository internals.

---

## Phase 8 — Automated maintenance

Create GitHub Action:

```text
scheduled daily
    ↓
check upstream state
    ↓
exit if unchanged
    ↓
fetch if changed
    ↓
parse
    ↓
normalize
    ↓
validate
    ↓
diff
    ↓
build
    ↓
test
    ↓
open PR
```

### Exit criteria

```text
✓ no-change run exits cleanly
✓ simulated change produces expected diff
✓ parser failure blocks publication
✓ PR contains useful change summary
```

---

## Phase 9 — AI enrichment

Only after the canonical system is trustworthy.

Possible features:

- semantic related-regulation suggestions
- topic pages
- checkride relevance
- common questions
- concept prerequisite graph
- study scenarios
- flashcards
- explanations

All enrichment must remain separate from authoritative text.

---

# 23. Private Pilot Study Layer

A useful first curated application is a PPL collection.

Potential structure:

```text
Collections/
└── Private Pilot/
    ├── Index.md
    ├── Pilot Qualifications.md
    ├── Medical and BasicMed.md
    ├── Student Pilot and Solo.md
    ├── Currency.md
    ├── Airworthiness.md
    ├── Required Equipment.md
    ├── Airspace.md
    ├── VFR Weather Minimums.md
    ├── Preflight Requirements.md
    ├── Right of Way.md
    ├── ATC Operations.md
    ├── Emergencies.md
    └── NTSB Reporting.md
```

These pages should primarily **link/transclude** authoritative material rather than duplicate it.

Example:

```markdown
# VFR Weather Minimums

## Governing regulation

[[91.155|§ 91.155 — Basic VFR weather minimums]]

## Related

[[91.157|§ 91.157 — Special VFR weather minimums]]
[[Controlled Airspace]]
[[Flight Visibility]]
[[Cloud Clearance]]
```

This layer may eventually connect to the Private Pilot ACS.

---

# 24. Important Obsidian Design Rules

1. Do not create one giant Markdown file.
2. Prefer one note per logical authoritative unit.
3. Do not create excessively tiny files for every sentence.
4. Name generated files by stable citation only (`91.155.md`, `4-1-9.md`); never embed mutable headings in filenames (see the §9 naming policy).
5. Use stable IDs in metadata.
6. Use aliases for human-friendly navigation.
7. Keep generated and human-authored content distinguishable.
8. Do not overwrite user notes during rebuilds.
9. Use transclusion instead of source duplication.
10. Prefer high-quality links over maximum link count.
11. Make the vault useful even without community plugins.
12. Treat Dataview as an enhancement, not a dependency.
13. Do not depend on Graph View as the information architecture; the graph should emerge from good structure.

---

# 25. Update Safety and Failure Modes

The pipeline must anticipate upstream changes.

## 25.1 FAA changes HTML structure

Symptoms:

- parser count collapses
- headings disappear
- entire AIM appears changed

Response:

- fail validation
- preserve raw snapshot
- do not regenerate published vault
- open/fail workflow with diagnostics

## 25.2 eCFR schema/markup changes

Same principle:

**fail closed rather than silently producing incomplete regulations.**

## 25.3 Network failure

Do not treat network failure as “source removed.”

Use retries and retain the last known-good corpus.

## 25.4 Partial download

Checksum/content validation should reject it.

## 25.5 Upstream content correction

Treat corrections as legitimate source changes but preserve history.

## 25.6 Filename change

Stable internal IDs and link generation must prevent data identity from depending on filenames.

---

# 26. HTTP / Source Etiquette

The updater should be a polite client.

Use:

- descriptive User-Agent identifying the project
- reasonable request rates
- caching
- conditional requests where supported
- retries with backoff
- no unnecessary repeated full-site downloads
- source version checks before bulk acquisition

Do not attempt to bypass access controls.

---

# 27. Testing Strategy

Tests should include:

## Unit tests

- citation parsing
- nested paragraph parsing
- link resolution
- slug/filename generation
- YAML generation
- hashing
- version comparison

## Fixture tests

Store representative source fixtures for:

- normal CFR section
- deeply nested CFR section
- CFR table
- appendix
- AIM paragraph
- AIM table
- AIM note
- PCG term
- PCG “See” reference

## Golden-file tests

For selected sections, compare generated Markdown against committed expected output.

## Integration tests

```text
fixture source
→ parser
→ canonical model
→ Markdown
→ link validation
```

## Regression tests

Every real parser bug should result in a fixture/test that prevents recurrence.

---

# 28. Quality Bar

This project should optimize for **trust**, not maximum AI cleverness.

A user should be able to ask:

> Where did this text come from?

and the system should answer precisely.

A developer should be able to ask:

> Why did this file change?

and Git + canonical diff should answer precisely.

An AI agent should be able to ask:

> Which source is authoritative versus derived?

and the schema should answer precisely.

---

# 29. Future AI/RAG Architecture

Once the canonical data layer is mature:

```text
Canonical JSON
    │
    ├── exact citation lookup
    ├── keyword/full-text index
    ├── embeddings
    └── relationship graph
              │
              ▼
             MCP
              │
              ▼
          AI Tutor/Agent
```

A good aviation-answering agent should retrieve:

- exact applicable FAR text
- relevant AIM guidance
- glossary definitions
- later: ACS material
- related authoritative references

before generating its explanation.

The UI should preserve citations back to canonical source units.

---

# 30. Potential Future Products Built on the Same Data

Once the underlying corpus exists:

### Study

- Private Pilot mode
- Instrument mode
- Commercial mode
- CFI mode
- oral-exam/checkride simulator
- scenario questions
- flashcards
- spaced repetition

### Reference

- fast citation lookup
- cross-regulation browser
- regulation history
- “what changed?” view
- effective-date filters

### AI

- citation-aware aviation assistant
- “explain §91.155”
- “what rules apply to this scenario?”
- comparison of FAR vs AIM guidance
- MCP server for other agents

### Applications

- website
- mobile app
- browser extension
- local search server
- API

Obsidian is the first frontend, not the architectural endpoint.

---

# 31. Recommended Initial Agent Assignment

> **Status (2026-09-08):** this section is historical. Phases 0 through 8 are
> complete and the first assignment's acceptance criteria are met; the current
> state of each phase is recorded in `AGENTS.md` ("Current status"). The next
> open work is Phase 9 (§22), whose design is in §36.

The AI coding agent should **start narrowly**.

## First assignment

> Build Phase 0 through Phase 2 only: repository foundation, eCFR Title 14 acquisition, and lossless canonical parsing.

### Required work

1. Create project scaffold.
2. Create `AGENTS.md` containing the invariants in this document.
3. Implement source manifest schema.
4. Implement eCFR Title 14 fetcher using the official eCFR source/API.
5. Archive the raw source with metadata/checksum.
6. Implement normalized models.
7. Parse Title 14 hierarchy.
8. Preserve nested section/paragraph structure.
9. Add representative fixtures.
10. Add exact-text and hierarchy tests.
11. Add deterministic serialization.
12. Add `validate` command.
13. Document known unsupported source constructs, if any.
14. Do **not** move on to AIM or AI enrichment until the eCFR parser is trustworthy.

### Acceptance criteria for first assignment

- A fresh checkout can fetch the current Title 14 corpus.
- Raw source is archived or reproducibly cacheable.
- Canonical JSON can be generated.
- All normalized units have stable IDs and provenance.
- No authoritative text is AI-generated.
- Tests cover representative nested structures.
- Running the normalization twice produces byte-stable output or semantically identical deterministic output.
- Validation detects obvious truncation/parser failure.
- Architecture documentation is present.

---

# 32. Agent Invariants — Do Not Violate

These requirements override convenience:

1. **Never use an LLM-generated paraphrase as authoritative FAR/AIM content.**
2. **Never silently omit source material because it is hard to parse.**
3. **Never modify official wording to make Markdown prettier.**
4. **Never make Markdown the only canonical representation.**
5. **Never overwrite curated/user-authored notes during regeneration.**
6. **Never treat a raw HTML/XML layout change as automatically equivalent to a regulatory content change.**
7. **Never auto-merge a massive unexplained upstream diff.**
8. **Every authoritative note must have provenance.**
9. **Every parser bug fixed should gain a regression test.**
10. **No-change rebuilds must be idempotent.**
11. **Explicit authoritative relationships outrank inferred semantic relationships.**
12. **AI enrichment must remain clearly separable from authoritative data.**
13. **When uncertain, fail validation and preserve the last known-good output.**

---

# 33. Definition of Project Success

The foundation is successful when a user can open the generated vault in Obsidian, navigate from a concept or citation through accurate FAR/AIM/PCG relationships, see the source/version of every authoritative note, and trust that a scheduled pipeline will detect upstream changes and present reviewable updates without silently corrupting the corpus.

At that point, the project has become more than a converted FAR/AIM.

It is a **continuously synchronized, version-controlled aviation knowledge base** that can serve as infrastructure for many future tools.

---

# 34. Source References

Authoritative source entry points used by this design:

- eCFR API Documentation  
  `https://www.ecfr.gov/developers/documentation/api/v1`

- FAA Air Traffic Plans and Publications  
  `https://www.faa.gov/air_traffic/publications/`

The FAA publication page should be treated as the primary discovery/version page for the current AIM and Pilot/Controller Glossary rather than assuming a permanently fixed edition URL.

---

# 35. Suggested Immediate Next Step

> **Status (2026-09-08):** historical — Phases 0–8 are done. See §36 for the
> Phase 9 design.

Give this document to the coding agent and instruct it to implement **Phase 0–2 only**.

Do not begin with an end-to-end “convert the whole FAR/AIM” prompt.

The highest-risk technical problem is trustworthy, deterministic ingestion. Once the eCFR canonical layer is correct, Markdown generation is comparatively straightforward; once the full canonical layer is correct, Obsidian, AI, search, and study features become downstream applications rather than fragile foundations.

---

# 36. Phase 9 Design — Enrichment Layer

Added 2026-09-08, after Phases 0–8 shipped. This section is the design of
record for Phase 9 (§22) and refines §12.3, §12.4 and §20.4 without changing
them. Two features ship together, chosen because both add navigational value
while staying fully separable from authoritative data (§32.12):

1. **Concept graph** (Tier 3, §12.3) — a curated prerequisite graph of study
   concepts, each pointing at the authoritative notes that define it.
2. **Related sections** (Tier 4, §12.4) — machine-derived "related" links
   between FAR sections and AIM paragraphs, produced by a deterministic
   similarity provider.

## 36.1 Placement and ownership

```text
data/enrichment/
├── concepts.json          # committed, human-curated (Tier 3)
├── related.json           # committed, machine-derived by `far-aim enrich` (Tier 4)
└── related-review.json    # committed, human review overlay for Tier 4 (deny list)

vault/
├── Concepts/              # generated, generator-owned (like FAR/, AIM/, PCG/)
│   ├── Concept Map.md     # generated index + prerequisite diagram
│   └── <Concept Title>.md # one generated note per concept
└── FAR/…, AIM/…           # authoritative notes gain one trailing derived section
```

- Everything enrichment-related lives under `data/enrichment/` and is
  loaded by the generator exactly as the PCG glossary gate is: missing or
  malformed curation fails the build loudly (§32.13); a *missing*
  `related.json` simply means no derived links are rendered.
- Authoritative canonical JSON (`data/normalized/`) is never touched by
  enrichment. Deleting `data/enrichment/` and rebuilding yields a vault with
  no enrichment and no other change — the separability test (§20.4).
- `vault/Concepts/` is an owned root: stale generated concept notes are
  pruned on rebuild, curated files parked inside are kept with a warning,
  and `validate` byte-compares it like the other corpora.

## 36.2 Concept graph (Tier 3)

`concepts.json` is a list of concept records:

```json
{
  "id": "vfr-weather-minimums",
  "title": "VFR Weather Minimums",
  "area": "Airspace and Weather",
  "description": "One or two curator-written sentences. Never regulatory text.",
  "prerequisites": ["airspace-classes"],
  "far": ["91.155", "91.157", "Part 91"],
  "aim": ["3-1-4", "AIM 3-2"],
  "pcg": ["VISUAL FLIGHT RULES"],
  "see_also": ["VFR Weather Minimums"]
}
```

Rules, all enforced at build time:

- `id` is a slug (`^[a-z0-9]+(-[a-z0-9]+)*$`), unique; `title` is the note
  stem and joins the global stem/alias namespace (§17.4), so it must not
  collide case-insensitively with any generated or curated name.
- `prerequisites` name concept ids; the graph must be acyclic (a cycle is a
  build error). "Builds on this" (reverse edges) is derived, never authored.
- `far`, `aim`, `pcg` name existing generated stems (`91.155`, `Part 91`,
  `3-1-4`, `AIM 3-2`, `VISUAL FLIGHT RULES`); a reference the accepted
  editions do not define fails the build — a concept must never silently
  point nowhere when an upstream edition drops a section.
- `see_also` names any stem: generated, another concept's title, or a
  curated note (`Collections/`, `Topics/`, `Study/`) that exists on disk.
- `description` is the curator's own words — a study aid, visibly marked as
  such in the note's callout. It is not, and must never quote as if it were,
  official text (§32.1, §32.3).

Rendering: `Concepts/<Title>.md` (`type: concept`, `generated: true`) with a
"Curated concept" callout, the description, `## Prerequisites`,
`## Builds on this`, `## Regulations`, `## AIM guidance`, `## Glossary`,
`## See also`. `Concepts/Concept Map.md` groups concepts by area, lists the
starting points (no prerequisites), a suggested study order (deterministic
topological sort, ties by title) and a Mermaid prerequisite diagram
(rendered natively by Obsidian). `Home.md` links the Concept Map when a
graph is present. Authoritative notes are **not** modified by Tier 3 —
Obsidian backlinks already surface "which concepts cite § 91.155".

## 36.3 Related sections (Tier 4)

### Provider

The first provider is `lexical-tfidf` v1 in `far_aim.links.semantic`: pure
Python, no dependencies, bit-for-bit deterministic across platforms:

- Units: every FAR section (heading + official text, via
  `citations.collect_text`) and every AIM paragraph (heading + text via
  `aim_markdown.collect_text`). Appendices, chapter/section containers and
  PCG terms are not units (definitions already have Tier 2 links; long
  tabular appendices swamp similarity with boilerplate).
- Tokens: ASCII lowercase, `[a-z][a-z0-9]{2,}`, a fixed English stopword
  list, simple plural folding. Terms occurring in fewer than 2 units or in
  more than 15 % of units are dropped (boilerplate such as "person",
  "aircraft", "shall" never drives similarity).
- Weights: `tf = 1 + ln(count)`, `idf = ln(N/df)`, L2-normalised; cosine
  similarity via an inverted index accumulated in sorted term order.
- Per unit, up to 5 FAR targets and 5 AIM targets with cosine ≥ 0.30,
  scores rounded to 4 places; ranking by (score desc, id asc) so ties are
  stable.

Why not embeddings first: an embedding provider needs either a multi-GB
local model stack or a paid API, neither of which can run inside the daily
`upstream-sync` Action reproducibly. The record format below is
provider-neutral (`provider.id`/`version` are recorded per file), so a
committed embedding-provider output can replace or sit beside the lexical
one later without changing the generator; that is the intended follow-up.

### File

`related.json` (committed, machine-written, one line per unit):

```json
{"schema": 1,
 "provider": {"id": "lexical-tfidf", "version": 1},
 "inputs": {"ecfr": "<eCFR title canonical_hash>", "aim": "<AIM canonical_hash>"},
 "units": {"cfr-14-91.155": [{"target": "aim-3-1-4", "score": 0.6123}, …]}}
```

- `inputs` pins the canonical layers the file was computed from. The
  generator refuses to render a `related.json` whose inputs do not match the
  manifest ("stale; run `far-aim enrich`") — §32.13, never silently stale.
- `related-review.json` is the human overlay: `{"deny": [{"unit": …,
  "target": …, "reason": …}]}`. Denied pairs are dropped at render time.
  A deny entry naming an unknown unit is an error; one whose pair no longer
  surfaces is reported as a warning by `enrich` (suggestions drift with
  content; a no-longer-needed denial is not a defect).

### Rendering

Each FAR section note and AIM paragraph note in the corpus ends with:

```markdown
## Related (derived)

> [!info] Derived links
> Suggested by lexical similarity (`lexical-tfidf` v1), not by an explicit
> reference. Review in `data/enrichment/related-review.json`.

- [[91.157|§ 91.157 — Special VFR weather minimums]]
- [[3-1-4|AIM 3-1-4 — Basic VFR Weather Minimums]]
```

Explicit relationships outrank inferred ones (§32.11): a target already
listed under `## Explicit Cross-References` of the same note is omitted from
the derived list. Scores stay in JSON — they are reviewer metadata, and
printing them would churn every note on tiny content edits.

## 36.4 CLI and pipeline

- `far-aim enrich` — computes `related.json` from the verified eCFR and AIM
  layers (AIM optional), applies nothing from the review file except
  validation, writes only when the bytes change (idempotent), reports unit
  and link counts plus stale review entries. Runs under the source lock.
- `build-vault`, `validate`, `diff` load the enrichment layer (concepts +
  related + review + curated stems on disk) through one loader; all three
  therefore agree byte-for-byte.
- `update` runs `fetch×3 → parse×3 → enrich → diff → build-vault →
  validate`, so an accepted upstream change always refreshes derived links
  before publication and a stale `related.json` never reaches the vault.
  The vault diff in the PR is how derived-link changes are reviewed.

## 36.5 Invariants applied

| Invariant | How Phase 9 honours it |
|---|---|
| §32.1, §32.3 | No enrichment output contains official text; concept descriptions are marked curated. |
| §32.4 | `concepts.json`/`related.json` are canonical for the layer; notes are compiled output. |
| §32.5 | `Concepts/` is generator-owned; curated files inside are kept, never overwritten. |
| §32.10 | Provider is deterministic; `enrich` twice is a no-op; rebuilds are byte-stable. |
| §32.11 | Explicit cross-references suppress duplicate derived links; sections are visually separate. |
| §32.12 | Separate directory, separate note section, separate frontmatter type; delete-and-rebuild removes it cleanly. |
| §32.13 | Stale inputs, unknown references, cycles and schema defects fail the build. |

## 36.6 Exit criteria

```text
✓ `far-aim enrich` is deterministic and idempotent on the full corpus
✓ A concept referencing a dropped section fails the build with a clear message
✓ Deleting data/enrichment/ and rebuilding changes only enrichment output
✓ validate byte-compares Concepts/ and the derived sections
✓ Home → Concept Map → concept → regulation navigation works in Obsidian
```
