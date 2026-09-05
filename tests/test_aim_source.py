"""Phase 4 tests: FAA publications discovery and AIM acquisition.

The fixture under ``tests/fixtures/aim/`` is verbatim official HTML from the
accepted Basic-with-Change-3 edition (effective 2026-07-09): the FAA
publications listing, the AIM index, chapter 0 and 4 pages, section pages
0-0 and 4-1 (whole paragraphs removed to keep it small; retained wording
untouched), and appendices 1 and 3. Figures are tiny placeholder files under
the real filenames. All HTTP traffic goes through httpx.MockTransport.
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
from far_aim.parsers import aim as aim_parser
from far_aim.sources import aim, faa_publications
from far_aim.sources.common import FetchError, strip_cdn_injection

FIXTURES = Path(__file__).parent / "fixtures" / "aim"
AIM_HTML = FIXTURES / "aim_html"
PUBLICATIONS_HTML = (FIXTURES / "publications.html").read_text(encoding="utf-8")
INDEX_URL = "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/index.html"
VERSION = "2026-07-09-change-3"
FIXTURE_CHAPTERS = frozenset({0, 4})
FIXTURE_APPENDICES = frozenset({1, 3})
FIXTURE_PAGES = (
    "index.html",
    "chap_0.html",
    "chap0_section_0.html",
    "chap_4.html",
    "chap4_section_1.html",
    "appendix_1.html",
    "appendix_3.html",
)


def no_sleep(_seconds: float) -> None:
    pass


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    """The fixture corpus is tiny; scale the truncation guards down to match."""
    monkeypatch.setattr(aim, "MIN_PAGES", 3)
    monkeypatch.setattr(aim, "MIN_PARAGRAPHS", 3)
    monkeypatch.setattr(aim, "MIN_FIGURES", 2)
    monkeypatch.setattr(aim, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(aim, "REQUIRED_CHAPTERS", FIXTURE_CHAPTERS)
    monkeypatch.setattr(aim, "REQUIRED_APPENDICES", FIXTURE_APPENDICES)
    monkeypatch.setattr(aim_parser, "REQUIRE_RESOLVED_REFERENCES", False)


class AimUpstream:
    """Configurable fake FAA site: publications page, AIM pages, figures."""

    PREFIX = "/air_traffic/publications/atpubs/aim_html/"

    def __init__(self) -> None:
        self.publications = PUBLICATIONS_HTML
        self.pages: dict[str, bytes] = {
            name: (AIM_HTML / name).read_bytes() for name in FIXTURE_PAGES
        }
        self.images: dict[str, bytes] = {
            path.name: path.read_bytes() for path in sorted((AIM_HTML / "images").iterdir())
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
        if path.startswith(self.PREFIX + "images/"):
            name = path[len(self.PREFIX) + len("images/") :]
            if name in self.images:
                kind = "image/svg+xml" if name.endswith(".svg") else "image/png"
                return httpx.Response(
                    200, content=self.images[name], headers={"Content-Type": kind}
                )
            return httpx.Response(404)
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
def upstream() -> AimUpstream:
    return AimUpstream()


@pytest.fixture
def config(tmp_path) -> Config:
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    return config


def fetch(config: Config, upstream: AimUpstream, **kwargs) -> aim.AimFetchResult:
    with upstream.client() as client:
        return aim.fetch_aim(config, client, sleep=no_sleep, **kwargs)


def fetch_fixture(tmp_path) -> tuple[Config, aim.AimFetchResult]:
    """Accept the fixture corpus into a fresh project root (shared by other suites)."""
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    with mock.patch.multiple(
        aim,
        MIN_PAGES=3,
        MIN_PARAGRAPHS=3,
        MIN_FIGURES=2,
        PAUSE_SECONDS=0,
        REQUIRED_CHAPTERS=FIXTURE_CHAPTERS,
        REQUIRED_APPENDICES=FIXTURE_APPENDICES,
    ):
        return config, fetch(config, AimUpstream())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_parse_publications_page():
    listings = faa_publications.parse_publications_page(PUBLICATIONS_HTML)
    assert set(listings) == {"aim", "pcg"}
    listing = listings["aim"]
    assert listing.label == "Basic with Change 1, 2 and 3"
    assert listing.effective_date == "2026-07-09"
    assert listing.change == 3
    assert listing.html_url == INDEX_URL
    pcg = listings["pcg"]
    assert pcg.label == "Basic with Change 1, 2 and 3"
    assert pcg.html_url.endswith("/atpubs/pcg_html/index.html")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Basic with Change 1, 2 and 3", 3),
        ("Basic with Change 1", 1),
        ("Basic", 0),
        ("Change 2", 2),
        ("Basic with Changes 1 and 2", 2),
    ],
)
def test_change_number(label, expected):
    assert faa_publications.change_number(label) == expected


def test_change_number_unknown_label_fails():
    with pytest.raises(FetchError):
        faa_publications.change_number("Reissue")


def test_publications_page_without_html_listing_fails():
    html = PUBLICATIONS_HTML.replace("(<abbr>HTML</abbr>)", "(<abbr>PDF</abbr>)", 1)
    with pytest.raises(FetchError, match="exactly one HTML listing"):
        faa_publications.parse_publications_page(html)


def test_pcg_listing_problems_do_not_block_aim_discovery():
    """Only the requested publication is validated (a PCG hiccup must not hide the AIM)."""
    html = PUBLICATIONS_HTML.replace("Pilot/Controller Glossary", "Pilot Glossary")
    assert faa_publications.parse_publications_page(html, publications=("aim",))["aim"].change == 3
    with pytest.raises(FetchError, match="Pilot/Controller Glossary"):
        faa_publications.parse_publications_page(html)
    with pytest.raises(ValueError, match="unknown publications"):
        faa_publications.parse_publications_page(html, publications=("atc",))


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/atpubs/aim_html/index.html",
        "//evil.example/atpubs/aim_html/index.html",
        "http://www.faa.gov/air_traffic/publications/atpubs/aim_html/index.html",
    ],
)
def test_publications_listing_outside_faa_origin_fails(href):
    html = PUBLICATIONS_HTML.replace('href="atpubs/aim_html/index.html"', f'href="{href}"', 1)
    with pytest.raises(FetchError, match="outside the approved FAA origin"):
        faa_publications.parse_publications_page(html, publications=("aim",))


def test_publications_page_without_effective_date_fails():
    html = PUBLICATIONS_HTML.replace("(effective 7/9/2026)", "", 1)
    with pytest.raises(FetchError, match="effective date"):
        faa_publications.parse_publications_page(html)


def test_discover_aim(upstream):
    with upstream.client() as client:
        discovery = aim.discover_aim(client, sleep=no_sleep)
    assert discovery.version == VERSION
    assert discovery.index_url == INDEX_URL


def test_index_edition():
    html = (AIM_HTML / "index.html").read_text(encoding="utf-8")
    assert aim.index_edition(html) == ("2026-07-09", 3)


def test_index_edition_missing_summary_fails():
    with pytest.raises(FetchError, match="publication summary"):
        aim.index_edition("<html><body><main></main></body></html>")


def test_page_names_from_index_are_ordered():
    html = (AIM_HTML / "index.html").read_text(encoding="utf-8")
    assert aim.page_names_from_index(html) == [
        "chap_0.html",
        "chap0_section_0.html",
        "chap_4.html",
        "chap4_section_1.html",
        "appendix_1.html",
        "appendix_3.html",
    ]


def test_unrecognized_navigation_link_fails_closed():
    html = (AIM_HTML / "index.html").read_text(encoding="utf-8")
    renamed = html.replace('href="./chap4_section_1.html"', 'href="./chapter4_sec1.html"')
    with pytest.raises(FetchError, match="unrecognized local page './chapter4_sec1.html'"):
        aim.page_names_from_index(renamed)
    # Non-page navigation links (home, anchors, external) remain acceptable.
    tolerated = html.replace(
        '<a class="nav-link" href="./chap4_section_1.html">',
        '<a class="nav-link" href="#top">x</a><a class="nav-link" href="https://www.faa.gov/">y</a>'
        '<a class="nav-link" href="./chap4_section_1.html">',
    )
    assert "chap4_section_1.html" in aim.page_names_from_index(tolerated)


def test_page_names_inconsistent_chapters_fail():
    html = (AIM_HTML / "index.html").read_text(encoding="utf-8")
    html = html.replace("chap_4.html", "chap_5.html")
    with pytest.raises(FetchError, match="inconsistent"):
        aim.page_names_from_index(html)


def test_page_set_requires_baseline_chapters_and_appendices():
    """A partially rendered index above the aggregate floors still fails closed."""
    import re

    html = (AIM_HTML / "index.html").read_text(encoding="utf-8")

    def drop_links(source: str, *names: str) -> str:
        for name in names:
            source = re.sub(rf'<a [^>]*href="(\./)?{re.escape(name)}"[^>]*>[^<]*</a>', "", source)
        return source

    without_chapter_4 = drop_links(html, "chap_4.html", "chap4_section_1.html")
    with pytest.raises(FetchError, match=r"missing baseline chapters \[4\]"):
        aim.page_names_from_index(without_chapter_4)
    without_appendix_3 = drop_links(html, "appendix_3.html")
    with pytest.raises(FetchError, match=r"appendices \[3\]"):
        aim.page_names_from_index(without_appendix_3)


def test_page_set_rejects_duplicate_identities_and_section_gaps():
    with pytest.raises(FetchError, match="denote the same chapter"):
        aim.check_page_set(["chap_4.html", "chap_04.html", "chap4_section_1.html"])
    with pytest.raises(FetchError, match="not contiguous"):
        aim.check_page_set(["chap_4.html", "chap4_section_1.html", "chap4_section_3.html"])
    with pytest.raises(FetchError, match="not contiguous"):
        aim.check_page_set(["chap_4.html", "chap4_section_2.html"])


def test_page_set_accepts_additions_beyond_baseline(monkeypatch):
    monkeypatch.setattr(aim, "REQUIRED_CHAPTERS", frozenset({0}))
    monkeypatch.setattr(aim, "REQUIRED_APPENDICES", frozenset())
    aim.check_page_set(
        ["chap_0.html", "chap0_section_0.html", "chap_7.html", "chap7_section_1.html",
         "chap7_section_2.html", "appendix_9.html"]
    )


def test_htmltree_strictness():
    from far_aim.htmltree import HTMLStructureError, parse_html

    with pytest.raises(HTMLStructureError, match="unclosed elements"):
        parse_html("<html><body><p>cut off")
    with pytest.raises(HTMLStructureError, match="unexpected </div>"):
        parse_html("<html><body><p>x</div></body></html>")
    root = parse_html("<html><body><p>cut off", strict=False)
    assert root.text() == "cut off"
    root = parse_html("<html><body><p>a<br>b</p><img src=x></body></html>")
    assert root.text() == "ab"


def test_classify_page():
    assert aim.classify_page("chap_4.html") == ("chapter", 4)
    assert aim.classify_page("chap4_section_1.html") == ("section", 4, 1)
    assert aim.classify_page("appendix_3.html") == ("appendix", 3)
    with pytest.raises(ValueError):
        aim.classify_page("glossary-a.html")


def test_image_names_ignores_site_chrome():
    html = (
        '<html><body><noscript><img src="https://www.faa.gov/akam/pixel"></noscript>'
        '<main><img src="./images/a.png"><img src="./images/a.png"><img src="images/b.svg">'
        "</main></body></html>"
    )
    assert aim.image_names(html) == ["a.png", "b.svg"]


def test_image_names_outside_images_dir_fails():
    html = '<html><body><main><img src="https://example.com/x.png"></main></body></html>'
    with pytest.raises(FetchError, match="outside images/"):
        aim.image_names(html)


# ---------------------------------------------------------------------------
# Fetch / archive / manifest
# ---------------------------------------------------------------------------


def test_fetch_downloads_archives_and_updates_manifest(config, upstream):
    result = fetch(config, upstream)
    assert result.downloaded
    assert result.version == VERSION
    assert result.page_count == len(FIXTURE_PAGES)
    assert result.paragraph_count == 7
    assert result.figure_count == 5
    snapshot = config.raw_dir / "aim" / VERSION
    assert result.snapshot_dir == snapshot
    for name in FIXTURE_PAGES:
        # Archived as served, minus the CDN's per-download script injection.
        assert (snapshot / "pages" / name).read_bytes() == strip_cdn_injection(upstream.pages[name])
    for name, data in upstream.images.items():
        assert (snapshot / "figures" / name).read_bytes() == data
    metadata = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["provider"] == "faa"
    assert metadata["edition_label"] == "Basic with Change 1, 2 and 3"
    assert metadata["effective_date"] == "2026-07-09"
    assert metadata["change"] == 3
    assert metadata["index_url"] == INDEX_URL
    assert set(metadata["files"]) == {f"pages/{n}" for n in FIXTURE_PAGES} | {
        f"figures/{n}" for n in upstream.images
    }
    assert metadata["raw_hash"] == result.raw_hash
    assert aim.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)

    state = SourceManifest.load(config.manifest_path).sources["aim"]
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
    assert second.page_count == first.page_count
    assert upstream.page_requests() == requests


def test_refetch_backfills_pinned_provenance_from_snapshot(config, upstream):
    fetch(config, upstream)
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["aim"].edition_label = None
    manifest.sources["aim"].source_url = None
    manifest.save(config.manifest_path)
    assert not fetch(config, upstream).downloaded
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.edition_label == "Basic with Change 1, 2 and 3"
    assert state.source_url == INDEX_URL


def test_altered_archive_provenance_is_not_reported_verified(config, upstream):
    """metadata.json sits outside the tree hash; a changed label there must not
    let `fetch` report a verified no-op that `parse` would then refuse."""
    fetch(config, upstream)
    path = config.raw_dir / "aim" / VERSION / "metadata.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["edition_label"] = "Basic with Change 9"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(FetchError, match="provenance was altered"):
        fetch(config, upstream)
    assert SourceManifest.load(config.manifest_path).sources["aim"].edition_label == (
        "Basic with Change 1, 2 and 3"
    )
    # --force re-accepts from the FAA and the archive is coherent again.
    assert fetch(config, upstream, force=True).downloaded
    assert not fetch(config, upstream).downloaded


def test_relabelled_edition_refused_even_when_cache_is_gone(config, upstream):
    fetch(config, upstream)
    shutil.rmtree(config.raw_dir / "aim")
    upstream.publications = upstream.publications.replace(
        "(<abbr>AIM</abbr>) Basic with Change 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
        "(<abbr>AIM</abbr>) Basic with Changes 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
    )
    with pytest.raises(FetchError, match="re-run with --force"):
        fetch(config, upstream)
    assert fetch(config, upstream, force=True).downloaded


def test_relabelled_edition_is_not_a_silent_noop(config, upstream):
    """Same date/change but a changed listing label: the archive still verifies,
    the manifest keeps the *snapshot's* provenance, and the operator must decide."""
    first = fetch(config, upstream)
    snapshot = config.raw_dir / "aim" / VERSION
    upstream.publications = upstream.publications.replace(
        "(<abbr>AIM</abbr>) Basic with Change 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
        "(<abbr>AIM</abbr>) Basic with Changes 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
    )
    # Legacy manifest (no pins): backfilled from the verified snapshot, not the listing.
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["aim"].edition_label = None
    manifest.sources["aim"].source_url = None
    manifest.save(config.manifest_path)
    with pytest.raises(FetchError, match="re-run with --force"):
        fetch(config, upstream)
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.edition_label == "Basic with Change 1, 2 and 3"
    assert aim.verify_snapshot(snapshot, version=VERSION, raw_hash=first.raw_hash)
    # Pinned manifest: the same reconciliation error, nothing changed on disk.
    with pytest.raises(FetchError, match="re-run with --force"):
        fetch(config, upstream)
    assert SourceManifest.load(config.manifest_path).sources["aim"].edition_label == (
        "Basic with Change 1, 2 and 3"
    )
    result = fetch(config, upstream, force=True)
    assert result.downloaded
    assert SourceManifest.load(config.manifest_path).sources["aim"].edition_label == (
        "Basic with Changes 1, 2 and 3"
    )


