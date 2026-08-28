# Vault generation (Phase 3)

`far-aim build-vault` renders the verified canonical eCFR layer into the
committed Obsidian vault at `vault/`. The full rules live in
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
  corpus — zero broken generated links by construction. Other-title
  citations (`49 CFR …`), bare `part 121` references, and appendix
  references stay plain text; a section number the document attributes to
  another title anywhere in its text (`§ 21.7 … (49 CFR 21.7)`) is never
  linked as Title 14, even from a bare `§`. The official text body itself
  is never rewritten into links.
- **Reserved documents**: `[Reserved]` under `## Official Text` is emitted
  only when the source itself marks the document reserved. A non-reserved
  document with no content (text pending as an amendment link) carries no
  Official Text section — no wording is ever fabricated.
- **Graphics** (`image`/`math` blocks) render as plain external links to
  the eCFR graphic path — not embedded/hotlinked images, and not yet
  archived assets. Downloading graphics into the vault is later-phase work
  alongside AIM figures (plan §4.2).

## Regeneration safety (plan §32.5, §32.13)

The whole vault is planned and verified in memory (links, counts, filename
policy, frontmatter schemas) before any write. Sync then:

1. refuses to overwrite any existing file that does not carry
   `generated: true` frontmatter (curated note at a generated path);
2. writes only changed files (0644), so a no-change rebuild is a byte-level
   no-op and `git status` stays clean;
3. journals every write and deletion against a backup: a mid-sync
   filesystem failure rolls the vault back to its previous state instead of
   leaving a mixed partial tree;
4. deletes generated notes no longer produced (upstream removals) but keeps
   — with a warning — curated notes parked inside `vault/FAR/`;
5. never touches anything outside `vault/FAR/` + `vault/Source Status.md`
   (`.obsidian/`, `Topics/`, etc. are out of bounds).

`far-aim validate` re-renders the plan in memory and byte-compares it
against the on-disk vault, making the zero-diff invariant a scripted check.
