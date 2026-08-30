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

## Canonical CFR model (implemented, Phase 2)

`far-aim parse ecfr` writes one JSON document per part to
`data/normalized/ecfr/part-NNNN.json` (four-digit zero-padded; reserved
ranges keep their text, e.g. `part-50-59.json`). Serialization is
deterministic: sorted keys, 2-space indent, UTF-8, trailing newline.

A **part document** (`document_type: cfr_part`, id `cfr-14-part-91`) carries
the title heading ("Title 14—Aeronautics and Space" — authoritative
wording, stored and hashed on every part), the subtitle designator/heading
(null today; Title 14 has no subtitle level, but a future `DIV2` rides
into the model instead of being dropped), the chapter/subchapter context,
part heading, `authority`, `source_note`,
editorial notes, part-level `notes`/`cross_references`, and an ordered
`children` list of subparts, subject groups, sections, and appendices —
SFARs appear as appendices in eCFR markup and keep their document position.
Subparts may carry their own `source_note`, `authority`, editorial notes,
interstitial headings, and nested appendices (part 291). A **section**
(`cfr_section`, `cfr-14-91.155`) carries its citation fields (`part`,
`subpart`, `subject_group`, `section`, `head_marker` — `§`, `§§`, or the
CAB-era `Section`/`Sec.`), `heading`, `reserved`, metadata captured from
trailing elements (`citations`, `approvals`, `section_authority`,
`amendment_notes`, `editorial_notes`), and `content`: an ordered list of
typed blocks. Sections numbered locally within their part (part 241's
`Sec. 1-1`) get part-qualified ids (`cfr-14-241-1-1`). An **appendix**
(`cfr_appendix`, `cfr-14-part-91-appendix-A` / `…-sfar-50-2` /
`…-appendixes-B-C`) holds a flat block list — appendix material does not
follow the section paragraph-label grammar, so labels stay inline in the
text.

Block types: `paragraph` (label + optional italic run-in `subject` + text +
nested `children`; a label-less paragraph opening with a period-terminated
italic run outside a definitions section is a run-in subject heading —
§29.755 — and appears as a `paragraph` with `label: null`), `definition`
(an italic defined term opening its own restartable sub-list — 1.1, 61.1;
inside sections whose heading names them a definitions/terms section, any
term shape qualifies, elsewhere only terms not ending with a period),
`text` (unlabelled/flush paragraphs with a `style`), `table` (header/body/
foot rows of cells, colspan/rowspan preserved; row children must be
TD/TH), `note`, `extract`, `heading`, `footnote`, `example`, `image`, and
`math` (equation images; the graphic path is recorded, the rendered image
itself is Phase 3+ work). An `img`/`MATH`/`FTNT` embedded mid-paragraph
keeps its source position: the paragraph text ends where the block sat,
and any trailing text follows the block as a continuation, so document
order is never reordered to text+text → block.

eCFR's flat `<P>` runs are re-nested by a deterministic label grammar over
the level kinds `(a)/(1)/(i)/(A)` plus italic letters, digits, and romans —
Title 14 is not uniform (certification parts nest romans under alphas, DOT
economic regulations use italic letters), so allowed child kinds form a
per-level graph. Ambiguous tokens like `(i)`-after-`(h)` are resolved by
sequence continuation plus one-token lookahead; repeated parent labels
(`(f)(1)` … `(f)(2)`) re-enter their level; amendment gaps (lists starting
at `(ii)`, buried mid-text run-ins) are tolerated explicitly. The full rule
order is in `far_aim.parsers.ecfr`'s module docstring.

Text is verbatim official wording; the only normalization is whitespace
collapsing. Inline styling (italics, super/subscripts, fractions) is
flattened to its character content; GPO `<AC/>` accent elements are decoded
to Unicode combining marks so mathematical notation survives (§420.5's ẋ,
ẏ, ż velocity components; §25.341's Ā; part 420's W̄az mean wind speed).
Unknown tags inside a text run fail loudly instead of being flattened —
their meaning may live in attributes, as AC's does. The raw XML archive
remains the styled record. `canonical_hash` is stored per section, per appendix, and per part
(`far_aim.models.cfr.canonical_hash`, provenance stripped recursively).

Every part parse must pass a **lossless-capture check**: the multiset of
words in the source subtree must equal the multiset of words stored in the
document, else `ParseError` (plan §32.2 — nothing silently omitted).

## Canonical AIM model (implemented, Phase 4)