def test_refetch_after_cache_purge_redownloads(config, upstream):
    first = fetch(config, upstream)
    shutil.rmtree(config.raw_dir / "aim")
    second = fetch(config, upstream)
    assert second.downloaded
    assert second.raw_hash == first.raw_hash


def test_refetch_with_missing_figure_redownloads(config, upstream):
    first = fetch(config, upstream)
    (config.raw_dir / "aim" / VERSION / "figures" / "aim0401_fig79_recovered.svg").unlink()
    second = fetch(config, upstream)
    assert second.downloaded
    assert second.raw_hash == first.raw_hash


def test_refetch_hash_mismatch_fails_closed(config, upstream):
    """A lost cache is re-fetched; same edition, different bytes must not be accepted."""
    first = fetch(config, upstream)
    shutil.rmtree(config.raw_dir / "aim" / VERSION)
    upstream.pages["chap4_section_1.html"] = upstream.pages["chap4_section_1.html"].replace(
        b"Centers are established", b"Centers are now established"
    )
    with pytest.raises(FetchError, match="different content"):
        fetch(config, upstream)
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.raw_hash == first.raw_hash
    assert not (config.raw_dir / "aim" / VERSION).exists()
    quarantined = [p for p in (config.raw_dir / "aim").iterdir() if ".mismatch-" in p.name]
    assert len(quarantined) == 1
    assert b"now established" in (quarantined[0] / "pages" / "chap4_section_1.html").read_bytes()


