"""Phase 5 tests: PCG discovery and acquisition.

The fixture under ``tests/fixtures/pcg/pcg_html/`` is verbatim official HTML
from the accepted Basic-with-Change-3 edition (effective 2026-07-09): the
index page and letter navigation trimmed to five letters (b, k, n, o, t —
letter t keeps only its TRAFFIC PATTERN region; whole entries removed to
keep it small, retained wording untouched). The publications listing is
shared with the AIM fixtures. All HTTP traffic goes through
httpx.MockTransport.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path
from unittest import mock

import httpx
import pytest

from far_aim.config import Config
from far_aim.manifest import SourceManifest, SourceState
from far_aim.parsers import pcg as pcg_parser
from far_aim.sources import pcg
from far_aim.sources.common import FetchError
from tests.test_aim_source import PUBLICATIONS_HTML, no_sleep

FIXTURES = Path(__file__).parent / "fixtures" / "pcg"
PCG_HTML = FIXTURES / "pcg_html"
INDEX_URL = "https://www.faa.gov/air_traffic/publications/atpubs/pcg_html/index.html"
VERSION = "2026-07-09-change-3"
FIXTURE_LETTERS = frozenset({"b", "k", "n", "o", "t"})
FIXTURE_PAGES = (
    "index.html",
    "glossary-b.html",
    "glossary-k.html",
    "glossary-n.html",
    "glossary-o.html",
    "glossary-t.html",
)


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    """The fixture corpus is tiny; scale the truncation guards down to match."""
    monkeypatch.setattr(pcg, "MIN_PAGES", 3)
    monkeypatch.setattr(pcg, "MIN_TERMS", 5)
    monkeypatch.setattr(pcg, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(pcg, "REQUIRED_LETTERS", FIXTURE_LETTERS)
    monkeypatch.setattr(pcg_parser, "REQUIRE_RESOLVED_REFERENCES", False)


class PcgUpstream:
    """Configurable fake FAA site: publications page and PCG pages."""

    PREFIX = "/air_traffic/publications/atpubs/pcg_html/"

    def __init__(self) -> None:
        self.publications = PUBLICATIONS_HTML
        self.pages: dict[str, bytes] = {
            name: (PCG_HTML / name).read_bytes() for name in FIXTURE_PAGES
        }
        self.requests: Counter[str] = Counter()
        self.queries: dict[str, list[str]] = {}
        self.status_overrides: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests[path] += 1
        self.queries.setdefault(path, []).append(request.url.query.decode("ascii"))
        if path in self.status_overrides:
            return httpx.Response(self.status_overrides[path])
        if path == "/air_traffic/publications/":
            return httpx.Response(
                200, content=self.publications.encode("utf-8"),
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        if path.startswith(self.PREFIX):
            name = path[len(self.PREFIX) :]
            if name in self.pages:
                return httpx.Response(
                    200, content=self.pages[name], headers={"Content-Type": "text/html"}
                )
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def page_requests(self) -> int:
        return sum(n for p, n in self.requests.items() if p.startswith(self.PREFIX))


@pytest.fixture
def upstream() -> PcgUpstream:
    return PcgUpstream()


@pytest.fixture
def config(tmp_path) -> Config:
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    return config


def fetch(config: Config, upstream: PcgUpstream, **kwargs) -> pcg.PcgFetchResult:
    with upstream.client() as client:
        return pcg.fetch_pcg(config, client, sleep=no_sleep, **kwargs)


def fetch_fixture(tmp_path) -> tuple[Config, pcg.PcgFetchResult]:
    """Accept the fixture corpus into a fresh project root (shared by other suites)."""
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    with mock.patch.multiple(
        pcg, MIN_PAGES=3, MIN_TERMS=5, PAUSE_SECONDS=0, REQUIRED_LETTERS=FIXTURE_LETTERS
    ):
        return config, fetch(config, PcgUpstream())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_pcg(upstream):
    with upstream.client() as client:
        discovery = pcg.discover_pcg(client, sleep=no_sleep)
    assert discovery.version == VERSION
    assert discovery.label == "Basic with Change 1, 2 and 3"
    assert discovery.index_url == INDEX_URL


def test_index_edition_parses_two_digit_year():
    html = (PCG_HTML / "index.html").read_text(encoding="utf-8")
    assert pcg.index_edition(html) == ("2026-07-09", 3)


def test_index_edition_missing_summary_fails():
    with pytest.raises(FetchError, match="publication summary"):
        pcg.index_edition("<html><body><main></main></body></html>")


def test_page_names_from_index_are_ordered():
    html = (PCG_HTML / "index.html").read_text(encoding="utf-8")
    assert pcg.page_names_from_index(html) == list(FIXTURE_PAGES[1:])


def test_unrecognized_navigation_link_fails_closed():
    html = (PCG_HTML / "index.html").read_text(encoding="utf-8")
    renamed = html.replace('<li><a href="glossary-k.html">K</a></li>',
                           '<li><a href="section-k.html">K</a></li>')
    with pytest.raises(FetchError, match="unrecognized page 'section-k.html'"):
        pcg.page_names_from_index(renamed)


def test_grid_and_nav_disagreement_fails():
    html = (PCG_HTML / "index.html").read_text(encoding="utf-8")
    # Drop letter k from the sidebar nav only; the letter grid still lists it.
    trimmed = html.replace('<li><a href="glossary-k.html">K</a></li>', "")
    with pytest.raises(FetchError, match="letter grid"):
        pcg.page_names_from_index(trimmed)


def test_page_set_requires_baseline_letters():
    with pytest.raises(FetchError, match=r"missing baseline letters \['t'\]"):
        pcg.check_page_set([f"glossary-{x}.html" for x in sorted(FIXTURE_LETTERS - {"t"})])
    with pytest.raises(FetchError, match="denote the same letter"):
        pcg.check_page_set(["glossary-b.html", "./glossary-b.html"])


def test_classify_page():
    assert pcg.classify_page("glossary-a.html") == ("letter", "a")
    assert pcg.classify_page("./glossary-w.html") == ("letter", "w")
    with pytest.raises(ValueError):
        pcg.classify_page("chap_4.html")


# ---------------------------------------------------------------------------
# Fetch / archive / manifest
# ---------------------------------------------------------------------------


def test_fetch_downloads_archives_and_updates_manifest(config, upstream):
    result = fetch(config, upstream)
    assert result.downloaded
    assert result.version == VERSION
    assert result.page_count == len(FIXTURE_PAGES)
    assert result.term_count == 117
    snapshot = config.raw_dir / "pcg" / VERSION
    assert result.snapshot_dir == snapshot
    for name in FIXTURE_PAGES:
        assert (snapshot / "pages" / name).read_bytes() == upstream.pages[name]
    metadata = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["provider"] == "faa"
    assert metadata["publication"] == "pcg"
    assert metadata["edition_label"] == "Basic with Change 1, 2 and 3"
    assert metadata["effective_date"] == "2026-07-09"
    assert metadata["change"] == 3
    assert metadata["index_url"] == INDEX_URL
    assert set(metadata["files"]) == {f"pages/{n}" for n in FIXTURE_PAGES}
    assert metadata["raw_hash"] == result.raw_hash
    assert pcg.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)

    state = SourceManifest.load(config.manifest_path).sources["pcg"]
    assert state.accepted_version == VERSION
    assert state.effective_date == "2026-07-09"
    assert state.change == 3
    assert state.raw_hash == result.raw_hash
    assert state.edition_label == "Basic with Change 1, 2 and 3"
    assert state.source_url == INDEX_URL
    assert state.canonical_hash is None
    assert state.last_checked_at is not None


def test_refetch_same_edition_does_not_redownload(config, upstream):
    first = fetch(config, upstream)
    requests = upstream.page_requests()
    second = fetch(config, upstream)
    assert not second.downloaded
    assert second.raw_hash == first.raw_hash
    assert upstream.page_requests() == requests


def test_refetch_hash_mismatch_fails_closed(config, upstream):
    first = fetch(config, upstream)
    shutil.rmtree(config.raw_dir / "pcg" / VERSION)
    upstream.pages["glossary-o.html"] = upstream.pages["glossary-o.html"].replace(
        b"A general term used within ATC", b"A general term now used within ATC"
    )
    with pytest.raises(FetchError, match="different content"):
        fetch(config, upstream)
    state = SourceManifest.load(config.manifest_path).sources["pcg"]
    assert state.raw_hash == first.raw_hash
    quarantined = [p for p in (config.raw_dir / "pcg").iterdir() if ".mismatch-" in p.name]
    assert len(quarantined) == 1
    result = fetch(config, upstream, force=True)
    assert result.downloaded and result.raw_hash != first.raw_hash


def test_edition_disagreement_between_listing_and_index_fails(config, upstream):
    upstream.pages["index.html"] = upstream.pages["index.html"].replace(
        b"<p>Change: 3</p>", b"<p>Change: 2</p>"
    )
    with pytest.raises(FetchError, match="mid-update"):
        fetch(config, upstream)
    assert not (config.raw_dir / "pcg" / VERSION).exists()
    assert SourceManifest.load(config.manifest_path).sources["pcg"].accepted_version is None


def test_too_few_terms_fails_closed(config, upstream, monkeypatch):
    monkeypatch.setattr(pcg, "MIN_TERMS", 5000)
    with pytest.raises(FetchError, match="term entries"):
        fetch(config, upstream)


def test_truncated_page_is_rejected(config, upstream):
    page = upstream.pages["glossary-n.html"]
    upstream.pages["glossary-n.html"] = page[: page.index(b'id="NAVAID_CLASSES"')]
    with pytest.raises(FetchError, match="malformed or truncated HTML"):
        fetch(config, upstream)
    assert not (config.raw_dir / "pcg" / VERSION).exists()


def test_upstream_rollback_is_refused_without_force(config, upstream):
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["pcg"] = SourceState(
        accepted_version="2026-09-01-change-4",
        effective_date="2026-09-01",
        change=4,
        raw_hash="sha256:" + "0" * 64,
    )
    manifest.save(config.manifest_path)
    with pytest.raises(FetchError, match="older than accepted"):
        fetch(config, upstream)
    result = fetch(config, upstream, force=True)
    assert result.downloaded
    state = SourceManifest.load(config.manifest_path).sources["pcg"]
    assert state.accepted_version == VERSION and state.change == 3


def test_new_edition_clears_stale_canonical_hash(config, upstream):
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["pcg"] = SourceState(
        accepted_version="2026-01-22-change-2",
        effective_date="2026-01-22",
        change=2,
        raw_hash="sha256:" + "0" * 64,
        canonical_hash="sha256:" + "1" * 64,
    )
    manifest.save(config.manifest_path)
    fetch(config, upstream)
    state = SourceManifest.load(config.manifest_path).sources["pcg"]
    assert state.accepted_version == VERSION
    assert state.canonical_hash is None


def test_relabelled_edition_is_refused_without_force(config, upstream):
    fetch(config, upstream)
    upstream.publications = upstream.publications.replace(
        "Pilot/Controller Glossary Basic with Change 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
        "Pilot/Controller Glossary Basic with Changes 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
    )
    with pytest.raises(FetchError, match="re-run with --force"):
        fetch(config, upstream)
    result = fetch(config, upstream, force=True)
    assert result.downloaded
    assert SourceManifest.load(config.manifest_path).sources["pcg"].edition_label == (
        "Basic with Changes 1, 2 and 3"
    )


def test_forced_relabel_of_parsed_edition_clears_canonical_hash(config, upstream):
    """Same version and bytes re-accepted under a new listing label: the
    normalized layer still carries the old provenance, so the recorded
    canonical_hash must be cleared (validate pins provenance against the
    manifest; a preserved hash would fail validation while claiming a
    parsed layer)."""
    fetch(config, upstream)
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["pcg"].canonical_hash = "sha256:" + "1" * 64
    manifest.save(config.manifest_path)
    upstream.publications = upstream.publications.replace(
        "Pilot/Controller Glossary Basic with Change 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
        "Pilot/Controller Glossary Basic with Changes 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
    )
    result = fetch(config, upstream, force=True)
    assert result.downloaded
    state = SourceManifest.load(config.manifest_path).sources["pcg"]
    assert state.edition_label == "Basic with Changes 1, 2 and 3"
    assert state.canonical_hash is None
    # An unchanged forced re-acceptance keeps the recorded hash.
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["pcg"].canonical_hash = "sha256:" + "2" * 64
    manifest.save(config.manifest_path)
    fetch(config, upstream, force=True)
    state = SourceManifest.load(config.manifest_path).sources["pcg"]
    assert state.canonical_hash == "sha256:" + "2" * 64


def test_altered_archive_provenance_is_not_reported_verified(config, upstream):
    fetch(config, upstream)
    path = config.raw_dir / "pcg" / VERSION / "metadata.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["edition_label"] = "Basic with Change 9"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(FetchError, match="provenance was altered"):
        fetch(config, upstream)
    assert fetch(config, upstream, force=True).downloaded
    assert not fetch(config, upstream).downloaded


def test_verify_snapshot_rejects_extra_and_tampered_files(config, upstream):
    result = fetch(config, upstream)
    snapshot = config.raw_dir / "pcg" / VERSION
    extra = snapshot / "pages" / "glossary-z.html"
    extra.write_bytes(b"<html></html>")
    assert not pcg.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)
    extra.unlink()
    assert pcg.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)
    page = snapshot / "pages" / "glossary-b.html"
    page.write_bytes(page.read_bytes() + b" ")
    assert not pcg.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)


def test_verify_snapshot_reconciles_metadata_with_files(config, upstream):
    result = fetch(config, upstream)
    snapshot = config.raw_dir / "pcg" / VERSION
    path = snapshot / "metadata.json"
    original = path.read_text(encoding="utf-8")
    for key, value in (("byte_count", -1), ("page_count", 99), ("term_count", 5000)):
        data = json.loads(original)
        data[key] = value
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert not pcg.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash), key
    path.write_text(original, encoding="utf-8")
    assert pcg.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)


def test_manifest_commit_failure_leaves_manifest_unchanged(config, upstream, monkeypatch):
    real_save = SourceManifest.save

    def failing_save(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(SourceManifest, "save", failing_save)
    with pytest.raises(OSError):
        fetch(config, upstream)
    monkeypatch.setattr(SourceManifest, "save", real_save)
    assert SourceManifest.load(config.manifest_path).sources["pcg"].accepted_version is None
    result = fetch(config, upstream)
    assert result.downloaded
    assert SourceManifest.load(config.manifest_path).sources["pcg"].accepted_version == VERSION


# ---------------------------------------------------------------------------
# CDN neutralisation (shared with the AIM; see test_aim_source / test_sources_common)
# ---------------------------------------------------------------------------

_INJECTION = (
    b'<script >bazadebezolkohpepadr="%b"</script>'
    b'<script type="text/javascript" src="https://www.faa.gov/akam/13/%b"  defer></script>'
)


def _inject(pages: dict[str, bytes], token: bytes, loader: bytes) -> dict[str, bytes]:
    out = {}
    for name, body in pages.items():
        assert body.count(b"</head>") == 1, name
        out[name] = body.replace(b"</head>", _INJECTION % (token, loader) + b"</head>")
    return out


def test_cdn_script_injection_does_not_change_raw_hash(config, upstream):
    clean = fetch(config, upstream)
    snapshot = config.raw_dir / "pcg" / VERSION
    shutil.rmtree(snapshot)
    upstream.pages = _inject(upstream.pages, b"1577209189", b"5e024e09")
    injected = fetch(config, upstream)
    assert injected.downloaded and injected.raw_hash == clean.raw_hash
    for name in FIXTURE_PAGES:
        assert b"bazadebezolkohpepadr" not in (snapshot / "pages" / name).read_bytes()
    shutil.rmtree(snapshot)
    first_pair = _INJECTION % (b"1577209189", b"5e024e09")
    upstream.pages = _inject(
        {n: b.replace(first_pair, b"") for n, b in upstream.pages.items()},
        b"1974812660",
        b"75b53f27",
    )
    assert fetch(config, upstream).raw_hash == clean.raw_hash
    assert not [p for p in (config.raw_dir / "pcg").iterdir() if ".mismatch-" in p.name]


def test_corpus_requests_are_cache_busted(config, upstream):
    fetch(config, upstream)
    assert upstream.queries["/air_traffic/publications/"] == [""]
    corpus = {p: q for p, q in upstream.queries.items() if p.startswith(upstream.PREFIX)}
    assert len(corpus) == len(FIXTURE_PAGES)
    nonces = {q for qs in corpus.values() for q in qs}
    assert len(nonces) == 1 and re.fullmatch(r"far-aim-nocache=[0-9a-f]{16}", nonces.pop())