`far-aim parse aim` writes one JSON document per chapter
(`data/normalized/aim/chapter-NN.json`, two-digit zero-padded), one per
appendix (`appendix-N.json`), and one for the publication itself
(`publication.json`: the index page's title, description, and edition
summary — `aim_publication`, id `aim`), with the same deterministic
serialization as the CFR layer. Stable IDs follow the FAA's own citation scheme
(`far_aim.models.aim`): `aim-chapter-4`, `aim-4-1`, `aim-4-1-9`,
`aim-appendix-3` — never headings or page filenames.

A **chapter document** (`aim_chapter`) carries the chapter `heading` (from the
chapter page's title) and an ordered `sections` list. A **section**
(`aim_section`, `aim-4-1`) carries `chapter`, `section`, `heading`,
`toc_label` (the chapter contents page's "Section N." label, validated
against the linked page — chapter 0's documented "Section 1." →
`chap0_section_0.html` mismatch is the one tolerated exception), `content` (blocks that precede the first numbered paragraph — chapter 0's
"Explanation of Changes" page is entirely section-level content and has no
paragraphs), its own `explicit_references`, and `paragraphs`. A
**paragraph** (`aim_paragraph`, `aim-4-1-9`) carries `chapter`, `section`,
`paragraph` (`"4-1-9"`), `number` (9), `heading`, `content`, and
`explicit_references`. An **appendix** (`aim_appendix`, `aim-appendix-3`)
carries `appendix`, `heading`, `content`, and `explicit_references`. Each
document's `source.url` is its own FAA page (plus `#4-1-9` for paragraphs);
the page a document came from is provenance, not content. Chapter 0's lone
section is numbered from its page filename (`chap0_section_0.html` →
`aim-0-0`), which is the URL-stable identifier, even though the chapter
contents page labels it "Section 1".

Block types (`far_aim.parsers.aim`): `text` (a `p.p` run; `<br>` becomes a
newline, all other whitespace collapses to one space), `heading` (chapter 0's
`h2` section title, `level: 2`), `list` (`level` 1–6 mirroring the FAA
edition's `ol.level-one` … `level-six` classes, whose CSS-generated markers —
`a.` `1.` `(a)` `(1)` `[a]` `[1]` — are never stored as text; an HTML `type`
attribute is preserved as `html_type`, while explicit numbering controls
(`start`, `reversed`, `li value`) fail the parse because position-derived
markers would misstate the enumeration; each item is `{"blocks": [...]}`),
`note` (`kind` note/example/reference/phraseology, the box `title` verbatim
— `NOTE-`, `EXAMPLE-`, `REFERENCE-`, `PHRASEOLOGY-` — and `blocks`), `figure`
(`number` such as `FIG 4-1-15`, `title`, and an `image` with `alt`, the
archived file's `sha256`, and its path under `source.src`), `image` (a bare
`img`, e.g. form reproductions in appendices — same fields), and `table` (`number` such as
`TBL 4-1-9`, `title`, `header_rows`/`rows`/`foot_rows` of cells; a cell is
`{"blocks": [...]}` plus optional `colspan`/`rowspan`/`header`; a
`borderless-header` presentation class is kept as `style`). Figure checksums
are part of the hashed content, so a re-published figure changes the
paragraph's `canonical_hash` (figures are part of the corpus, plan §4.2).

Inline styling (`strong`, `em`, `sup`, `sub`) is flattened to its text, as
for the CFR; the raw archive remains the styled record. Anchors keep their
text inline and are additionally collected, in document order and per
owning document, as `explicit_references`: `{"text", "target", "source":
{"href"}}` where `target` is the in-corpus id (`chapN_section_M.html#N-M-K` →
`aim-N-M-K`, `#chapN_section_M` → `aim-N-M`, `appendix_N.html` →
`aim-appendix-N`, `chap_N.html` → `aim-chapter-N`); an in-corpus link
naming a document the edition does not contain fails the parse (a broken
or incomplete corpus, never a silently dropped relationship); external
destinations (`https://…`, `mailto:`) are kept as a hashed `url` — an
authoritative link changed under unchanged display text is a content
change — while the raw `source.href` stays provenance; a paragraph link
whose fragment names a different chapter/section than its page is
malformed and fails the parse (plan §12.1 Tier 1 — explicit authoritative
references; AIM → FAR links are Phase 6 work).

Every page must pass a **lossless-capture check**: the multiset of words in
the page's content region (block-element edges counted as separators;
inline styling and anchors not) must equal the multiset of words stored in
its documents (paragraph number + heading recombined), else `ParseError`.
Chapter contents pages are held to the same rule: their elements are
allowlisted (title, section entries with number label, heading and
paragraph links; toggle buttons are controls, not content) and every word
must be accounted for by the section/paragraph structure — added prose or
a notice on a chapter page fails the parse instead of being dropped. A
non-paragraph contents entry (chapter 0 lists its lone section in place of
paragraphs) is accepted only as an exact duplicate of its section entry
(same page, same heading), so it carries no wording the canonical layer
lacks.