def test_refetch_hash_mismatch_force_accepts_and_preserves_old(config, upstream):
    first = fetch(config, upstream)
    upstream.pages["chap4_section_1.html"] = upstream.pages["chap4_section_1.html"].replace(
        b"Centers are established", b"Centers are now established"
    )
    second = fetch(config, upstream, force=True)
    assert second.downloaded and second.raw_hash != first.raw_hash
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.raw_hash == second.raw_hash
    superseded = [p for p in (config.raw_dir / "aim").iterdir() if ".superseded-" in p.name]
    assert len(superseded) == 1
    assert aim.verify_snapshot(superseded[0], version=VERSION, raw_hash=first.raw_hash)


def test_edition_disagreement_between_listing_and_index_fails(config, upstream):
    upstream.pages["index.html"] = upstream.pages["index.html"].replace(
        b"<strong>Change:</strong> Change 3", b"<strong>Change:</strong> Change 2"
    )
    with pytest.raises(FetchError, match="mid-update"):
        fetch(config, upstream)
    assert not (config.raw_dir / "aim" / VERSION).exists()
    assert SourceManifest.load(config.manifest_path).sources["aim"].accepted_version is None


def test_missing_figure_upstream_fails_closed(config, upstream):
    del upstream.images["aim0401_fig80_recovered.svg"]
    with pytest.raises(FetchError, match="aim0401_fig80_recovered.svg"):
        fetch(config, upstream)
    assert not (config.raw_dir / "aim" / VERSION).exists()
    assert not any(
        p.name.startswith(".") for p in (config.raw_dir / "aim").iterdir()
    ), "temporary download directory must be cleaned up"


