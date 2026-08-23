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
- Never scrape commercial FAR/AIM publishers (ASA, Sporty's, Gleim, …).

## Raw snapshot storage (plan §6.2 — decided)

- `data/raw/` is a **gitignored local cache**; manifests and checksums are
  always committed.
- **eCFR:** no separate archive needed — any accepted snapshot is exactly
  reconstructible from the point-in-time API given (version, checksum).
- **AIM/PCG:** the FAA has no historical access, so every accepted raw
  snapshot **must** be durably archived outside this repo (archive repo,
  LFS, or object storage). AIM figures/images are part of the snapshot.

## HTTP etiquette

Descriptive User-Agent identifying this project; conditional requests and
caching where supported; retries with backoff (a server `Retry-After` on
429/5xx is honored over the default backoff, capped at 5 minutes);
version-check before bulk download; no repeated full-site downloads; never
bypass access controls. Network failure ≠ "source removed" — retain last
known-good.