Source-layout locations — `source.url`, an anchor's `source.href`, an
image's `source.src` — sit under `source` keys and are therefore excluded
from `canonical_hash`: a renamed page or image file with unchanged wording
is not a content change (plan §14.4), while a changed figure *file*
(different `sha256`) is.
The provenance block (`source`) records `provider: faa`, `publication: aim`,
`source_version` (`2026-07-09-change-3`), `edition_label`, `effective_date`,
`change`, the index `url`, `retrieved_at`, and `raw_checksum` (the
snapshot tree hash); like the CFR it is excluded from `canonical_hash`.

## Canonical PCG model (implemented, Phase 5)

`far-aim parse pcg` writes one JSON document per glossary letter
(`data/normalized/pcg/letter-a.json` … `letter-w.json`) and one for the
publication itself (`publication.json`: the index page's title, purpose
section, and edition summary — `pcg_publication`, id `pcg`), with the same
deterministic serialization as the other layers.

**Stable term IDs derive from the term text itself** (`far_aim.models.pcg`:
`pcg-controlled-airspace`, `pcg-acc-icao`) — never from upstream anchors,
which are demonstrably non-unique in the FAA HTML (`ACROBATIC FLIGHT` and
`ACROBATIC FLIGHT [ICAO]` share one `id`), and never from headings-as-display
(the term *is* the citation, plan §9). A **letter document** (`pcg_letter`,
`pcg-letter-a`) carries the page's big-letter heading and an ordered `terms`
list. A **term** (`pcg_term`) carries `letter`, `term` (extracted from the
entry's `dfn` or, for the 165 dfn-less entries, split from the text by a
validated deterministic rule — see `far_aim.parsers.pcg`'s docstring), and
`content`: ordered blocks in which the full entry paragraph is stored
**verbatim** (`entry` — term, separator and all; the extracted `term` field
is derived identity metadata, never a rewrite). The glossary lists a few
terms twice with an `OR` row between alternative definitions (`COMMON
ROUTE`, `OUTER FIX`); repeated entries merge into one term document in
document order, the `OR` preserved as a text block.

Block types: `entry` (verbatim definition paragraph), `text` (continuation
paragraphs, `OR` rows, `(a)`-labelled sub-items), `list` (the `a.`/`1.`
glossary sub-lists; explicit numbering controls are rejected), `note`
(`aside` note boxes and the italic `NOTE-`/`REFERENCE-` boxes; `label` +
`text`), and `reference` — the See/Refer cross-reference rows (`kind`
see/refer, verbatim target `text`, `form` row/parenthetical/embedded, the
raw href under `source`, external destinations as a hashed `url` exactly as
in the AIM model). References resolve to term ids (plan §12.1 Tier 1):
linked rows via the archived page+anchor (a dangling one fails the parse),
unlinked rows by term text — exact match first, then `ICAO term X` →
`X [ICAO]`, then a parenthetical-stripped match accepted only when
unambiguous (the glossary cites "ADVISORY CIRCULAR" for "ADVISORY CIRCULAR
(AC)"; the `[ICAO]` tag stays significant). Targets that match no term
(upstream typos like "PREFFERED IFR ROUTES") stay unresolved as plain text
rather than guessed. The decorative glyphs on external rows ("📖", "↗") and
screen-reader-only spans are presentation chrome, excluded from captured
text; the index page's breadcrumb, sidebar, letter-card grid and statistics
are navigation with derived counts (absent from the printed glossary) and
are likewise not canonical content.

Every page must pass the **lossless-capture check** (word multiset of the
letter page's content region = word multiset of its documents), term ids
must be unique corpus-wide, and parsing is deterministic; the provenance
block matches the AIM's (`provider: faa`, `publication: pcg`, edition
label/date/change, the document's own page URL — a term's anchor — and the
snapshot tree hash), excluded from `canonical_hash`.

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
      "accepted_version": "2026-07-09-change-3",
      "effective_date": "2026-07-09",
      "change": 3,
      "edition_label": "Basic with Change 1, 2 and 3",
      "source_url": "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/index.html",
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
- For the AIM, `accepted_version` is the snapshot directory name
  (`{effective_date}-change-{n}`), and `raw_hash` is a *tree hash* — sha256
  over the sorted (path, checksum) listing of every archived page and figure
  — rather than a single file's checksum. `edition_label` and `source_url`
  pin the edition text and FAA index URL the notes render: provenance is
  outside `canonical_hash`, so `validate` compares these against every
  document's `source` block (and `parse aim` refuses a snapshot whose
  metadata disagrees with them).