def test_too_few_paragraphs_fails_closed(config, upstream, monkeypatch):
    monkeypatch.setattr(aim, "MIN_PARAGRAPHS", 500)
    with pytest.raises(FetchError, match="numbered paragraphs"):
        fetch(config, upstream)


def test_page_without_main_region_fails(config, upstream):
    upstream.pages["chap0_section_0.html"] = b"<html><body><p>moved</p></body></html>"
    with pytest.raises(FetchError, match="no main content region"):
        fetch(config, upstream)


def test_non_html_page_fails(config, upstream, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("chap_4.html"):
            return httpx.Response(200, content=b"%PDF", headers={"Content-Type": "application/pdf"})
        return upstream.handler(request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(FetchError, match="expected html"),
    ):
        aim.fetch_aim(config, client, sleep=no_sleep)


def test_transient_failures_are_retried(config, upstream):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("chap_4.html") and attempts["n"] < 2:
            attempts["n"] += 1
            return httpx.Response(503)
        return upstream.handler(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = aim.fetch_aim(config, client, sleep=no_sleep)
    assert result.downloaded
    assert attempts["n"] == 2


def test_upstream_rollback_is_refused_without_force(config, upstream):
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["aim"] = SourceState(
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
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.accepted_version == VERSION and state.change == 3


def test_forced_relabel_of_parsed_edition_clears_canonical_hash(config, upstream):
    """Same version and bytes re-accepted under a new listing label: the
    normalized layer still carries the old provenance, so the recorded
    canonical_hash must be cleared."""
    fetch(config, upstream)
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["aim"].canonical_hash = "sha256:" + "1" * 64
    manifest.save(config.manifest_path)
    upstream.publications = upstream.publications.replace(
        "(<abbr>AIM</abbr>) Basic with Change 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
        "(<abbr>AIM</abbr>) Basic with Changes 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
    )
    result = fetch(config, upstream, force=True)
    assert result.downloaded
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.edition_label == "Basic with Changes 1, 2 and 3"
    assert state.canonical_hash is None


def test_new_edition_clears_stale_canonical_hash(config, upstream):
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["aim"] = SourceState(
        accepted_version="2026-01-22-change-2",
        effective_date="2026-01-22",
        change=2,
        raw_hash="sha256:" + "0" * 64,
        canonical_hash="sha256:" + "1" * 64,
    )
    manifest.save(config.manifest_path)
    fetch(config, upstream)
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.accepted_version == VERSION
    assert state.canonical_hash is None


def test_manifest_commit_failure_leaves_manifest_unchanged(config, upstream, monkeypatch):
    real_save = SourceManifest.save

    def failing_save(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(SourceManifest, "save", failing_save)
    with pytest.raises(OSError):
        fetch(config, upstream)
    monkeypatch.setattr(SourceManifest, "save", real_save)
    assert SourceManifest.load(config.manifest_path).sources["aim"].accepted_version is None
    # The next fetch recovers on its own.
    result = fetch(config, upstream)
    assert result.downloaded
    assert SourceManifest.load(config.manifest_path).sources["aim"].accepted_version == VERSION


def test_verify_snapshot_rejects_extra_and_tampered_files(config, upstream):
    result = fetch(config, upstream)
    snapshot = config.raw_dir / "aim" / VERSION
    extra = snapshot / "pages" / "chap_9.html"
    extra.write_bytes(b"<html></html>")
    assert not aim.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)
    extra.unlink()
    assert aim.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)
    page = snapshot / "pages" / "chap_0.html"
    page.write_bytes(page.read_bytes() + b" ")
    assert not aim.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)


def test_verify_snapshot_reconciles_metadata_with_files(config, upstream):
    result = fetch(config, upstream)
    snapshot = config.raw_dir / "aim" / VERSION
    path = snapshot / "metadata.json"
    original = path.read_text(encoding="utf-8")
    for key, value in (
        ("byte_count", -1),
        ("page_count", 99),
        ("figure_count", 1),
        ("paragraph_count", 500),
    ):
        data = json.loads(original)
        data[key] = value
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert not aim.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash), key
    data = json.loads(original)
    data["files"]["pages/chap_0.html"]["bytes"] += 1
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert not aim.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)
    path.write_text(original, encoding="utf-8")
    assert aim.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)


