# Automated maintenance (Phase 8)

`far-aim update` is the automated-maintenance entry point (plan §22
Phase 8): one command that runs the plan's
check → fetch → parse → validate → diff → build sequence with the
project's fail-closed rules. The GitHub Action
`.github/workflows/upstream-sync.yml` runs it daily and turns real
changes into a pull request.

## `far-aim update`

1. **Discovery first.** All three upstreams are polled (eCFR issue date,
   FAA AIM/PCG editions) with the same guards as `check --remote`: an
   upstream that reports an *older* version than accepted, or that
   re-labels/relocates the accepted edition, is an error — never
   auto-resolved (`--force` exists only on `fetch`, for a human).
   A network failure is an error too, and changes nothing (plan §25.3).
2. **Exit if unchanged — but resume if interrupted.** When every accepted
   version matches upstream *and* the published output is consistent with
   the manifest, `update` prints `nothing to do` and exits **without
   touching anything** — not even the manifest's `last_checked_at` — so a
   scheduled no-change run leaves a byte-clean tree. Because fetch
   accepts each source before parse/build run, an update that died
   mid-pipeline would otherwise look "current" forever; persisted,
   layer-independent probes prevent that: an accepted source with no
   `canonical_hash` (parse never completed), a `Source Status.md` that no
   longer byte-matches the manifest's accepted state (the vault predates
   a version acceptance), or a corpus index note (`Title 14.md`,
   `AIM.md`, `PCG.md`) pinning a different `canonical_hash` than the
   manifest records (a same-version correction — `fetch --force` +
   re-parse — that was never published). And whenever the local
   canonical layers are on disk,
   the full read-only validation runs too, catching damage the cheap
   probes cannot see (a sync killed mid-write, a deleted or hand-damaged
   generated note). Any of these resumes the full pipeline. A fresh
   checkout of a validated commit — CI's daily case — carries no local
   layers (gitignored, reconstructible), cannot be re-verified locally,
   and is trusted as merged: it trips nothing and stays a clean no-op.
3. **`--reverify` (CI's mode): version equality is not trusted alone.**
   Discovery cannot see the documented FAA failure mode of editing pages
   without bumping the edition, so with `--reverify` the no-op path first
   re-runs the fetchers: with a local archive they verify it cheaply;
   without one (the fresh runner) they re-download the accepted editions
   and compare tree hashes against the pinned `raw_hash`, quarantining
   and **failing the run** on any mismatch (plan §25.5) — resolved by a
   human with `far-aim fetch <source> --force`. Success or failure, pure
   `last_checked_at` drift is restored under the source lock (a real
   concurrent acceptance is kept, never clobbered), so a run that
   accepted no content leaves the manifest byte-identical. And because
   the fetchers run their own discovery, an edition that moves upstream
   mid-run gets accepted during re-verification: the manifest is
   reloaded afterwards, so that freshly accepted state resumes into
   publication in the same run rather than exiting "nothing to do".
4. **Full pipeline on change.** Otherwise it runs `fetch ecfr/aim/pcg`
   (unchanged sources re-verify their archive, or re-download and verify
   against the pinned `raw_hash` in a fresh environment), `parse` for all
   three layers, `diff`, `build-vault`, then `validate` — stopping at the
   first failing step. A fetch/parse/validation defect therefore blocks
   publication and the committed vault stays at the last known-good
   state (plan §32.13).
5. **Structural diff before generation.** `far-aim diff` re-plans the
   vault from the newly parsed layers and compares it against the vault
   still on disk (which reflects the previous versions): one line per
   document to **add**, **rewrite**, or **remove**, each category capped
   at 50 lines (a provenance-only issue bump rewrites every note of a
   corpus; the cap keeps that bounded and the summary's "canonical
   content unchanged" explains it). Upstream additions, changes, and
   removals are therefore explicit in the update log — and in the PR —
   before anything is published (plan §32.7). `diff` is also a
   standalone read-only command; differences are a report, never an
   error.
6. **Summary.** The final `update summary:` block reports each source's
   version transition and whether **canonical content** actually changed
   (canonical hashes exclude provenance, so a new eCFR issue date with
   no Title 14 amendments reports `canonical content unchanged` even
   though every FAR note's `source_version` provenance is rewritten —
   the invariant-6 distinction between layout/provenance and content).

Known failure mode worth expecting in CI: the FAA sometimes edits pages
without bumping the edition. `--reverify` checks for exactly this on
every scheduled run (not only when an unrelated version bump forces a
re-fetch): the re-download mismatches the pinned `raw_hash`, is
quarantined, and fails the run — by design (plan §25.5, §32.6/7). The
quarantined tree rides along in the run's raw-snapshot artifact;
investigate it, then re-accept deliberately with
`far-aim fetch aim --force` (or `pcg`, `ecfr`) and commit the manifest
change.

## The workflow

- **Schedule:** daily (`cron: 17 9 * * *`) plus `workflow_dispatch`.
- **No-change run:** `update` says `nothing to do`, the change-detection
  step finds a clean `data/manifests` + `vault`, and the job ends green
  with no commit, branch, or PR.
- **Changed run:** the gate re-runs `ruff` and the full `pytest` suite
  (the fetched + parsed layers are on disk, so the full-title build test
  and the curated-name collision check run for real), then a PR is
  opened (or the existing `upstream-sync` PR updated) containing the
  `update summary:`, the vault `--stat` diff, and the full log — which
  includes the structural `diff` report (adds/removals by citation). The
  vault git diff *is* the reviewable content diff — canonical JSON is
  reconstructible, Markdown is its compiled output. All steps run
  through the project `.venv`, per the repository workflow rule.
- **Nothing is auto-merged** (plan §32.7): a human reviews the PR, and a
  massive unexplained diff is a reason to reject, not merge.
- **Raw snapshot archival (plan §6.2):** the AIM/PCG downloads in
  `data/raw` are uploaded as a workflow artifact when the run **failed**
  with downloads on disk (a quarantined mismatch, or a new edition whose
  parse/tests failed — on an ephemeral runner that may be the only copy
  of a FAA edition, which has no point-in-time retrieval) or when a
  **real change** was accepted. A clean `--reverify` run also
  re-downloads the accepted editions, but those bytes hash-match the
  snapshots archived when the edition was first accepted, so uploading
  them daily would only duplicate storage — skipped. Artifacts expire —
  download and archive them durably outside the repository when
  accepting the PR (or when investigating a failed run). eCFR raw is
  reconstructible from its point-in-time API and is not uploaded.

## Exit criteria (plan §22 Phase 8)

- *No-change run exits cleanly* — `test_update_from_empty_runs_full_pipeline_then_noops`
  (second run: `nothing to do`, manifest byte-identical).
- *Simulated change produces expected diff* —
  `test_update_version_bump_without_content_change` (new issue date,
  identical XML → `canonical content unchanged`, per-note provenance
  updated).
- *Parser failure blocks publication* —
  `test_update_fetch_failure_blocks_publication` (pipeline stops, vault
  and accepted state untouched); in CI any failing step prevents the PR.
- *PR contains useful change summary* — the PR body carries the
  `update summary:` version/content transitions plus the vault diff stat.
