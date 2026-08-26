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