def test_fetch_refuses_case_colliding_filenames(config, upstream):
    page = upstream.pages["chap4_section_1.html"]
    upstream.pages["chap4_section_1.html"] = page.replace(
        b"./images/aim0401_fig80_recovered.svg", b"./images/AIM0401_FIG79_RECOVERED.SVG"
    )
    upstream.images["AIM0401_FIG79_RECOVERED.SVG"] = upstream.images["aim0401_fig79_recovered.svg"]
    with pytest.raises(FetchError, match="differing only by case"):
        fetch(config, upstream)
    assert not (config.raw_dir / "aim" / VERSION).exists()


def test_fetch_self_verifies_before_accepting(config, upstream, monkeypatch):
    monkeypatch.setattr(aim, "verify_snapshot", lambda *a, **k: False)
    with pytest.raises(FetchError, match="failed self-verification"):
        fetch(config, upstream)
    assert not (config.raw_dir / "aim" / VERSION).exists()
    assert SourceManifest.load(config.manifest_path).sources["aim"].accepted_version is None
    assert not any(p.name.startswith(".") for p in (config.raw_dir / "aim").iterdir())


def test_truncated_page_is_rejected(config, upstream):
    """A response cut short can still carry every marker; strict parsing catches it."""
    page = upstream.pages["appendix_3.html"]
    cut = page.index(b"<tr class=\"row\">", page.index(b"<tbody"))
    upstream.pages["appendix_3.html"] = page[:cut]
    with pytest.raises(FetchError, match="malformed or truncated HTML"):
        fetch(config, upstream)
    assert not (config.raw_dir / "aim" / VERSION).exists()


