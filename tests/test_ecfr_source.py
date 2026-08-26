"""Phase 1 tests: eCFR Title 14 discovery, download, archive, idempotency.

All HTTP traffic goes through httpx.MockTransport — no network access.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from far_aim.config import Config
from far_aim.manifest import SourceManifest
from far_aim.sources import ecfr

FIXTURES = Path(__file__).parent / "fixtures" / "ecfr"
SAMPLE_XML = (FIXTURES / "title-14-sample.xml").read_bytes()
ISSUE_DATE = "2026-08-19"

TITLES_PAYLOAD = {
    "meta": {"date": ISSUE_DATE, "import_in_progress": False},
    "titles": [
        {"number": 1, "name": "General Provisions", "latest_issue_date": "2026-08-01"},
        {
            "number": 14,
            "name": "Aeronautics and Space",
            "latest_amended_on": "2026-08-17",
            "latest_issue_date": ISSUE_DATE,
            "up_to_date_as_of": ISSUE_DATE,
            "reserved": False,
        },
    ],
}

def no_sleep(_seconds: float) -> None:
    pass


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    """The fixture XML is tiny; scale the truncation guards down to match."""
    monkeypatch.setattr(ecfr, "MIN_XML_BYTES", 100)
    monkeypatch.setattr(ecfr, "MIN_SECTION_COUNT", 3)


def titles_payload(issue_date: str) -> dict:
    payload = json.loads(json.dumps(TITLES_PAYLOAD))
    payload["titles"][1]["latest_issue_date"] = issue_date
    return payload


class Upstream:
    """Configurable fake eCFR server."""

    def __init__(self, titles=None, xml=SAMPLE_XML, issue_date=ISSUE_DATE):
        self.issue_date = issue_date
        self.titles = titles if titles is not None else titles_payload(issue_date)
        self.xml = xml
        self.xml_headers: dict[str, str] = {"Content-Type": "application/xml; charset=utf-8"}
        self.titles_requests = 0
        self.xml_requests = 0
        self.fail_first_xml_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/versioner/v1/titles.json":
            self.titles_requests += 1
            return httpx.Response(200, json=self.titles)
        if request.url.path == f"/api/versioner/v1/full/{self.issue_date}/title-14.xml":
            self.xml_requests += 1
            if self.fail_first_xml_with is not None and self.xml_requests == 1:
                return httpx.Response(self.fail_first_xml_with)
            return httpx.Response(200, content=self.xml, headers=self.xml_headers)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config.load(tmp_path)
    SourceManifest.default().save(cfg.manifest_path)
    return cfg


def fetch(config, upstream, **kwargs):
    kwargs.setdefault("sleep", no_sleep)
    kwargs.setdefault("now", lambda: "2026-08-20T00:00:00Z")
    with upstream.client() as client:
        return ecfr.fetch_title14(config, client, **kwargs)


# ---------------------------------------------------------------- discovery


def test_discover_title14():
    upstream = Upstream()
    with upstream.client() as client:
        discovery = ecfr.discover_title14(client, sleep=no_sleep)
    assert discovery.latest_issue_date == ISSUE_DATE
    assert discovery.latest_amended_on == "2026-08-17"
    assert discovery.up_to_date_as_of == ISSUE_DATE


def test_discover_missing_title14_fails():
    upstream = Upstream(titles={"titles": [{"number": 1, "latest_issue_date": "2026-08-01"}]})
    with upstream.client() as client, pytest.raises(ecfr.FetchError, match="no entry"):
        ecfr.discover_title14(client, sleep=no_sleep)


def test_discover_malformed_issue_date_fails():
    titles = {"titles": [{"number": 14, "latest_issue_date": "not-a-date"}]}
    upstream = Upstream(titles=titles)
    with upstream.client() as client, pytest.raises(ecfr.FetchError, match="calendar date"):
        ecfr.discover_title14(client, sleep=no_sleep)


@pytest.mark.parametrize("bad_date", ["2026-02-31", "2026-13-01", "2026-00-10"])
def test_discover_impossible_calendar_date_fails(bad_date):
    upstream = Upstream(titles=titles_payload(bad_date))
    with upstream.client() as client, pytest.raises(ecfr.FetchError, match="calendar date"):
        ecfr.discover_title14(client, sleep=no_sleep)


def test_discover_drops_impossible_optional_dates():
    payload = titles_payload(ISSUE_DATE)
    payload["titles"][1]["latest_amended_on"] = "2026-02-30"
    upstream = Upstream(titles=payload)
    with upstream.client() as client:
        discovery = ecfr.discover_title14(client, sleep=no_sleep)
    assert discovery.latest_amended_on is None
    assert discovery.up_to_date_as_of == ISSUE_DATE


def test_discover_honors_retry_after_seconds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json=TITLES_PAYLOAD)

    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ecfr.discover_title14(client, sleep=delays.append)
    assert delays == [30.0]  # server-requested wait beats the 2s default backoff


def test_discover_retry_after_http_date_is_capped():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}
            )
        return httpx.Response(200, json=TITLES_PAYLOAD)

    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ecfr.discover_title14(client, sleep=delays.append)
    assert delays == [ecfr.RETRY_AFTER_MAX_SECONDS]


def test_discover_ignores_unparseable_retry_after():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "soon"})
        return httpx.Response(200, json=TITLES_PAYLOAD)

    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ecfr.discover_title14(client, sleep=delays.append)
    assert delays == [2.0]  # falls back to default backoff


def test_discover_retries_transient_errors():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=TITLES_PAYLOAD)

    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        discovery = ecfr.discover_title14(client, sleep=delays.append)
    assert discovery.latest_issue_date == ISSUE_DATE
    assert calls == 3
    assert delays == [2.0, 4.0]


def test_discover_waits_for_import_in_progress_to_clear():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = titles_payload(ISSUE_DATE)
        payload["meta"]["import_in_progress"] = calls < 3
        return httpx.Response(200, json=payload)

    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        discovery = ecfr.discover_title14(client, sleep=delays.append)
    assert discovery.latest_issue_date == ISSUE_DATE
    assert calls == 3
    assert delays == [2.0, 4.0]


def test_discover_fails_closed_if_import_never_finishes():
    payload = titles_payload(ISSUE_DATE)
    payload["meta"]["import_in_progress"] = True
    upstream = Upstream(titles=payload)
    with (
        upstream.client() as client,
        pytest.raises(ecfr.FetchError, match="import in progress"),
    ):
        ecfr.discover_title14(client, sleep=no_sleep)


def test_discover_gives_up_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ecfr.FetchError, match="failed after"),
    ):
        ecfr.discover_title14(client, sleep=no_sleep)


# ---------------------------------------------------------------- fetch


def test_fetch_downloads_archives_and_updates_manifest(config):
    upstream = Upstream()
    result = fetch(config, upstream)

    assert result.downloaded
    assert result.version == ISSUE_DATE
    assert result.section_count == 3
    assert result.xml_path == config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml"
    assert result.xml_path.read_bytes() == SAMPLE_XML
    assert result.raw_hash.startswith("sha256:")

    metadata = json.loads((result.xml_path.parent / "metadata.json").read_text())
    assert metadata["provider"] == "ecfr"
    assert metadata["source_version"] == ISSUE_DATE
    assert metadata["raw_hash"] == result.raw_hash
    assert metadata["byte_count"] == len(SAMPLE_XML)
    assert metadata["section_count"] == 3
    assert metadata["retrieved_at"] == "2026-08-20T00:00:00Z"

    manifest = SourceManifest.load(config.manifest_path)
    state = manifest.sources["ecfr_title_14"]
    assert state.accepted_version == ISSUE_DATE
    assert state.raw_hash == result.raw_hash
    assert state.last_checked_at == "2026-08-20T00:00:00Z"


def test_refetch_same_version_does_not_redownload(config):
    upstream = Upstream()
    fetch(config, upstream)
    assert upstream.xml_requests == 1

    result = fetch(config, upstream, now=lambda: "2026-08-21T00:00:00Z")
    assert not result.downloaded
    assert upstream.xml_requests == 1  # only titles.json was polled again

    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.last_checked_at == "2026-08-21T00:00:00Z"
    assert state.accepted_version == ISSUE_DATE


def test_refetch_after_cache_purge_redownloads_and_verifies(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    first.xml_path.unlink()

    result = fetch(config, upstream)
    assert result.downloaded
    assert result.raw_hash == first.raw_hash
    assert result.xml_path.read_bytes() == SAMPLE_XML


def test_refetch_hash_mismatch_fails_closed(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    first.xml_path.unlink()

    upstream.xml = SAMPLE_XML.replace(b"Rules of construction", b"Rules of destruction")
    with pytest.raises(ecfr.FetchError, match="different content"):
        fetch(config, upstream)

    # Manifest still records the originally accepted snapshot...
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.raw_hash == first.raw_hash
    # ...the new bytes are quarantined, not accepted.
    assert not first.xml_path.exists()
    assert (first.xml_path.parent / "title-14.xml.mismatch").exists()


def test_refetch_hash_mismatch_force_accepts_and_preserves_old_snapshot(config):
    upstream = Upstream()
    first = fetch(config, upstream)

    upstream.xml = SAMPLE_XML.replace(b"Rules of construction", b"Rules of interpretation")
    result = fetch(config, upstream, force=True)
    assert result.downloaded
    assert result.raw_hash != first.raw_hash
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.raw_hash == result.raw_hash

    # The last known-good bytes survive under a hash-qualified name: upstream
    # changed this point-in-time URL in place, so they may be unrecoverable
    # from the API.
    suffix = first.raw_hash.removeprefix("sha256:")[:12]
    superseded = first.xml_path.with_name(f"title-14.xml.superseded-{suffix}")
    assert superseded.read_bytes() == SAMPLE_XML
    assert result.xml_path.read_bytes() == upstream.xml


def _assert_consistent(config, upstream, expected_xml: bytes):
    """Archive, metadata and manifest all describe the same accepted bytes."""
    xml_path = config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml"
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert xml_path.read_bytes() == expected_xml
    assert ecfr.sha256_of(xml_path) == state.raw_hash
    metadata = json.loads((xml_path.parent / "metadata.json").read_text())
    assert metadata["raw_hash"] == state.raw_hash
    assert not (xml_path.parent / "title-14.xml.mismatch").exists()


def _crash(*args, **kwargs):
    raise OSError("disk full")


NEW_XML = SAMPLE_XML.replace(b"Rules of construction", b"Rules of interpretation")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _named(xml_path, kind: str, raw_hash: str):
    return xml_path.with_name(f"title-14.xml.{kind}-{raw_hash.removeprefix('sha256:')[:12]}")


def test_manifest_commit_failure_rolls_back_archive_in_process(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    upstream.xml = NEW_XML

    # Archive published, then the manifest commit fails.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SourceManifest, "save", _crash)
        with pytest.raises(OSError):
            fetch(config, upstream, force=True)

    # Manifest unchanged and the archive was rolled back to match it; the
    # validated-but-unaccepted bytes are parked, not lost.
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.raw_hash == first.raw_hash
    assert first.xml_path.read_bytes() == SAMPLE_XML
    assert _named(first.xml_path, "unaccepted", _sha(NEW_XML)).read_bytes() == NEW_XML
    assert not _named(first.xml_path, "superseded", first.raw_hash).exists()

    # Metadata was left describing the new bytes; a plain fetch heals it from
    # the verified archive without re-downloading.
    result = fetch(config, upstream)
    assert not result.downloaded
    assert upstream.xml_requests == 2  # first fetch + the failed forced one
    _assert_consistent(config, upstream, SAMPLE_XML)


def test_manifest_failure_after_rename_does_not_roll_back_archive(config):
    """A durability failure *after* the manifest rename leaves the new state committed."""
    upstream = Upstream()
    first = fetch(config, upstream)
    upstream.xml = NEW_XML
    real_save = SourceManifest.save

    def save_then_fail(self, path):
        real_save(self, path)  # rename happened: manifest now records the new hash
        raise OSError(errno.EIO, "directory fsync failed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SourceManifest, "save", save_then_fail)
        with pytest.raises(OSError):
            fetch(config, upstream, force=True)

    # Visible manifest records the new hash, so the archive must keep the new
    # bytes (no rollback); old bytes remain preserved for reference.
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.raw_hash == _sha(NEW_XML)
    assert first.xml_path.read_bytes() == NEW_XML
    assert _named(first.xml_path, "superseded", first.raw_hash).read_bytes() == SAMPLE_XML
    assert not _named(first.xml_path, "unaccepted", _sha(NEW_XML)).exists()
    # And the state is already consistent: a plain fetch is a verified no-op.
    result = fetch(config, upstream)
    assert not result.downloaded
    _assert_consistent(config, upstream, NEW_XML)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda m: m.pop("url"),
        lambda m: m.pop("provider"),
        lambda m: m.update(provider="faa"),
        lambda m: m.update(byte_count=m["byte_count"] + 1),
        lambda m: m.update(section_count=0),
        lambda m: m.update(retrieved_at="yesterday"),
        lambda m: m.update(latest_amended_on="2026-02-30"),
        lambda m: m.update(extra="field"),
    ],
    ids=[
        "missing-url",
        "missing-provider",
        "wrong-provider",
        "wrong-byte-count",
        "bad-section-count",
        "bad-timestamp",
        "bad-optional-date",
        "unknown-field",
    ],
)
def test_incomplete_metadata_is_regenerated(config, corrupt):
    upstream = Upstream()
    first = fetch(config, upstream)
    metadata_path = first.xml_path.parent / "metadata.json"
    good = json.loads(metadata_path.read_text())
    broken = dict(good)
    corrupt(broken)
    metadata_path.write_text(json.dumps(broken))

    result = fetch(config, upstream, now=lambda: "2026-08-21T00:00:00Z")
    assert not result.downloaded
    regenerated = json.loads(metadata_path.read_text())
    assert set(regenerated) == set(good)
    for key in good:
        if key != "retrieved_at":
            assert regenerated[key] == good[key]


def test_archive_publish_failure_midway_restores_old_bytes(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    upstream.xml = NEW_XML
    real_publish = ecfr._publish_xml

    def publish_then_fail(tmp_path, xml_path, *, new_hash, version):
        # Old bytes moved aside, then the final rename fails.
        real_publish(tmp_path, xml_path, new_hash=new_hash, version=version)
        os.replace(xml_path, tmp_path)
        raise OSError("rename failed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ecfr, "_publish_xml", publish_then_fail)
        with pytest.raises(OSError):
            fetch(config, upstream, force=True)

    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.raw_hash == first.raw_hash
    assert first.xml_path.read_bytes() == SAMPLE_XML
    result = fetch(config, upstream)
    assert not result.downloaded
    _assert_consistent(config, upstream, SAMPLE_XML)


def test_hard_crash_between_publish_and_commit_self_heals_offline(config):
    """Simulate a process kill after archive publish but before manifest commit."""
    upstream = Upstream()
    first = fetch(config, upstream)

    # Old bytes preserved as superseded, new bytes at the archive path,
    # metadata for the new bytes, manifest still recording the old hash.
    _plant_interrupted_acceptance(first)

    # Upstream is unreachable for the XML: healing must not need the network.
    upstream.fail_first_xml_with = 503
    result = fetch(config, upstream)
    assert not result.downloaded
    assert upstream.xml_requests == 1  # only the original fetch
    _assert_consistent(config, upstream, SAMPLE_XML)
    assert _named(first.xml_path, "unaccepted", _sha(NEW_XML)).read_bytes() == NEW_XML
    assert not _named(first.xml_path, "superseded", first.raw_hash).exists()


def _plant_interrupted_acceptance(first):
    """On-disk state after a kill between archive publish and manifest commit."""
    os.replace(first.xml_path, _named(first.xml_path, "superseded", first.raw_hash))
    first.xml_path.write_bytes(NEW_XML)
    metadata_path = first.xml_path.parent / "metadata.json"
    meta = json.loads(metadata_path.read_text())
    meta["raw_hash"] = _sha(NEW_XML)
    metadata_path.write_text(json.dumps(meta))


def test_accepted_archive_reconciled_even_when_upstream_advanced(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    _plant_interrupted_acceptance(first)

    # Upstream moved on to a newer issue whose download is unavailable.
    new_date = "2026-09-01"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/versioner/v1/titles.json":
            return httpx.Response(200, json=titles_payload(new_date))
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ecfr.FetchError, match="failed after"),
    ):
        ecfr.fetch_title14(config, client, sleep=no_sleep)

    # The new version failed, but the accepted snapshot is back at its
    # canonical path and the manifest still points at it.
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.accepted_version == ISSUE_DATE
    assert first.xml_path.read_bytes() == SAMPLE_XML
    assert _named(first.xml_path, "unaccepted", _sha(NEW_XML)).read_bytes() == NEW_XML
    assert not _named(first.xml_path, "superseded", first.raw_hash).exists()


def test_accepted_archive_reconciled_even_when_discovery_fails(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    _plant_interrupted_acceptance(first)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ecfr.FetchError),
    ):
        ecfr.fetch_title14(config, client, sleep=no_sleep)

    # Fully offline recovery: reconciled before any network call mattered.
    assert first.xml_path.read_bytes() == SAMPLE_XML
    assert _named(first.xml_path, "unaccepted", _sha(NEW_XML)).read_bytes() == NEW_XML


def test_manifest_never_records_unarchived_snapshot(config):
    """Whenever the manifest changes, the archive already holds the matching bytes."""
    upstream = Upstream()
    seen: list[tuple[str | None, bytes | None]] = []
    real_save = SourceManifest.save
    xml_path = config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml"

    def spy_save(self, path):
        state = self.sources["ecfr_title_14"]
        seen.append((state.raw_hash, xml_path.read_bytes() if xml_path.exists() else None))
        real_save(self, path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SourceManifest, "save", spy_save)
        fetch(config, upstream)
        upstream.xml = NEW_XML
        fetch(config, upstream, force=True)

    for raw_hash, archived in seen:
        if raw_hash is not None:
            assert archived is not None and _sha(archived) == raw_hash


def test_concurrent_fetch_fails_closed_while_lock_held(config):
    upstream = Upstream()
    with (
        ecfr.exclusive_lock(ecfr.fetch_lock_path(config)),
        pytest.raises(
            ecfr.FetchError, match="another far-aim fetch or parse is already in progress"
        ),
    ):
        fetch(config, upstream)
    assert upstream.titles_requests == 0  # refused before touching the network
    # Lock released: the same fetch now succeeds.
    assert fetch(config, upstream).downloaded


def test_lock_released_after_failed_fetch(config):
    upstream = Upstream(xml=SAMPLE_XML[: len(SAMPLE_XML) // 2])
    with pytest.raises(ecfr.FetchError):
        fetch(config, upstream)
    upstream.xml = SAMPLE_XML
    assert fetch(config, upstream).downloaded


def test_download_uses_unique_temp_files(config):
    upstream = Upstream()
    dests: list[Path] = []
    real_download = ecfr._download_xml

    def spy(client, url, dest, *, sleep):
        dests.append(dest)
        return real_download(client, url, dest, sleep=sleep)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ecfr, "_download_xml", spy)
        fetch(config, upstream)
        fetch(config, upstream, force=True)
    assert len(dests) == 2
    assert dests[0] != dests[1]
    for dest in dests:
        assert dest.name.startswith("title-14.xml.") and dest.name.endswith(".download")
        assert not dest.exists()  # cleaned up


def test_snapshot_dir_synced_before_manifest_commit(config):
    upstream = Upstream()
    events: list[str] = []
    snapshot_dir = config.raw_dir / "ecfr" / ISSUE_DATE
    real_fsync_dir = ecfr.fsync_dir
    real_save = SourceManifest.save

    def spy_fsync(path):
        if Path(path) == snapshot_dir:
            events.append("fsync-dir")
        real_fsync_dir(path)

    def spy_save(self, path):
        events.append("manifest-save")
        real_save(self, path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ecfr, "fsync_dir", spy_fsync)
        mp.setattr(SourceManifest, "save", spy_save)
        fetch(config, upstream)

    # Both metadata.json and title-14.xml renames are synced before the
    # manifest records the snapshot.
    assert events.count("fsync-dir") >= 2
    assert events.index("manifest-save") > events.index("fsync-dir")
    assert events[-1] == "manifest-save"


def test_superseded_copy_synced_before_replacement_installed(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    upstream.xml = NEW_XML
    superseded = _named(first.xml_path, "superseded", first.raw_hash)
    events: list[str] = []
    real_fsync_dir = ecfr.fsync_dir
    real_replace = os.replace

    def spy_fsync(path):
        if Path(path) == first.xml_path.parent:
            events.append("fsync-dir")
        real_fsync_dir(path)

    def spy_replace(src, dst):
        src, dst = Path(src), Path(dst)
        if dst == superseded:
            events.append("preserve-old")
        elif dst == first.xml_path:
            events.append("install-new")
        real_replace(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ecfr, "fsync_dir", spy_fsync)
        mp.setattr(ecfr.os, "replace", spy_replace)
        fetch(config, upstream, force=True)

    preserve = events.index("preserve-old")
    install = events.index("install-new")
    assert preserve < install
    assert "fsync-dir" in events[preserve + 1 : install]  # synced in between


def test_metadata_written_atomically(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    leftovers = [p.name for p in first.xml_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_force_refetch_identical_bytes_leaves_no_superseded_copy(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    result = fetch(config, upstream, force=True)
    assert result.downloaded
    assert result.raw_hash == first.raw_hash
    leftovers = [p.name for p in first.xml_path.parent.iterdir() if "superseded" in p.name]
    assert leftovers == []


def test_new_version_clears_stale_canonical_hash(config):
    upstream = Upstream()
    fetch(config, upstream)
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["ecfr_title_14"].canonical_hash = "sha256:stale"
    manifest.save(config.manifest_path)

    new_date = "2026-09-01"
    upstream.titles = json.loads(json.dumps(TITLES_PAYLOAD))
    upstream.titles["titles"][1]["latest_issue_date"] = new_date

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/versioner/v1/titles.json":
            return httpx.Response(200, json=upstream.titles)
        if request.url.path == f"/api/versioner/v1/full/{new_date}/title-14.xml":
            return httpx.Response(
                200, content=SAMPLE_XML, headers={"Content-Type": "application/xml"}
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ecfr.fetch_title14(
            config, client, sleep=no_sleep, now=lambda: "2026-09-01T00:00:00Z"
        )

    assert result.version == new_date
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.accepted_version == new_date
    assert state.canonical_hash is None
    # The previous accepted snapshot stays in the cache.
    assert (config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml").exists()


def test_upstream_version_rollback_rejected(config):
    fetch(config, Upstream())
    older = Upstream(issue_date="2026-08-01")
    with pytest.raises(ecfr.FetchError, match="refusing to roll back"):
        fetch(config, older)

    assert older.xml_requests == 0  # failed before any bulk download
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.accepted_version == ISSUE_DATE


def test_upstream_version_rollback_force_accepts(config):
    fetch(config, Upstream())
    older = Upstream(issue_date="2026-08-01")
    result = fetch(config, older, force=True)
    assert result.downloaded
    assert result.version == "2026-08-01"
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.accepted_version == "2026-08-01"


def test_missing_metadata_regenerated_without_redownload(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    metadata_path = first.xml_path.parent / "metadata.json"
    metadata_path.unlink()

    result = fetch(config, upstream)
    assert not result.downloaded
    assert upstream.xml_requests == 1  # regenerated from the verified cache
    assert result.section_count == 3
    metadata = json.loads(metadata_path.read_text())
    assert metadata["source_version"] == ISSUE_DATE
    assert metadata["raw_hash"] == first.raw_hash
    assert metadata["section_count"] == 3


def test_stale_metadata_regenerated(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    metadata_path = first.xml_path.parent / "metadata.json"
    metadata_path.write_text("not json", encoding="utf-8")

    result = fetch(config, upstream)
    assert not result.downloaded
    metadata = json.loads(metadata_path.read_text())
    assert metadata["raw_hash"] == first.raw_hash


def test_truncated_xml_rejected(config, monkeypatch):
    upstream = Upstream(xml=SAMPLE_XML[: len(SAMPLE_XML) // 2])
    with pytest.raises(ecfr.FetchError, match="not well-formed"):
        fetch(config, upstream)
    # Nothing accepted: no archive at the final path, manifest untouched.
    assert not (config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml").exists()
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.accepted_version is None
    assert state.raw_hash is None


def test_too_small_xml_rejected(config, monkeypatch):
    monkeypatch.setattr(ecfr, "MIN_XML_BYTES", 10_000_000)
    upstream = Upstream()
    with pytest.raises(ecfr.FetchError, match="suspiciously small"):
        fetch(config, upstream)


def test_too_few_sections_rejected(config, monkeypatch):
    monkeypatch.setattr(ecfr, "MIN_SECTION_COUNT", 100)
    upstream = Upstream()
    with pytest.raises(ecfr.FetchError, match="SECTION elements"):
        fetch(config, upstream)


def test_wrong_root_element_rejected(config):
    upstream = Upstream(
        xml=b'<?xml version="1.0"?><HTML><DIV8 TYPE="SECTION"/>'
        b'<DIV8 TYPE="SECTION"/><DIV8 TYPE="SECTION"/></HTML>'
    )
    with pytest.raises(ecfr.FetchError, match="root element"):
        fetch(config, upstream)


def test_wrong_content_type_rejected(config):
    upstream = Upstream()
    upstream.xml_headers = {"Content-Type": "text/html"}
    with pytest.raises(ecfr.FetchError, match="content-type"):
        fetch(config, upstream)


@pytest.mark.parametrize("content_type", ["Application/XML", "TEXT/XML; charset=UTF-8"])
def test_xml_content_type_is_case_insensitive(config, content_type):
    upstream = Upstream()
    upstream.xml_headers = {"Content-Type": content_type}
    assert fetch(config, upstream).downloaded


def test_download_honors_retry_after(config):
    upstream = Upstream()
    original = upstream.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("title-14.xml") and upstream.xml_requests == 1:
            return httpx.Response(429, headers={"Retry-After": "45"})
        return response

    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ecfr.fetch_title14(
            config, client, sleep=delays.append, now=lambda: "2026-08-20T00:00:00Z"
        )
    assert result.downloaded
    assert delays == [45.0]


def test_malformed_content_length_is_a_controlled_failure(config):
    upstream = Upstream()
    upstream.xml_headers = {
        "Content-Type": "application/xml",
        "Content-Length": "1093, 1093",
    }
    with pytest.raises(ecfr.FetchError, match="malformed Content-Length"):
        fetch(config, upstream)
    assert upstream.xml_requests == 1  # not retried: the server is misbehaving, not flaky
    assert not (config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml").exists()


def test_corrupt_compressed_body_is_retried(config):
    upstream = Upstream()
    original = upstream.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("title-14.xml") and upstream.xml_requests == 1:
            # Claims gzip but the body is garbage: httpx raises DecodingError
            # while streaming.
            return httpx.Response(
                200,
                content=b"\x1f\x8b\x08\x00not-really-gzip",
                headers={"Content-Type": "application/xml", "Content-Encoding": "gzip"},
            )
        return response

    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ecfr.fetch_title14(
            config, client, sleep=delays.append, now=lambda: "2026-08-20T00:00:00Z"
        )
    assert result.downloaded
    assert upstream.xml_requests == 2
    assert delays == [2.0]


def test_persistent_decoding_error_is_controlled_failure(config):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/versioner/v1/titles.json":
            return httpx.Response(200, json=TITLES_PAYLOAD)
        return httpx.Response(
            200,
            content=b"\x1f\x8b\x08\x00not-really-gzip",
            headers={"Content-Type": "application/xml", "Content-Encoding": "gzip"},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ecfr.FetchError, match="failed after"),
    ):
        ecfr.fetch_title14(config, client, sleep=no_sleep)


def _dir_fsync_raising(err: int):
    real_fsync = os.fsync

    def fake_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(err, os.strerror(err))
        real_fsync(fd)

    return fake_fsync


def test_unsupported_directory_fsync_is_tolerated(config):
    upstream = Upstream()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ecfr.os, "fsync", _dir_fsync_raising(errno.EINVAL))
        assert fetch(config, upstream).downloaded


def test_real_directory_fsync_failure_aborts_acceptance(config):
    upstream = Upstream()
    first = fetch(config, upstream)
    upstream.xml = NEW_XML

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ecfr.os, "fsync", _dir_fsync_raising(errno.EIO))
        with pytest.raises(OSError) as excinfo:
            fetch(config, upstream, force=True)
    assert excinfo.value.errno == errno.EIO

    # Nothing accepted: manifest unchanged, archive rolled back to known-good.
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.raw_hash == first.raw_hash
    assert first.xml_path.read_bytes() == SAMPLE_XML


def test_download_retries_transient_server_errors(config):
    upstream = Upstream()
    upstream.fail_first_xml_with = 502
    result = fetch(config, upstream)
    assert result.downloaded
    assert upstream.xml_requests == 2


def test_missing_registry_fails(tmp_path):
    upstream = Upstream()
    with pytest.raises(ecfr.FetchError, match="source registry missing"):
        fetch(Config.load(tmp_path), upstream)
