---
id: "home"
type: "home"
generated: true
title: "FAR/AIM Knowledge Vault"
---

# FAR/AIM Knowledge Vault

Reference notes generated from official U.S. aviation sources, plus a curated study layer. Official wording is never altered; every generated note records the source edition it came from.

## Sources

- [[Title 14|Title 14, Code of Federal Regulations (the FARs)]]
- [[AIM|Aeronautical Information Manual]]
- [[PCG|Pilot/Controller Glossary]]
- [[Source Status]] — the editions this vault is built from

## What is here

Two kinds of note live side by side. **Generated** notes hold the official text and are rewritten on every rebuild; **curated** notes are written by hand, point into the generated ones, and are never touched by a rebuild.

Generated:

- `FAR/` — Title 14 CFR, one note per section named by citation (`91.155`), plus one index per part. This is the regulation itself.
- `AIM/` — the Aeronautical Information Manual, one note per paragraph named by citation (`4-1-9`), figures embedded. Procedures and guidance, not regulation.
- `PCG/` — the Pilot/Controller Glossary, one note per term, named by the term. Definitions as controllers and the AIM use them.
- `Concepts/` — one note per study concept: what it builds on, and the regulations, AIM guidance and glossary terms that define it. Rendered from the curated concept graph, so edit the graph, not the notes. Start at the [[Concept Map]] for a suggested study order.

Curated:

- [[Collections]] — reading lists for one certificate, rating or operation: a subject page per topic, each a curated list of links into the official notes. Use one as a checklist of what to read.
- [[Topics]] — one concept followed across every source: where it is defined, every rule built on the definition, and the AIM guidance around it. Use one when a single idea turns up in many places.
- [[Study]] — freeform working notes: lesson debriefs, checkride questions, scratch calculations. Organize it however you like.

## Where to start

Pick a [[Collections|collection]] for your certificate and read its subject pages in order, or follow the [[Concept Map]] and open each concept's prerequisites first. Either route ends at the official text.

## Reading a note

Every regulation and AIM paragraph note has the same shape, top to bottom:

- A **Source** callout naming the edition and linking the official page, so every note can be checked against its origin.
- The **official text**, verbatim, as a nested list that mirrors the paragraph hierarchy. Headings are display-only; wording is never altered.
- **Explicit Cross-References** — the sections and parts the text itself cites, linked when the target note exists. Glossary terms list their own See/Refer references under **See Also** instead.
- **Defined Terms** (FAR notes only) — the definitions in force for the section that its text uses: 14 CFR Part 1's chapter-wide terms and abbreviations, and the part's or subpart's own definitions section where it has one (a part's definition outranks Part 1's). Each is linked to the definition itself and names the section that defines it.
- **Glossary Terms** (AIM notes only) — the glossary terms the paragraph uses. FAR notes carry none, because the regulation defines its own vocabulary in Part 1 and glossary definitions can differ.
- **Related (derived)** — similarity-based suggestions between regulations and AIM paragraphs. A study aid, never a citation: the text does not refer to these notes.

## Finding things

- **Search** any citation (`91.155`, `AIM 4-1-9`) or heading — citation forms and headings are note aliases, so the quick switcher finds a section by number or by name.
- **Backlinks** on any note list every note that cites it — open a glossary term or a definition to see everything that depends on it.
- **Follow the links at the bottom** of a note before searching: the cross-references are what the text itself points to.

## Writing your own notes

- Write in [[Study]], add a page under [[Topics]], or copy the [[Collections]] layout for a new certificate. These folders are never touched by a rebuild.
- Link by citation — double square brackets around the number, `91.155` — or transclude a passage by putting `!` in front of such a link, instead of retyping a rule: official wording then lives in one place, and your note appears in the rule's backlinks.
- Do not edit generated notes: the next rebuild overwrites them. A wrong link or parse belongs in the pipeline, not the vault.