def test_invalid_utf8_page_is_a_controlled_failure(config, upstream):
    upstream.pages["chap_4.html"] = upstream.pages["chap_4.html"].replace(
        b"Air Traffic Control", b"Air Traffic Contr\xff\xfel"
    )
    with pytest.raises(FetchError, match="not valid UTF-8"):
        fetch(config, upstream)
    upstream.pages["index.html"] = b"\xff" + upstream.pages["index.html"]
    with pytest.raises(FetchError, match="AIM index page: response is not valid UTF-8"):
        fetch(config, upstream)
    assert not (config.raw_dir / "aim" / VERSION).exists()


def test_index_page_figures_are_archived(config, upstream):
    upstream.pages["index.html"] = upstream.pages["index.html"].replace(
        b'<div class="publication-description">',
        b'<div class="publication-description"><p><img class="image" src="./images/cover.png" '
        b'alt="Cover"></p>',
    )
    upstream.images["cover.png"] = upstream.images["aimapd1_floating0.png"]
    result = fetch(config, upstream)
    assert (result.snapshot_dir / "figures" / "cover.png").exists()
    assert result.figure_count == 6


def test_redirect_outside_faa_origin_is_rejected(config, upstream):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.faa.gov" and request.url.path.endswith("chap_4.html"):
            return httpx.Response(
                302, headers={"Location": "https://mirror.example/aim_html/chap_4.html"}
            )
        if request.url.host == "mirror.example":
            return httpx.Response(
                200, content=upstream.pages["chap_4.html"], headers={"Content-Type": "text/html"}
            )
        return upstream.handler(request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client,
        pytest.raises(FetchError, match="outside the approved FAA origin"),
    ):
        aim.fetch_aim(config, client, sleep=no_sleep)
    assert not (config.raw_dir / "aim" / VERSION).exists()


