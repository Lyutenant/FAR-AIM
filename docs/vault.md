# Vault generation (Phases 3–4)

`far-aim build-vault` renders the verified canonical eCFR and AIM layers into
the committed Obsidian vault at `vault/`. The full rules live in
`far_aim.generate`; this note records the layout, the rendering decisions,
and the known limitations.

## Layout and naming (plan §9)

```text
vault/
├── Source Status.md          # manifest-derived currency table
├── .obsidian/                # minimal committed config (workspace gitignored)
└── FAR/
    ├── Title 14.md           # chapters → subchapters → part links
    └── Part 091/             # folder digit-runs padded to 3 for sort order
        ├── Part 91.md        # part index: hierarchy, authority, contents
        ├── 91.155.md         # one note per section, named by citation only
        └── Part 91 Appendix A.md
```

- Section notes are named by the `section` field verbatim (`91.155.md`,
  `91.27-91.99.md`, part 241's `1-1.md`). Headings never appear in
  filenames, so curated links survive upstream heading edits.
- Appendix stems derive from the stable id: `Part 91 Appendix A`,
  `Part 91 SFAR 50-2`, `Part 91 Appendixes B-C`; the parser's fallback
  slugs (`Table-A-to-Part-117`) are already self-describing and become the
  stem verbatim.
- All stems are checked globally unique (case-folded) at build time.

## Note anatomy

Frontmatter is hand-emitted YAML in a fixed key order with every string
JSON-double-quoted (deterministic bytes; `91.155` and dates would otherwise
be misread by YAML). Schema-checked per note kind (`regulation`,
`appendix`, `index`, `status`) before anything is written. No volatile
timestamps ever appear in notes (plan §32.10) — `retrieved_at` and
`last_checked_at` live only in the manifest.

Body: H1 (`# § 91.155 — Basic VFR weather minimums`), a `> [!info] Source`
callout linking the point-in-time eCFR page, `## Official Text`,
`## Source Notes` (FR citations, approvals, amendment/editorial notes, when
present), and `## Explicit Cross-References`.

Aliases: `§ 91.155`, `14 CFR 91.155`, plus the heading — the heading alias
is dropped globally whenever two notes would share it (plan §17.4; e.g.
"Applicability" appears in dozens of parts).

## Rendering decisions

- **Paragraph hierarchy renders flat.** Each paragraph is its own Markdown
  paragraph led by its bold label (`**(a)** *Subject.* text…`), children
  following in order. Nesting reaches depth 5 and contains tables and
  extracts, which break inside Markdown list indentation; printed CFR is
  read flat via its labels anyway.
- **Official wording is verbatim**, subject only to Markdown escaping;
  unknown block types fail the build rather than being dropped.
- **Tables** render as pipe tables when safe (single header row, no
  col/rowspans, rectangular, no newlines in cells) and as inline HTML
  tables otherwise — both lossless, both rendered by Obsidian.
- **Cross-references** are extracted deterministically from official text
  (`§ 91.157`, `§§ 91.101 through 91.135` endpoints, `14 CFR 121.317(c)`
  lists) and listed as wikilinks only when the target section exists in the
  corpus — zero broken generated links by construction. Since Phase 6 the
  same list continues with the **parts** the text cites as `[[Part 121]]`
  links to the part index, in part order after the sections; the
  document's own part is never listed. Only CFR-qualified forms count —
  `part 121 or part 135 of this chapter`, `parts 43 and 91 of this
  chapter`, `Part 375 of this title`, `part 26 of this subchapter`,
  `14 CFR part 13` — because Title 14 text also writes a bare `Part 1` for
  ICAO Annex 6, an IEC standard, or an appendix's own headings; an
  unqualified `part 119 certificate holder` or `Part 121 or 135` therefore
  stays plain text unless the same note qualifies that number elsewhere
  (the list is per-note and deduplicated). Other-title
  citations (`49 CFR …`) and appendix references stay plain text; a number
  the document attributes to another title anywhere in its text (`§ 21.7 …
  (49 CFR 21.7)`, `part 21 … (49 CFR part 21)`) is never linked as Title
  14, even from a bare `§`/`part`. Not read as parts: Civil Air
  Regulations numbering (`CAR Part 3`, `part 4a of the Civil Air
  Regulations`), dashed other-code numbers (`41 CFR part 60-1`), printed
  volume ranges (`14 CFR parts 1 to 59`), and a document's own
  subdivisions (`Part 1 of this appendix`, `part 1 of appendix C to part
  25`). The official text body itself is never rewritten into links.
- **Reserved documents**: `[Reserved]` under `## Official Text` is emitted
  only when the source itself marks the document reserved. A non-reserved
  document with no content (text pending as an amendment link) carries no
  Official Text section — no wording is ever fabricated.
- **Graphics** (`image`/`math` blocks) render as plain external links to
  the eCFR graphic path — not embedded/hotlinked images, and not yet
  archived assets. Downloading eCFR graphics into the vault remains
  later-phase work; AIM figures are archived and embedded (see below).

## Regeneration safety (plan §32.5, §32.13)

The whole vault is planned and verified in memory (links, counts, filename
policy, frontmatter schemas) before any write. Sync then:

1. refuses to overwrite any existing file that does not carry
   `generated: true` frontmatter (curated note at a generated path) or, for
   assets, that the ledger does not attribute to the generator;
2. writes only changed files (0644), so a no-change rebuild is a byte-level
   no-op and `git status` stays clean;
3. journals every write and deletion against a backup: a mid-sync
   filesystem failure rolls the vault back to its previous state instead of
   leaving a mixed partial tree;
4. deletes generated notes no longer produced (upstream removals) but keeps
   — with a warning — curated notes parked inside `vault/FAR/`;
5. never touches anything outside `vault/FAR/`, `vault/AIM/` and
   `vault/Source Status.md` (`.obsidian/`, `Topics/`, etc. are out of
   bounds).

`far-aim validate` re-renders the plan in memory and byte-compares it
against the on-disk vault, making the zero-diff invariant a scripted check.

## AIM (Phase 4)

```text
vault/AIM/
├── AIM.md                       # publication index: chapters, appendices
├── Chapter 04/                  # zero-padded for sort order
│   ├── AIM Chapter 4.md         # chapter contents: sections → paragraphs
│   ├── AIM 4-1.md               # section note (preamble content, paragraph list)
│   └── 4-1-9.md                 # one note per numbered paragraph
├── Appendices/
│   └── AIM Appendix 3.md
└── assets/                      # archived figures, embedded by the notes
    └── aim0401_fig79_recovered.svg
```

- Paragraph notes are named by the paragraph number verbatim (`4-1-9.md`,
  plan §9). Chapter, section and appendix notes carry an `AIM` prefix
  (`AIM Chapter 4`, `AIM 4-1`, `AIM Appendix 3`) because bare `4-1` would
  collide with FAR part-local section stems (part 241's `4-1.md`). All stems
  and aliases are unique across both corpora; a heading alias claimed by
  both a FAR section and an AIM note is dropped from both (this is why a
  handful of FAR notes — e.g. part 71's "Class B airspace" — lost their
  heading alias when the AIM arrived).
- Frontmatter kinds `aim` (paragraph), `aim_section`, `aim_chapter`,
  `aim_appendix`, `aim_index` carry `effective_date` + `change` instead of
  an issue date (plan §10.2). Chapter 0's "Explanation of Changes" is a
  section note (`AIM 0-0.md`, stable id from its page filename) with
  section-level Official Text; its displayed citation follows the FAA's own
  "Section 1." contents label (`toc_label`), never an invented "Section 0".
- Body: H1 (`# AIM 4-1-9 — Heading`), a Source callout naming the edition
  and linking the FAA page anchor, `## Official Text`, `## Paragraphs`
  (section notes) and `## Explicit Cross-References` — the anchors the FAA
  itself places in the text (`Para 5-4-3`, `Section 4`, `Appendix 4`),
  linked only when the target is in the corpus, followed by the **FAR
  sections and parts the official text cites** (Phase 6, plan §12.1 "AIM
  citing a FAR"): `14 CFR section 91.171`, `14 CFR § 91.225`, `14 CFR
  91.113(g)`, `14 CFR part 91`, and bare `Part 107 operations` are
  recognized deterministically (`far_aim.links.citations`) and rendered as
  `[[91.155|14 CFR § 91.155]]` / `[[Part 91|14 CFR Part 91]]` — sections in
  citation order, then parts — only when the FAR note exists. Other-title
  citations (`49 CFR part 1542`) never link and ban that number for the
  whole note; a bare `section 91.185` without a `CFR`/`§` anchor, appendix
  references and the FAA's own typos (`91.113b`) stay plain text. The FAR
  links are derived at render time from the official text, so the canonical
  AIM JSON and its hashes are unchanged.
- **`## Glossary Terms`** (Phase 6, plan §12.2 Tier 2) closes every AIM
  section, paragraph and appendix note whose official text uses
  Pilot/Controller Glossary terms, listing them as `[[TERM|TERM]]` links in
  term order. This is a lexical relationship, not an FAA citation, so it
  never shares the Tier 1 `## Explicit Cross-References` list. Recognition
  (`far_aim.links.glossary`) is deterministic and gated: multiword terms
  match case-insensitively (`flight plan`), acronyms — a term's
  parenthetical (`… (AFP)`) or an entry that is only a `See` reference
  (`ATC`) — match in capitals only and at three letters or more, and
  single-word defined entries (`AIRCRAFT`, `OVER`) never match unless the
  committed gate allows them. The gate, `data/links/pcg-glossary-gate.json`
  (human-maintained, plan §12.3), denies entries whose capitals still
  collide with ordinary AIM usage (`CAT` is an approach category there,
  `CENTER`/`CLEARANCE`/`SPEED` are phraseology) and allows single-word
  aviation nouns (`TRANSPONDER`, `WAYPOINT`, `NOTAM`); every entry carries a
  reason and must name a term the accepted edition defines. An alias two
  terms would share goes to the term whose text is exactly the alias, else
  is dropped; the longest alias wins at a position (`ADS-B` is never also
  `ADS`). The FAR gets no glossary links: its vocabulary is defined by
  14 CFR Part 1, and the PCG's ATC-oriented definitions can differ.
- **Lists render flat** with the FAA edition's markers (`**a.**`, `**1.**`,
  `**(a)**`, `**(1)**`, `**[a]**`, `**[1]**`) derived from list level and
  position — the same decision as the CFR's flat paragraphs, for the same
  reason (six-deep nesting containing tables, figures and callouts). The
  `type="a"`/`type="i"` attributes on many source `<ol>` elements are
  ignored on purpose: the edition's stylesheet overrides them, and the
  official text agrees with the stylesheet (AIM 4-1-20 cites the fifth
  item of a `type="i"` level-three list as "(e) above").
- **Boxes** become callouts titled with the verbatim label: NOTE- →
  `[!note]`, EXAMPLE- → `[!example]`, REFERENCE- → `[!cite]`, PHRASEOLOGY- →
  `[!quote]`.
- **Source line breaks** (`<br>`, preserved by the parser as newlines) render
  as backslash hard breaks, so multi-line references and phraseology keep
  their layout under CommonMark as well as Obsidian.
- **Figures** are embedded from `vault/AIM/assets/` as `![[file|title]]`
  beneath a `**FIG 4-1-15** *Title*` caption line; bare images (form
  reproductions) embed the same way. Asset files keep the FAA's filenames
  (underscores allowed; collision-checked with note stems since they share
  the wikilink namespace) and are byte-verified against the checksum the
  canonical layer recorded. Binary files carry no frontmatter, so generator
  ownership is recorded in `assets/.generated.json` (filename → sha256,
  itself a generated file carrying a `far_aim_asset_ledger` marker, so a
  user-authored file at that reserved path is recognized as curated and
  refused/kept like any other): only files the ledger lists, with bytes
  unchanged since the generator wrote them, are ever overwritten or pruned;
  a curated file at a generated asset path refuses the build even when its
  bytes happen to match (it is never silently adopted into the ledger), and
  other files in `assets/` (or a generated asset edited in place) are kept
  with a warning — the same contract as curated notes.
- **Tables** render as pipe tables when every cell is one line of text or a
  single image (embeds work inside cells) with a single header row and no
  spans, and as inline HTML otherwise (nested lists and boxes are flattened
  to marker-led `<p>` runs; image cells use `<img src="../assets/…">`, a
  path relative to the note's own directory — TBL 7-1-10 is the one such
  table).
- Figures add ~54 MB of PNG/SVG to the committed vault for the current
  edition; that is the plan's decision (§4.2: figures are part of the corpus
  and are never hotlinked).

## PCG (Phase 5)

```text
vault/PCG/
├── PCG.md                       # publication index: purpose, terms by letter
├── A/
│   └── ABEAM.md                 # one note per term, named by the term itself
├── C/
│   └── CONTROLLED AIRSPACE.md
└── N/
    └── NAVIGATION SPECIFICATION (ICAO).md
```

- The term is the citation (plan §9): the stem is the term text verbatim,
  deterministically sanitized to a portable, wikilink-safe filename
  (`naming.pcg_term_stem`): unicode hyphens and `/` become `-`, `[…]`
  becomes `(…)`, a trailing period drops (`CHART SUPPLEMENT U.S`), and a
  term matching a reserved stem or a FAR/AIM citation shape gets a
  ` (PCG)` suffix (today only the term `AIM`). Terms are published in
  capitals and the stems keep that capitalization — deriving mixed case
  from all-caps official text would be guesswork. When sanitization
  changed anything, the verbatim term rides along as an alias — filtered
  through the global alias-uniqueness rule, so a verbatim form that equals
  another note's stem is dropped (the term `AIM`'s verbatim form is the
  AIM index note's stem; `[[AIM]]` stays unambiguous).
- Note body: H1 (the verbatim term), the Source callout naming the edition
  and linking the term's own FAA page anchor, `## Official Text` — the
  full entry paragraph(s) verbatim (term, dash and all), sub-lists flat
  with `**a.**`-style markers, note boxes as callouts — then `## See Also`
  (glossary cross-references, wikilinked when the target term is in the
  corpus, plain text when the FAA's citation matches no term) and
  `## References` (external documents). Since Phase 6 a `Refer to` row
  that names a FAR part or section, or the AIM itself, links the vault
  note instead: `14 CFR part 91` → `[[Part 91|14 CFR part 91]]`, `14 CFR
  part 1, §1.1` → the row text followed by `[[1.1|§ 1.1]], [[Part 1]]`, a
  bare `AIM` → `[[AIM]]` (the AIM index note; the FAA URL is kept when
  the AIM corpus is not built). Other documents (FAA Orders, ACs) stay
  plain text or keep their source URL. Bracketed `[ICAO]` tags display as
  `(ICAO)` inside wikilinks, whose syntax reserves square brackets.
- `PCG.md` renders the index page's purpose section and edition summary,
  then every term as a link under its letter heading.
- Cross-corpus effects: PCG stems join the global namespace, so a FAR/AIM
  heading alias that case-folds to a glossary term (e.g. "Wake Turbulence")
  is dropped by the existing global alias-uniqueness rule (plan §17.4).
