# Source Policy

Full specification: plan §5 (sources), §6 (acquisition), §26 (etiquette).

## Authoritative upstreams (the only canonical inputs)

- **FAR:** eCFR API — `https://www.ecfr.gov/developers/documentation/api/v1`
  (version-aware, point-in-time capable). Endpoints used:
  `GET /api/versioner/v1/titles.json` (the Title 14 entry's
  `latest_issue_date` is the source version) and
  `GET /api/versioner/v1/full/{date}/title-14.xml` (complete point-in-time
  XML, root element `<ECFR>`).
- **AIM / PCG:** discovered from the FAA publications page —
  `https://www.faa.gov/air_traffic/publications/` — never a hard-coded dated
  URL. HTML is the primary ingestion format; PDF is fallback/validation only.
  The AIM listing (`Aeronautical Information Manual (AIM) Basic with Change
  1, 2 and 3 (HTML) (effective 7/9/2026)`) yields the edition label, change
  number (the highest listed; `Basic` alone is 0), effective date, and the
  HTML index URL, which must resolve to `https://` on the FAA origin
  (`www.faa.gov`) — any other host, a protocol-relative link, or plain
  `http` fails discovery, and every FAA request's final URL and redirect hops
  are held to the same origin; the index page's own edition summary must
  agree before anything is downloaded. Pages are the index navigation's chapter, section
  and appendix pages; figures are every `images/*` file those pages embed.
- Never scrape commercial FAR/AIM publishers (ASA, Sporty's, Gleim, …).

## Raw snapshot storage (plan §6.2 — decided)

- `data/raw/` is a **gitignored local cache**; manifests and checksums are
  always committed.
- **eCFR:** no separate archive needed — any accepted snapshot is exactly
  reconstructible from the point-in-time API given (version, checksum).
- **AIM/PCG:** the FAA has no historical access, so every accepted raw
  snapshot **must** be durably archived outside this repo (archive repo,
  LFS, or object storage). AIM figures/images are part of the snapshot:
  `data/raw/aim/{effective-date}-change-{n}/{pages,figures,metadata.json}`
  (~61 MB for the 2026-07-09 Change 3 edition), identified in the manifest by
  a tree hash over every file's checksum. The PCG snapshot is
  `data/raw/pcg/{effective-date}-change-{n}/{pages,metadata.json}` (~1.1 MB
  — the index page plus one page per glossary letter; no figures), archived
  and tree-hashed the same way. `far-aim fetch aim` / `fetch pcg` print a
  reminder on every acceptance; the archive step itself is operational and
  not automated here.

## HTTP etiquette

Descriptive User-Agent identifying this project; conditional requests and
caching where supported; retries with backoff (a server `Retry-After` on
429/5xx is honored over the default backoff, capped at 5 minutes);
version-check before bulk download; no repeated full-site downloads; never
bypass access controls. Network failure ≠ "source removed" — retain last
known-good.