def test_publications_page_redirect_outside_faa_origin_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.faa.gov":
            return httpx.Response(302, headers={"Location": "https://mirror.example/pubs/"})
        return httpx.Response(
            200, content=PUBLICATIONS_HTML.encode(), headers={"Content-Type": "text/html"}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client,
        pytest.raises(FetchError, match="outside the approved FAA origin"),
    ):
        faa_publications.discover_publications(client, sleep=no_sleep, publications=("aim",))


def test_truncated_publications_page_is_rejected():
    html = PUBLICATIONS_HTML[: PUBLICATIONS_HTML.index("<h2 id=\"pubs\">")]
    with pytest.raises(FetchError, match="malformed or truncated HTML"):
        faa_publications.parse_publications_page(html)


def test_forced_refetch_with_equal_content_preserves_snapshot_until_commit(
    config, upstream, monkeypatch
):
    """Same bytes, new provenance: a failed manifest commit must leave the old
    snapshot (and its metadata) in place, not the uncommitted replacement."""
    first = fetch(config, upstream)
    snapshot = config.raw_dir / "aim" / VERSION
    old_metadata = (snapshot / "metadata.json").read_bytes()
    upstream.publications = upstream.publications.replace(
        "(<abbr>AIM</abbr>) Basic with Change 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
        "(<abbr>AIM</abbr>) Basic with Changes 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
    )
    real_save = SourceManifest.save

    def failing_save(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(SourceManifest, "save", failing_save)
    with pytest.raises(OSError):
        fetch(config, upstream, force=True)
    monkeypatch.setattr(SourceManifest, "save", real_save)
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.edition_label == "Basic with Change 1, 2 and 3"
    assert (snapshot / "metadata.json").read_bytes() == old_metadata
    assert aim.verify_snapshot(snapshot, version=VERSION, raw_hash=first.raw_hash)
    assert not any(".superseded-" in p.name for p in (config.raw_dir / "aim").iterdir())
    # The committed path then succeeds and leaves no redundant copy behind.
    second = fetch(config, upstream, force=True)
    assert second.raw_hash == first.raw_hash
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.edition_label == "Basic with Changes 1, 2 and 3"
    assert json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))[
        "edition_label"
    ] == "Basic with Changes 1, 2 and 3"
    assert not any(".superseded-" in p.name for p in (config.raw_dir / "aim").iterdir())


