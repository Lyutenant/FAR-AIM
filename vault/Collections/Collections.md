---
type: "curated-index"
title: "Collections"
tags:
  - "study"
---

# Collections

Study collections gather the authoritative notes that matter for one
certificate, rating, or operation. They are **curated** — the vault
generator never writes or deletes anything in this folder, so edit
freely; the links carry you to the generated FAR/AIM/PCG notes, which
hold the official text.

## Available collections

- [[Private Pilot]] — the regulations, AIM guidance, and glossary terms a
  private pilot (airplane) works with most

## Building your own

Copy the [[Private Pilot]] layout: one index note per collection, one
page per subject, and let every page link (or transclude with
`![[91.155]]`) rather than restate the rules. With the Dataview plugin
installed you can also query the generated metadata, e.g.:

```dataview
TABLE citation, title
FROM "FAR"
WHERE part = 61 AND type = "regulation"
SORT section ASC
```
