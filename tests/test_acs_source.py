"""Phase 10a tests: ACS discovery and acquisition.

Fixtures under ``tests/fixtures/acs/``: ``acs.html`` is the FAA ACS page's
"ACS List" table verbatim (page chrome outside the table removed), and
``private_airplane_acs_6.pdf`` is a page subset of the accepted
FAA-S-ACS-6C PDF (cover and front matter, Areas of Operation I, XI and
XII, the three appendices) assembled with pypdf — page content streams are
copied unchanged, so extracted text is byte-identical to the full
document's. All HTTP traffic goes through httpx.MockTransport.
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
from far_aim.parsers import acs as acs_parser
from far_aim.sources import acs
from far_aim.sources.common import FetchError
from tests.test_aim_source import no_sleep

FIXTURES = Path(__file__).parent / "fixtures" / "acs"
ACS_HTML = (FIXTURES / "acs.html").read_text(encoding="utf-8")
ACS_PDF = (FIXTURES / "private_airplane_acs_6.pdf").read_bytes()
PDF_URL = "https://www.faa.gov/training_testing/testing/acs/private_airplane_acs_6.pdf"
VERSION = "FAA-S-ACS-6C"
LABEL = "Private Pilot for Airplane Category (FAA-S-ACS-6C)"
FIXTURE_PAGES = 35
FIXTURE_AREAS = frozenset({"I", "XI", "XII"})


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    """The fixture PDF is a page subset; scale the truncation guards down to match."""
    monkeypatch.setattr(acs, "MIN_PAGES", 10)
    monkeypatch.setattr(acs, "MIN_ELEMENTS", 100)
    monkeypatch.setattr(acs_parser, "REQUIRE_COMPLETE", False)


class AcsUpstream:
    """Configurable fake FAA site: the ACS page and the PDF it links."""

    PAGE_PATH = "/training_testing/testing/acs"
    PDF_PATH = "/training_testing/testing/acs/private_airplane_acs_6.pdf"

    def __init__(self) -> None:
        self.page = ACS_HTML
        self.pdf = ACS_PDF
        self.requests: Counter[str] = Counter()
        self.queries: dict[str, list[str]] = {}
        self.status_overrides: dict[str, int] = {}
        self.pdf_content_type = "application/pdf"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests[path] += 1
        self.queries.setdefault(path, []).append(request.url.query.decode("ascii"))
        if path in self.status_overrides:
            return httpx.Response(self.status_overrides[path])
        if path == self.PAGE_PATH:
            return httpx.Response(
                200,
                content=self.page.encode("utf-8"),
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        if path == self.PDF_PATH:
            return httpx.Response(
                200, content=self.pdf, headers={"Content-Type": self.pdf_content_type}
            )
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def upstream() -> AcsUpstream:
    return AcsUpstream()


@pytest.fixture
def config(tmp_path) -> Config:
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    return config


def fetch(config: Config, upstream: AcsUpstream, **kwargs) -> acs.AcsFetchResult:
    with upstream.client() as client:
        return acs.fetch_acs(config, client, sleep=no_sleep, **kwargs)


def fetch_fixture(tmp_path) -> tuple[Config, acs.AcsFetchResult]:
    """Accept the fixture PDF into a fresh project root (shared by other suites)."""
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    with mock.patch.multiple(acs, MIN_PAGES=10, MIN_ELEMENTS=100):
        return config, fetch(config, AcsUpstream())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_acs(upstream):
    with upstream.client() as client:
        discovery = acs.discover_acs(client, sleep=no_sleep)
    assert discovery.version == VERSION
    assert discovery.label == LABEL
    assert discovery.title == "Private Pilot for Airplane Category"
    assert discovery.publication_date == "April 2024"
    assert discovery.effective_date == "2024-05-31"
    assert discovery.pdf_url == PDF_URL


def test_parse_acs_page_requires_exactly_one_listing():
    duplicated = ACS_HTML.replace(
        "Private Pilot for Airplane Category (FAA-S-ACS-6C)",
        "Private Pilot for Airplane Category (FAA-S-ACS-6C)",
    )
    row = re.search(r"<tr>\s*<td><a[^>]*private_airplane_acs_6\.pdf.*?</tr>", ACS_HTML, re.S)
    assert row is not None
    with pytest.raises(FetchError, match="found 2"):
        acs.parse_acs_page(duplicated.replace(row.group(0), row.group(0) * 2))
    with pytest.raises(FetchError, match="found 0"):
        acs.parse_acs_page(ACS_HTML.replace(row.group(0), ""))


def test_parse_acs_page_rejects_foreign_origin_and_non_pdf():
    foreign = ACS_HTML.replace(
        '"/training_testing/testing/acs/private_airplane_acs_6.pdf"',
        '"https://example.com/private_airplane_acs_6.pdf"',
    )
    with pytest.raises(FetchError, match="outside the approved FAA origin"):
        acs.parse_acs_page(foreign)
    html = ACS_HTML.replace("private_airplane_acs_6.pdf", "private_airplane_acs_6.html")
    with pytest.raises(FetchError, match="does not link a PDF"):
        acs.parse_acs_page(html)


def test_parse_acs_page_requires_an_effective_date():
    row = re.search(r"<tr>\s*<td><a[^>]*private_airplane_acs_6\.pdf.*?</tr>", ACS_HTML, re.S)
    assert row is not None
    broken = ACS_HTML.replace(
        row.group(0), row.group(0).replace("Effective May 31, 2024", "Coming soon")
    )
    with pytest.raises(FetchError, match="no parseable effective date"):
        acs.parse_acs_page(broken)


def test_document_number_ordering():
    assert acs.document_number_key("FAA-S-ACS-6C") == (6, "C")
    assert acs.document_number_key("FAA-S-ACS-6") == (6, "")
    assert acs.document_number_key("FAA-S-ACS-6") < acs.document_number_key("FAA-S-ACS-6A")
    assert acs.document_number_key("FAA-S-ACS-6C") < acs.document_number_key("FAA-S-ACS-7")
    with pytest.raises(ValueError):
        acs.document_number_key("FAA-G-ACS-2")


# ---------------------------------------------------------------------------
# Fetch / archive / manifest
# ---------------------------------------------------------------------------


def test_fetch_downloads_archives_and_updates_manifest(config, upstream):
    result = fetch(config, upstream)
    assert result.downloaded
    assert result.version == VERSION
    assert result.page_count == FIXTURE_PAGES
    assert result.element_count > 100
    snapshot = config.raw_dir / "acs" / VERSION
    assert result.snapshot_dir == snapshot
    assert result.pdf_path == snapshot / "private_airplane_acs_6.pdf"
    assert result.pdf_path.read_bytes() == ACS_PDF
    metadata = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["provider"] == "faa"
    assert metadata["publication"] == "acs"
    assert metadata["document_number"] == VERSION
    assert metadata["edition_label"] == LABEL
    assert metadata["effective_date"] == "2024-05-31"
    assert metadata["pdf_url"] == PDF_URL
    assert metadata["file"] == "private_airplane_acs_6.pdf"
    assert metadata["raw_hash"] == result.raw_hash
    assert acs.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)

    state = SourceManifest.load(config.manifest_path).sources[acs.SOURCE_NAME]
    assert state.accepted_version == VERSION
    assert state.effective_date == "2024-05-31"
    assert state.change is None
    assert state.raw_hash == result.raw_hash
    assert state.edition_label == LABEL
    assert state.source_url == PDF_URL
    assert state.canonical_hash is None
    assert state.last_checked_at is not None


def test_refetch_same_document_does_not_redownload(config, upstream):
    first = fetch(config, upstream)
    second = fetch(config, upstream)
    assert not second.downloaded
    assert second.raw_hash == first.raw_hash
    assert upstream.requests[AcsUpstream.PDF_PATH] == 1


def test_refetch_hash_mismatch_fails_closed(config, upstream):
    first = fetch(config, upstream)
    shutil.rmtree(config.raw_dir / "acs" / VERSION)
    upstream.pdf = ACS_PDF + b"\n%% trailing edit\n"
    with pytest.raises(FetchError, match="different content"):
        fetch(config, upstream)
    state = SourceManifest.load(config.manifest_path).sources[acs.SOURCE_NAME]
    assert state.raw_hash == first.raw_hash
    quarantined = [p for p in (config.raw_dir / "acs").iterdir() if ".mismatch-" in p.name]
    assert len(quarantined) == 1
    result = fetch(config, upstream, force=True)
    assert result.downloaded and result.raw_hash != first.raw_hash


def test_cover_page_must_name_the_listed_document(config, upstream):
    upstream.page = upstream.page.replace("(FAA-S-ACS-6C)", "(FAA-S-ACS-6D)")
    with pytest.raises(FetchError, match="cover page does not name document FAA-S-ACS-6D"):
        fetch(config, upstream)
    assert not (config.raw_dir / "acs").exists() or not any(
        (config.raw_dir / "acs").iterdir()
    )
    state = SourceManifest.load(config.manifest_path).sources[acs.SOURCE_NAME]
    assert state.accepted_version is None


def test_non_pdf_and_truncated_downloads_are_rejected(config, upstream):
    upstream.pdf = b"<html>not a pdf</html>"
    upstream.pdf_content_type = "application/pdf"
    with pytest.raises(FetchError, match="not a PDF"):
        fetch(config, upstream)
    upstream.pdf = ACS_PDF[: len(ACS_PDF) // 2]
    with pytest.raises(FetchError, match="unreadable PDF|truncated"):
        fetch(config, upstream)
    upstream.pdf = ACS_PDF
    upstream.pdf_content_type = "text/html"
    with pytest.raises(FetchError, match="expected pdf"):
        fetch(config, upstream)


def test_too_few_elements_or_pages_fails_closed(config, upstream, monkeypatch):
    monkeypatch.setattr(acs, "MIN_ELEMENTS", 5000)
    with pytest.raises(FetchError, match="element codes"):
        fetch(config, upstream)
    monkeypatch.setattr(acs, "MIN_ELEMENTS", 100)
    monkeypatch.setattr(acs, "MIN_PAGES", 500)
    with pytest.raises(FetchError, match="pages"):
        fetch(config, upstream)


def test_upstream_rollback_is_refused_without_force(config, upstream):
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources[acs.SOURCE_NAME] = SourceState(
        accepted_version="FAA-S-ACS-6D",
        effective_date="2027-01-01",
        raw_hash="sha256:" + "0" * 64,
    )
    manifest.save(config.manifest_path)
    with pytest.raises(FetchError, match="older than accepted"):
        fetch(config, upstream)
    result = fetch(config, upstream, force=True)
    assert result.downloaded
    state = SourceManifest.load(config.manifest_path).sources[acs.SOURCE_NAME]
    assert state.accepted_version == VERSION


def test_new_document_clears_stale_canonical_hash(config, upstream):
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources[acs.SOURCE_NAME] = SourceState(
        accepted_version="FAA-S-ACS-6B",
        effective_date="2019-06-06",
        raw_hash="sha256:" + "0" * 64,
        canonical_hash="sha256:" + "1" * 64,
    )
    manifest.save(config.manifest_path)
    fetch(config, upstream)
    state = SourceManifest.load(config.manifest_path).sources[acs.SOURCE_NAME]
    assert state.accepted_version == VERSION
    assert state.canonical_hash is None


def test_relabelled_listing_is_refused_without_force(config, upstream):
    fetch(config, upstream)
    upstream.page = upstream.page.replace("Effective May 31, 2024", "Effective June 1, 2024")
    with pytest.raises(FetchError, match="re-run with --force"):
        fetch(config, upstream)


def test_altered_archive_provenance_is_not_reported_verified(config, upstream):
    fetch(config, upstream)
    path = config.raw_dir / "acs" / VERSION / "metadata.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["edition_label"] = "Something else (FAA-S-ACS-6C)"
    data["title"] = "Something else"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(FetchError, match="provenance was altered"):
        fetch(config, upstream)
    assert fetch(config, upstream, force=True).downloaded
    assert not fetch(config, upstream).downloaded


def test_verify_snapshot_rejects_extra_and_tampered_files(config, upstream):
    result = fetch(config, upstream)
    snapshot = config.raw_dir / "acs" / VERSION
    extra = snapshot / "notes.txt"
    extra.write_text("scratch", encoding="utf-8")
    assert not acs.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)
    extra.unlink()
    assert acs.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)
    pdf = snapshot / "private_airplane_acs_6.pdf"
    pdf.write_bytes(pdf.read_bytes() + b" ")
    assert not acs.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)


def test_verify_snapshot_reconciles_metadata_counts(config, upstream):
    result = fetch(config, upstream)
    snapshot = config.raw_dir / "acs" / VERSION
    path = snapshot / "metadata.json"
    original = path.read_text(encoding="utf-8")
    for key, value in (("byte_count", -1), ("page_count", 99), ("element_count", 5000)):
        data = json.loads(original)
        data[key] = value
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert not acs.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash), key
    path.write_text(original, encoding="utf-8")
    assert acs.verify_snapshot(snapshot, version=VERSION, raw_hash=result.raw_hash)


def test_manifest_commit_failure_leaves_manifest_unchanged(config, upstream, monkeypatch):
    real_save = SourceManifest.save

    def failing_save(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(SourceManifest, "save", failing_save)
    with pytest.raises(OSError):
        fetch(config, upstream)
    monkeypatch.setattr(SourceManifest, "save", real_save)
    state = SourceManifest.load(config.manifest_path).sources[acs.SOURCE_NAME]
    assert state.accepted_version is None
    result = fetch(config, upstream)
    assert result.downloaded


def test_pdf_request_is_cache_busted(config, upstream):
    fetch(config, upstream)
    assert upstream.queries[AcsUpstream.PAGE_PATH] == [""]
    (query,) = upstream.queries[AcsUpstream.PDF_PATH]
    assert re.fullmatch(r"far-aim-nocache=[0-9a-f]{16}", query)