def test_tree_hash_is_order_independent():
    a = aim.tree_hash({"pages/a.html": "sha256:1", "figures/b.png": "sha256:2"})
    b = aim.tree_hash({"figures/b.png": "sha256:2", "pages/a.html": "sha256:1"})
    assert a == b
    assert a != aim.tree_hash({"pages/a.html": "sha256:1"})


def test_version_string_and_page_url():
    assert aim.version_string("2026-07-09", 3) == VERSION
    assert aim.page_url(INDEX_URL, "chap4_section_1.html", "4-1-9") == (
        "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/chap4_section_1.html#4-1-9"
    )


# ---------------------------------------------------------------------------
# CDN neutralisation (first scheduled upstream-sync run, 2026-09-05)
# ---------------------------------------------------------------------------


def _reinject(pages: dict[str, bytes], token: bytes, loader: bytes) -> dict[str, bytes]:
    """The fixture pages carry the FAA site's Akamai script pair verbatim;
    return them with different (per-download) values."""
    out = {}
    for name, body in pages.items():
        assert b"bazadebezolkohpepadr" in body, name
        out[name] = re.sub(
            rb'bazadebezolkohpepadr="[^"]*"', b'bazadebezolkohpepadr="' + token + b'"', body
        )
        out[name] = re.sub(rb"/akam/13/[0-9a-f]+", b"/akam/13/" + loader, out[name])
    return out


def test_cdn_script_injection_does_not_change_raw_hash(config, upstream):
    """A re-download whose only difference is the CDN's per-download script
    values must verify against the pinned raw_hash, not be quarantined."""
    first = fetch(config, upstream)
    snapshot = config.raw_dir / "aim" / VERSION
    for name in FIXTURE_PAGES:
        archived = (snapshot / "pages" / name).read_bytes()
        assert b"bazadebezolkohpepadr" not in archived
        assert not re.search(rb"<script[^>]*www\.faa\.gov/akam/", archived)
        # The stable noscript tracking pixel is page content as served.
        assert re.search(rb"<noscript><img src=\"https://www\.faa\.gov/akam/", archived)
    metadata = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["files"]["pages/chap_4.html"]["bytes"] == len(
        strip_cdn_injection(upstream.pages["chap_4.html"])
    )

    shutil.rmtree(snapshot)
    upstream.pages = _reinject(upstream.pages, b"1974812660", b"75b53f27")
    second = fetch(config, upstream)
    assert second.downloaded
    assert second.raw_hash == first.raw_hash
    assert not [p for p in (config.raw_dir / "aim").iterdir() if ".mismatch-" in p.name]

    # Real content edits are still caught.
    shutil.rmtree(snapshot)
    upstream.pages["chap4_section_1.html"] = upstream.pages["chap4_section_1.html"].replace(
        b"Centers are established", b"Centers are now established"
    )
    with pytest.raises(FetchError, match="different content"):
        fetch(config, upstream)


def test_corpus_requests_are_cache_busted_per_download(config, upstream):
    fetch(config, upstream)
    assert upstream.queries["/air_traffic/publications/"] == [""]
    corpus = {p: q for p, q in upstream.queries.items() if p.startswith(upstream.PREFIX)}
    assert len(corpus) == len(FIXTURE_PAGES) + len(upstream.images)
    nonces = {q for qs in corpus.values() for q in qs}
    assert len(nonces) == 1
    (nonce,) = nonces
    assert re.fullmatch(r"far-aim-nocache=[0-9a-f]{16}", nonce)
    # Archived provenance keeps the canonical URL.
    metadata = json.loads(
        (config.raw_dir / "aim" / VERSION / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["index_url"] == INDEX_URL

    shutil.rmtree(config.raw_dir / "aim" / VERSION)
    upstream.queries.clear()
    fetch(config, upstream)
    again = {q for p, qs in upstream.queries.items() if p.startswith(upstream.PREFIX) for q in qs}
    assert again and again != nonces
