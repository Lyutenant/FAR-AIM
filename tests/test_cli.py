import json
import os
import shutil

import httpx
import pytest

from far_aim.cli import EXIT_ERROR, EXIT_NOT_IMPLEMENTED, EXIT_OK, main
from far_aim.config import Config
from far_aim.manifest import SourceManifest, SourceState
from far_aim.parsers import aim as aim_parser
from far_aim.parsers import pcg as pcg_parser
from far_aim.sources import aim as aim_source
from far_aim.sources import ecfr
from far_aim.sources import pcg as pcg_src
from tests.test_aim_source import FIXTURE_APPENDICES, FIXTURE_CHAPTERS, AimUpstream
from tests.test_aim_source import VERSION as AIM_VERSION
from tests.test_ecfr_source import FIXTURES as FIXTURES_DIR
from tests.test_ecfr_source import ISSUE_DATE, Upstream, titles_payload
from tests.test_pcg_source import FIXTURE_LETTERS as PCG_LETTERS
from tests.test_pcg_source import VERSION as PCG_VERSION
from tests.test_pcg_source import PcgUpstream


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "far-aim" in capsys.readouterr().out


def test_check_without_manifest(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "check"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "not created yet" in out
    for source in ("ecfr_title_14", "aim", "pcg"):
        assert source in out


def test_check_with_manifest(tmp_path, capsys):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["aim"] = SourceState(
        last_checked_at="2026-08-20T00:00:00Z", effective_date="2026-07-09", change=3
    )
    manifest.save(config.manifest_path)
    assert main(["--root", str(tmp_path), "check"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "2026-07-09 (change 3)" in out
    assert "2026-08-20T00:00:00Z" in out


def test_check_with_corrupt_manifest(tmp_path, capsys):
    config = Config.load(tmp_path)
    config.manifest_path.parent.mkdir(parents=True)
    config.manifest_path.write_text("{}", encoding="utf-8")
    assert main(["--root", str(tmp_path), "check"]) == EXIT_ERROR
    assert "error" in capsys.readouterr().err


def test_validate_missing_registry_fails(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "source registry missing" in capsys.readouterr().err


def test_validate_valid_manifest(tmp_path, capsys):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "manifest schema valid" in capsys.readouterr().out


def test_validate_invalid_manifest(tmp_path, capsys):
    config = Config.load(tmp_path)
    config.manifest_path.parent.mkdir(parents=True)
    config.manifest_path.write_text('{"schema_version": 99, "sources": {}}', encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "schema_version" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["normalize"],
    ],
)
def test_unimplemented_commands_exit_2(tmp_path, capsys, argv):
    assert main(["--root", str(tmp_path), *argv]) == EXIT_NOT_IMPLEMENTED
    err = capsys.readouterr().err
    assert "not implemented" in err
    assert "Phase" in err


def test_unknown_corpus_rejected(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        main(["--root", str(tmp_path), "fetch", "phak"])
    assert excinfo.value.code == 2


@pytest.fixture
def mock_upstream(monkeypatch):
    """Fake eCFR *and* FAA upstreams behind one client (all sources are polled)."""
    upstream = Upstream()
    upstream.aim = AimUpstream()
    upstream.pcg = PcgUpstream()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.ecfr.gov":
            return upstream.handler(request)
        if request.url.path.startswith(PcgUpstream.PREFIX):
            return upstream.pcg.handler(request)
        return upstream.aim.handler(request)

    def client() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(ecfr, "make_client", client)
    monkeypatch.setattr(ecfr, "MIN_XML_BYTES", 100)
    monkeypatch.setattr(ecfr, "MIN_SECTION_COUNT", 3)
    monkeypatch.setattr(ecfr.time, "sleep", lambda _s: None)
    monkeypatch.setattr(aim_source, "make_client", client)
    monkeypatch.setattr(aim_source, "MIN_PAGES", 3)
    monkeypatch.setattr(aim_source, "MIN_PARAGRAPHS", 3)
    monkeypatch.setattr(aim_source, "MIN_FIGURES", 2)
    monkeypatch.setattr(aim_source, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(aim_source, "REQUIRED_CHAPTERS", FIXTURE_CHAPTERS)
    monkeypatch.setattr(aim_source, "REQUIRED_APPENDICES", FIXTURE_APPENDICES)
    monkeypatch.setattr(aim_source.time, "sleep", lambda _s: None)
    monkeypatch.setattr(aim_parser, "REQUIRE_RESOLVED_REFERENCES", False)
    monkeypatch.setattr(pcg_src, "make_client", client)
    monkeypatch.setattr(pcg_src, "MIN_PAGES", 3)
    monkeypatch.setattr(pcg_src, "MIN_TERMS", 5)
    monkeypatch.setattr(pcg_src, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(pcg_src, "REQUIRED_LETTERS", PCG_LETTERS)
    monkeypatch.setattr(pcg_src.time, "sleep", lambda _s: None)
    monkeypatch.setattr(pcg_parser, "REQUIRE_RESOLVED_REFERENCES", False)
    return upstream


def test_fetch_ecfr_end_to_end(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)

    assert main(["--root", str(tmp_path), "fetch", "ecfr"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"accepted: eCFR Title 14 issue {ISSUE_DATE}" in out
    assert (config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml").exists()

    assert main(["--root", str(tmp_path), "fetch", "ecfr"]) == EXIT_OK
    assert "unchanged" in capsys.readouterr().out
    assert mock_upstream.xml_requests == 1


def test_fetch_ecfr_without_registry_fails(tmp_path, capsys, mock_upstream):
    assert main(["--root", str(tmp_path), "fetch", "ecfr"]) == EXIT_ERROR
    assert "source registry missing" in capsys.readouterr().err


def test_fetch_ecfr_filesystem_failure_is_controlled(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    # A regular file where the raw cache directory should be makes mkdir fail.
    config.data_dir.mkdir(exist_ok=True)
    config.raw_dir.write_text("not a directory", encoding="utf-8")

    assert main(["--root", str(tmp_path), "fetch", "ecfr"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "filesystem failure" in captured.err
    assert "Traceback" not in captured.err
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.accepted_version is None


def test_check_remote_reports_update_available(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"update available — latest issue {ISSUE_DATE}, accepted none" in out


def test_check_remote_reports_up_to_date(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version=ISSUE_DATE)
    manifest.save(config.manifest_path)
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_OK
    assert f"up to date (issue {ISSUE_DATE})" in capsys.readouterr().out


def test_check_remote_reports_rollback_as_error(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version="2026-09-01")
    manifest.save(config.manifest_path)
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "older than accepted 2026-09-01" in captured.err
    assert "ecfr_title_14: update available" not in captured.out


def test_check_remote_upstream_failure(tmp_path, capsys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(
        ecfr, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(ecfr.time, "sleep", lambda _s: None)
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_ERROR
    assert "error" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# parse ecfr (Phase 2)
# ---------------------------------------------------------------------------

SLICE_XML = FIXTURES_DIR / "part-91-slice.xml"


def _accepted_snapshot(tmp_path, xml_bytes: bytes, version: str = ISSUE_DATE) -> Config:
    """A root directory with an accepted snapshot + matching manifest/metadata."""
    import hashlib

    config = Config.load(tmp_path)
    raw_hash = f"sha256:{hashlib.sha256(xml_bytes).hexdigest()}"
    snapshot_dir = config.raw_dir / "ecfr" / version
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "title-14.xml").write_bytes(xml_bytes)
    (snapshot_dir / "metadata.json").write_text(
        json.dumps(
            {
                "provider": "ecfr",
                "title_number": 14,
                "source_version": version,
                "url": ecfr.full_title14_url(version),
                "retrieved_at": "2026-08-20T10:05:13Z",
                "raw_hash": raw_hash,
                "byte_count": len(xml_bytes),
                "section_count": 6363,
                "latest_amended_on": None,
                "up_to_date_as_of": None,
            }
        ),
        encoding="utf-8",
    )
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(
        last_checked_at="2026-08-20T10:05:13Z",
        accepted_version=version,
        raw_hash=raw_hash,
    )
    manifest.save(config.manifest_path)
    return config


def test_parse_ecfr_writes_canonical_json(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "part-0091.json" in out
    out_path = config.normalized_dir / "ecfr" / "part-0091.json"
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["id"] == "cfr-14-part-91"
    assert doc["source"]["source_version"] == ISSUE_DATE
    assert doc["source"]["retrieved_at"] == "2026-08-20T10:05:13Z"

    # Re-running with unchanged sources must be byte-identical (plan §32.10).
    first = out_path.read_bytes()
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    assert out_path.read_bytes() == first


def test_parse_ecfr_without_accepted_snapshot(tmp_path, capsys):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    assert "no accepted eCFR snapshot" in capsys.readouterr().err


def test_parse_ecfr_checksum_mismatch(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    xml_path = config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml"
    xml_path.write_bytes(SLICE_XML.read_bytes() + b"\n<!-- tampered -->")
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    assert "does not match the accepted checksum" in capsys.readouterr().err


def test_parse_ecfr_unknown_part(tmp_path, capsys):
    _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "137"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "not present in this title" in err
    assert "nothing was written" in err


def test_parse_ecfr_full_records_canonical_hash(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "manifest updated: canonical_hash sha256:" in out
    manifest = SourceManifest.load(config.manifest_path)
    recorded = manifest.sources["ecfr_title_14"].canonical_hash
    assert recorded is not None and recorded.startswith("sha256:")

    # Re-parsing unchanged sources leaves the recorded hash untouched.
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    assert "canonical_hash unchanged" in capsys.readouterr().out
    assert SourceManifest.load(config.manifest_path).sources[
        "ecfr_title_14"
    ].canonical_hash == recorded

    # A partial parse never touches the recorded hash.
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    assert SourceManifest.load(config.manifest_path).sources[
        "ecfr_title_14"
    ].canonical_hash == recorded


def test_validate_verifies_normalized_layer(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "normalized eCFR layer verified" in capsys.readouterr().out

    # Tampering with a normalized file must fail validation loudly.
    part_file = config.normalized_dir / "ecfr" / "part-0091.json"
    doc = json.loads(part_file.read_text(encoding="utf-8"))
    doc["heading"] = "TAMPERED"
    part_file.write_text(json.dumps(doc), encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "does not match its content" in capsys.readouterr().err


def test_full_parse_removes_stale_part_files(tmp_path, capsys):
    """A part removed upstream must not survive as a stale normalized file."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    stale = config.normalized_dir / "ecfr" / "part-9999.json"
    stale.write_text("{}", encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    assert not stale.exists()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "normalized eCFR layer verified" in capsys.readouterr().out


def test_full_parse_rolls_back_on_manifest_failure(tmp_path, capsys, monkeypatch):
    """If the manifest commit fails, the previous normalized layer survives."""
    from far_aim.manifest import SourceManifest as ManifestClass

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    # A marker only the old layer has: rollback must bring it back.
    marker = config.normalized_dir / "ecfr" / "part-9999.json"
    marker.write_text("{}", encoding="utf-8")
    # Clear the recorded hash so the next full parse must commit the manifest.
    manifest = ManifestClass.load(config.manifest_path)
    manifest.sources["ecfr_title_14"].canonical_hash = None
    manifest.save(config.manifest_path)

    def failing_save(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(ManifestClass, "save", failing_save)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "manifest commit failed" in err
    assert "rolled back to the pre-parse state" in err
    assert marker.exists()  # the old layer is back in place
    assert not (config.normalized_dir / ".ecfr-staging").exists()
    assert not (config.normalized_dir / ".ecfr-previous").exists()


def test_validate_rejects_misnamed_part_file(tmp_path, capsys):
    """A valid document copied under another part's filename must fail."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    (out_dir / "part-0090.json").write_bytes((out_dir / "part-0091.json").read_bytes())
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "expected filename" in capsys.readouterr().err


def test_parse_fails_closed_while_lock_held(tmp_path, capsys):
    """The whole parse runs under the shared source lock (no fetch races)."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    with ecfr.exclusive_lock(ecfr.fetch_lock_path(config)):
        assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    assert "already in progress" in capsys.readouterr().err


def test_interrupted_swap_layer_is_restored_not_deleted(tmp_path, capsys):
    """A crash between the swap renames leaves the only copy of the layer at
    `.ecfr-previous`; the next publish must restore it, never discard it."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    os.rename(out_dir, previous)  # simulate the crash window
    marker = previous / "part-9999.json"
    marker.write_text("{}", encoding="utf-8")

    # Make the publish fail after recovery (manifest commit fails, commit
    # not visible), so the *recovered* layer must survive as the fallback.
    from far_aim.manifest import SourceManifest as ManifestClass

    manifest = ManifestClass.load(config.manifest_path)
    manifest.sources["ecfr_title_14"].canonical_hash = None
    manifest.save(config.manifest_path)

    def failing_save(self, path):
        raise OSError("disk full")

    capsys.readouterr()
    real_save = ManifestClass.save
    try:
        ManifestClass.save = failing_save
        assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    finally:
        ManifestClass.save = real_save
    output = capsys.readouterr()
    assert "restored interrupted normalized layer" in output.out
    assert "rolled back to the pre-parse state" in output.err
    # The recovered old layer (with its marker) is back at the canonical path.
    assert (out_dir / "part-9999.json").exists()
    assert not previous.exists()
    assert not (config.normalized_dir / ".ecfr-staging").exists()


def test_swap_failure_restores_previous_layer(tmp_path, capsys, monkeypatch):
    """If installing the staged layer fails mid-swap, the old layer returns."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    marker = out_dir / "part-9999.json"
    marker.write_text("{}", encoding="utf-8")

    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if str(src).endswith(".ecfr-staging") and str(dst).endswith("ecfr"):
            raise OSError("boom")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    assert "rolled back to the pre-parse state" in capsys.readouterr().err
    assert marker.exists()  # old layer back at its canonical path
    assert not (config.normalized_dir / ".ecfr-staging").exists()
    assert not (config.normalized_dir / ".ecfr-previous").exists()


def test_visible_manifest_commit_is_not_rolled_back(tmp_path, capsys):
    """save() failing *after* its rename became visible must keep the new
    layer — rolling back would desynchronize manifest and canonical data."""
    from far_aim.manifest import SourceManifest as ManifestClass

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    manifest = ManifestClass.load(config.manifest_path)
    manifest.sources["ecfr_title_14"].canonical_hash = None
    manifest.save(config.manifest_path)

    real_save = ManifestClass.save

    def save_then_fail(self, path):
        real_save(self, path)
        raise OSError("directory fsync failed")

    capsys.readouterr()
    try:
        ManifestClass.save = save_then_fail
        assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    finally:
        ManifestClass.save = real_save
    err = capsys.readouterr().err
    assert "after committing" in err
    assert "kept" in err
    # Manifest and layer agree: validation passes despite the reported error.
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


TWO_PART_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ECFR><DIV1 N="14" TYPE="TITLE"><HEAD>Title 14</HEAD>
<DIV3 N="I" TYPE="CHAPTER"><HEAD>CHAPTER I\xe2\x80\x94TEST</HEAD>
<DIV5 N="9" TYPE="PART"><HEAD>PART 9\xe2\x80\x94ALPHA</HEAD>
<DIV8 N="9.1" TYPE="SECTION"><HEAD>\xc2\xa7 9.1   One.</HEAD><P>(a) Alpha text.</P></DIV8>
</DIV5>
<DIV5 N="10" TYPE="PART"><HEAD>PART 10\xe2\x80\x94BETA</HEAD>
<DIV8 N="10.1" TYPE="SECTION"><HEAD>\xc2\xa7 10.1   Two.</HEAD><P>(a) Beta text.</P></DIV8>
</DIV5>
</DIV3></DIV1></ECFR>
"""


def test_partial_parse_rolls_back_all_files_on_failure(tmp_path, capsys, monkeypatch):
    """With several --part targets, a failed write restores every touched file."""
    config = _accepted_snapshot(tmp_path, TWO_PART_XML)
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    # Tamper part 9's provenance timestamp — a volatile field excluded
    # from canonical hashing whose exact value the stale-layer deep check
    # does not pin, so the gate still passes — to make a successful
    # in-place update visibly change it back.
    part9 = out_dir / "part-0009.json"
    doc = json.loads(part9.read_text(encoding="utf-8"))
    doc["source"]["retrieved_at"] = "2001-01-01T00:00:00Z"
    part9.write_text(json.dumps(doc), encoding="utf-8")
    tampered = part9.read_bytes()
    part10_before = (out_dir / "part-0010.json").read_bytes()

    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        # Fail only the initial write (its temp files end in ".tmp"); the
        # rollback path writes via ".restore" temp files and must succeed.
        if str(src).endswith(".tmp") and str(dst).endswith("part-0010.json"):
            raise OSError("boom")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    capsys.readouterr()
    argv = ["--root", str(tmp_path), "parse", "ecfr", "--part", "9", "--part", "10"]
    assert main(argv) == EXIT_ERROR
    assert "normalized layer left untouched" in capsys.readouterr().err
    # Part 9 was written before the failure; rollback must undo it.
    assert (out_dir / "part-0009.json").read_bytes() == tampered
    assert (out_dir / "part-0010.json").read_bytes() == part10_before


def test_first_publish_removes_layer_on_manifest_failure(tmp_path, capsys):
    """With no earlier layer, a failed manifest commit must not leave the
    new normalized output behind uncommitted."""
    from far_aim.manifest import SourceManifest as ManifestClass

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())

    def failing_save(self, path):
        raise OSError("disk full")

    real_save = ManifestClass.save
    capsys.readouterr()
    try:
        ManifestClass.save = failing_save
        assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    finally:
        ManifestClass.save = real_save
    assert "rolled back to the pre-parse state" in capsys.readouterr().err
    assert not (config.normalized_dir / "ecfr").exists()
    assert not (config.normalized_dir / ".ecfr-staging").exists()


def test_validate_rejects_non_object_part_file(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    (config.normalized_dir / "ecfr" / "part-0091.json").write_text("[]", encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "not a JSON object" in capsys.readouterr().err


def test_recovery_prefers_manifest_committed_layer(tmp_path, capsys, monkeypatch):
    """Crash between swap and manifest commit: the next parse must keep the
    manifest-matching layer as the fallback, not the uncommitted one."""
    import shutil

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    # Simulate the crash window: committed layer displaced to `previous`
    # (marked so restoration is observable), an uncommitted divergent layer
    # at the canonical path.
    os.rename(out_dir, previous)
    (previous / "marker.txt").write_text("committed layer", encoding="utf-8")
    shutil.copytree(previous, out_dir)
    (out_dir / "marker.txt").unlink()
    part91 = out_dir / "part-0091.json"
    doc = json.loads(part91.read_text(encoding="utf-8"))
    doc["canonical_hash"] = "sha256:" + "0" * 64  # divergent, uncommitted
    part91.write_text(json.dumps(doc), encoding="utf-8")

    # Make this publish fail during staging, after recovery ran.
    import far_aim.cli as cli_mod

    def failing_write(path, obj):
        raise OSError("disk full")

    monkeypatch.setattr(cli_mod, "write_json_atomic", failing_write)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    output = capsys.readouterr()
    assert "reinstated the previous normalized layer" in output.out
    assert "filesystem failure during parse" in output.err
    # The committed layer (with its marker) is back at the canonical path,
    # so manifest and normalized data still agree.
    assert (out_dir / "marker.txt").exists()
    assert not previous.exists()
    assert not (config.normalized_dir / ".ecfr-staging").exists()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_recovery_discards_superseded_previous_layer(tmp_path, capsys):
    """`previous` alongside a manifest-matching layer (crash after commit,
    before cleanup) is superseded and removed; the parse proceeds normally."""
    import shutil

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    shutil.copytree(out_dir, previous)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    assert "reinstated" not in capsys.readouterr().out
    assert not previous.exists()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_recovery_arbitration_checks_provenance(tmp_path, capsys):
    """Two layers with the recorded content hash, but the current one
    carrying tampered provenance: canonical hashes exclude source blocks,
    so hash-only arbitration would keep the layer `validate` rejects and
    delete the intact fallback. Recovery must reinstate the fallback."""
    import shutil

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    # Crash window between the swap and cleanup: both layers on disk,
    # content-identical.
    shutil.copytree(out_dir, previous)
    # Tamper only the current layer's provenance — its content hash still
    # matches the manifest.
    part_file = out_dir / "part-0091.json"
    doc = json.loads(part_file.read_text(encoding="utf-8"))
    doc["source"]["source_version"] = "1999-01-01"
    part_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    assert "reinstated the previous normalized layer" in capsys.readouterr().out
    assert not previous.exists()
    restored = json.loads(part_file.read_text(encoding="utf-8"))
    assert restored["source"]["source_version"] == ISSUE_DATE
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_parse_rejects_non_object_metadata(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    metadata = config.raw_dir / "ecfr" / ISSUE_DATE / "metadata.json"
    metadata.write_text("[]", encoding="utf-8")
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "snapshot metadata missing or unreadable" in err
    assert "Traceback" not in err


def test_parse_rejects_metadata_for_wrong_snapshot(tmp_path, capsys):
    """Metadata left behind by an interrupted forced fetch must not attach
    false provenance to the canonical layer."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    metadata_path = config.raw_dir / "ecfr" / ISSUE_DATE / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["raw_hash"] = "sha256:" + "0" * 64  # describes some other bytes
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    assert "does not describe the accepted snapshot" in capsys.readouterr().err


def test_validate_rejects_stale_nested_hash(tmp_path, capsys):
    """A tampered section-level canonical_hash must fail validation even
    though the part hash (which excludes nested hashes) still matches."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    part_file = config.normalized_dir / "ecfr" / "part-0091.json"
    doc = json.loads(part_file.read_text(encoding="utf-8"))

    def tamper_first_section(node):
        if isinstance(node, dict):
            if node.get("document_type") == "cfr_section":
                node["canonical_hash"] = "sha256:" + "0" * 64
                return True
            return any(
                tamper_first_section(item)
                for value in node.values()
                if isinstance(value, list)
                for item in value
            )
        return False

    assert tamper_first_section(doc)
    # Recompute the part hash so ONLY the nested hash is stale.
    from far_aim.models import cfr as cfr_model

    doc["canonical_hash"] = cfr_model.canonical_hash(doc)
    part_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "does not match its content" in capsys.readouterr().err


def test_partial_parse_recovers_interrupted_publish(tmp_path, capsys):
    """A --part parse must restore a displaced committed layer before
    writing, not strand it by creating a fresh partial directory."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    os.rename(out_dir, previous)  # crash window between the swap renames
    (previous / "marker.txt").write_text("committed layer", encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    assert "restored interrupted normalized layer" in capsys.readouterr().out
    # The full committed layer is back, with the partial update applied.
    assert (out_dir / "marker.txt").exists()
    assert not previous.exists()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_partial_publish_fsyncs_fallback_removal(tmp_path, monkeypatch):
    """Removing `.ecfr-previous` after a partial publish must be made
    durable: with no full-title parse yet (`canonical_hash` unset), a
    fallback resurrected by a power loss would be reinstated
    unconditionally by the next run's recovery, silently rolling back a
    publish that reported success."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    # Partial-parse-only history: canonical_hash is never recorded.
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK

    previous = config.normalized_dir / ".ecfr-previous"
    calls = []
    real_fsync = ecfr.fsync_dir

    def recording_fsync(path):
        calls.append((os.fspath(path), previous.exists()))
        return real_fsync(path)

    monkeypatch.setattr(ecfr, "fsync_dir", recording_fsync)
    # Second partial publish displaces the first layer to `.ecfr-previous`
    # and removes it after the swap.
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    assert not previous.exists()
    # The swap fsyncs the parent while the fallback still exists; the
    # removal must be followed by its own parent-directory fsync.
    assert (os.fspath(config.normalized_dir), False) in calls


def test_partial_parse_rejects_tampered_nested_provenance(tmp_path, capsys):
    """A carried-over part whose *nested* section provenance is stale must
    fail the partial-parse gate: nested source blocks are excluded from
    canonical hashes, so a root-only check would publish a layer that
    `validate` immediately rejects while reporting success."""
    from far_aim.models import cfr as cfr_model

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    part_file = config.normalized_dir / "ecfr" / "part-0091.json"
    doc = json.loads(part_file.read_text(encoding="utf-8"))

    def first_section(node):
        if isinstance(node, dict):
            if node.get("document_type") == cfr_model.DOCUMENT_TYPE_SECTION:
                return node
            for value in node.values():
                if isinstance(value, list):
                    for item in value:
                        found = first_section(item)
                        if found is not None:
                            return found
        return None

    first_section(doc)["source"]["source_version"] = "1999-01-01"
    part_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "not built from the accepted snapshot" in err
    assert "source_version" in err


def test_full_publish_fsyncs_fallback_removal(tmp_path, monkeypatch):
    """The full-title publish must also make the fallback's removal
    durable: a `.ecfr-previous` resurrected by a power loss would be
    reinstated unconditionally once a later fetch clears the recorded
    canonical_hash, displacing the newer committed layer."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK

    previous = config.normalized_dir / ".ecfr-previous"
    calls = []
    real_fsync = ecfr.fsync_dir

    def recording_fsync(path):
        calls.append((os.fspath(path), previous.exists()))
        return real_fsync(path)

    monkeypatch.setattr(ecfr, "fsync_dir", recording_fsync)
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    assert not previous.exists()
    # The swap fsyncs the parent while the fallback still exists; the
    # removal must be followed by its own parent-directory fsync.
    assert (os.fspath(config.normalized_dir), False) in calls


def test_parse_failure_still_recovers_displaced_layer(tmp_path, capsys, monkeypatch):
    """A parse failure after an interrupted publish must first restore the
    committed layer from `.ecfr-previous` — otherwise it returns with the
    last known-good output stranded off its canonical path while claiming
    it was preserved."""
    from far_aim.parsers import ecfr as ecfr_parser

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    os.rename(out_dir, previous)  # crash window between the swap renames
    (previous / "marker.txt").write_text("committed layer", encoding="utf-8")

    def failing_build(root, source, only=None):
        raise ecfr_parser.ParseError("injected parse failure")

    monkeypatch.setattr(ecfr_parser, "build_part_docs", failing_build)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    output = capsys.readouterr()
    assert "restored interrupted normalized layer" in output.out
    assert "last known-good output preserved" in output.err
    # The committed layer is back at its canonical path despite the failure.
    assert (out_dir / "marker.txt").exists()
    assert not previous.exists()


def test_recovery_deep_verifies_before_discarding_fallback(tmp_path, capsys):
    """A corrupted current layer whose stored hashes were left intact must
    not win the recovery arbitration and cause the intact fallback's
    deletion."""
    import shutil

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    # Crash window state: intact committed layer at `previous`, and a copy
    # at the canonical path corrupted WITHOUT touching any stored hash.
    os.rename(out_dir, previous)
    shutil.copytree(previous, out_dir)
    part91 = out_dir / "part-0091.json"
    doc = json.loads(part91.read_text(encoding="utf-8"))
    doc["heading"] = "TAMPERED"  # stored hashes now stale but unchanged
    part91.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out = capsys.readouterr().out
    # Deep verification disqualifies the corrupted layer; the intact one is
    # reinstated instead of deleted, and the publish then proceeds.
    assert "reinstated the previous normalized layer" in out
    assert not previous.exists()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_partial_parse_interrupt_leaves_layer_untouched(tmp_path, capsys, monkeypatch):
    """A KeyboardInterrupt mid-staging must not leave a partially updated
    canonical layer (in-memory rollback would not survive a kill)."""
    import far_aim.cli as cli_mod

    config = _accepted_snapshot(tmp_path, TWO_PART_XML)
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    before = {p.name: p.read_bytes() for p in sorted(out_dir.glob("part-*.json"))}

    real_write = cli_mod.write_json_atomic
    calls = {"n": 0}

    def interrupting_write(path, obj):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_write(path, obj)

    monkeypatch.setattr(cli_mod, "write_json_atomic", interrupting_write)
    with pytest.raises(KeyboardInterrupt):
        main(["--root", str(tmp_path), "parse", "ecfr", "--part", "9", "--part", "10"])
    # The canonical layer is untouched; only the staging copy was abandoned,
    # and the next run's recovery discards it.
    after = {p.name: p.read_bytes() for p in sorted(out_dir.glob("part-*.json"))}
    assert after == before
    monkeypatch.setattr(cli_mod, "write_json_atomic", real_write)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "9"]) == EXIT_OK
    assert not (config.normalized_dir / ".ecfr-staging").exists()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_recovery_reinstates_previous_layer_without_recorded_hash(tmp_path, capsys):
    """With no manifest hash to arbitrate (partial-only history), a crash
    after the swap must not cost the pre-command layer its fallback role."""
    import shutil

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    # Build a layer WITHOUT recording a hash: partial parse only.
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    # Crash-after-swap state: pre-command layer displaced to `previous`
    # (marked), a divergent uncommitted layer at the canonical path.
    os.rename(out_dir, previous)
    (previous / "marker.txt").write_text("pre-command layer", encoding="utf-8")
    shutil.copytree(previous, out_dir)
    (out_dir / "marker.txt").unlink()
    (out_dir / "part-0091.json").write_text('{"uncommitted": true}', encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "reinstated the previous normalized layer" in out
    # The pre-command layer (with its marker) survived as the base.
    assert (out_dir / "marker.txt").exists()
    assert not previous.exists()


def test_validate_rejects_missing_nested_hash(tmp_path, capsys):
    """Deleting a section's canonical_hash key must be a defect, not a
    skipped check (parent hashes exclude nested hashes)."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    part_file = config.normalized_dir / "ecfr" / "part-0091.json"
    doc = json.loads(part_file.read_text(encoding="utf-8"))

    def delete_first_section_hash(node):
        if isinstance(node, dict):
            if node.get("document_type") == "cfr_section":
                del node["canonical_hash"]
                return True
            return any(
                delete_first_section_hash(item)
                for value in node.values()
                if isinstance(value, list)
                for item in value
            )
        return False

    assert delete_first_section_hash(doc)
    part_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "has no stored canonical_hash" in capsys.readouterr().err


def test_validate_rejects_false_provenance(tmp_path, capsys):
    """A tampered or missing source block must fail validation even though
    canonical hashing strips provenance."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    part_file = config.normalized_dir / "ecfr" / "part-0091.json"
    original = part_file.read_text(encoding="utf-8")

    # Wrong source_version on a nested section.
    doc = json.loads(original)

    def tamper_first_section(node):
        if isinstance(node, dict):
            if node.get("document_type") == "cfr_section":
                node["source"]["source_version"] = "1999-01-01"
                return True
            return any(
                tamper_first_section(item)
                for value in node.values()
                if isinstance(value, list)
                for item in value
            )
        return False

    assert tamper_first_section(doc)
    part_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "source source_version '1999-01-01' does not match" in err

    # Missing source block on the part itself.
    doc = json.loads(original)
    del doc["source"]
    part_file.write_text(json.dumps(doc), encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "missing source block" in capsys.readouterr().err


def test_validate_fails_closed_while_lock_held(tmp_path, capsys):
    """validate reads manifest + layer under the source lock, so it can
    never observe a half-published state from a concurrent parse."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    capsys.readouterr()
    with ecfr.exclusive_lock(ecfr.fetch_lock_path(config)):
        assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "already in progress" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_validate_rejects_wrong_root_document_type(tmp_path, capsys):
    """A part file whose root document_type was stripped must fail: the
    type-keyed walker would otherwise skip the root's own hash check while
    its stored hash still feeds the title hash."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    part_file = config.normalized_dir / "ecfr" / "part-0091.json"
    doc = json.loads(part_file.read_text(encoding="utf-8"))
    doc["document_type"] = "bogus"  # stored hash left untouched
    part_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "root document_type 'bogus' is not 'cfr_part'" in capsys.readouterr().err


def test_validate_lock_creation_failure_is_controlled(tmp_path, capsys):
    """A read-only manifests directory must yield a controlled error, not a
    traceback, from the otherwise read-only validate command."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    manifests_dir = config.manifests_dir
    lock = ecfr.fetch_lock_path(config)
    assert not lock.exists()
    os.chmod(manifests_dir, 0o500)
    try:
        assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    finally:
        os.chmod(manifests_dir, 0o755)
    err = capsys.readouterr().err
    assert "filesystem failure during validate" in err
    assert "Traceback" not in err


def test_recovery_fails_closed_when_neither_layer_verifies(tmp_path, capsys):
    """If both the current layer and the displaced copy fail verification,
    recovery must keep both for inspection rather than guess."""
    import shutil

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    os.rename(out_dir, previous)
    shutil.copytree(previous, out_dir)
    # Corrupt BOTH copies (content changed, stored hashes untouched).
    for layer in (out_dir, previous):
        part = layer / "part-0091.json"
        doc = json.loads(part.read_text(encoding="utf-8"))
        doc["heading"] = f"TAMPERED {layer.name}"
        part.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_ERROR
    assert "refusing to discard either" in capsys.readouterr().err
    assert out_dir.exists()
    assert previous.exists()


def test_recovery_rejects_layer_with_misnamed_duplicate(tmp_path, capsys):
    """A layer whose extra misnamed copy would collapse in a dict must not
    win recovery arbitration when validate would reject it."""
    import shutil

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out_dir = config.normalized_dir / "ecfr"
    previous = config.normalized_dir / ".ecfr-previous"
    os.rename(out_dir, previous)
    (previous / "marker.txt").write_text("intact fallback", encoding="utf-8")
    shutil.copytree(previous, out_dir)
    (out_dir / "marker.txt").unlink()
    # Duplicate part 91 under another part's filename: every document still
    # deep-verifies and the dict of stored hashes would collapse to the
    # committed title hash — but validate would reject this layer.
    shutil.copyfile(out_dir / "part-0091.json", out_dir / "part-0090.json")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    out = capsys.readouterr().out
    # The malformed layer lost the arbitration (without the filename/
    # duplicate checks it would have hashed to the manifest value and the
    # fallback would have been deleted instead of reinstated); the
    # successful publish then replaces the reinstated fallback normally.
    assert "reinstated the previous normalized layer" in out
    assert not (out_dir / "part-0090.json").exists()
    assert not previous.exists()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_partial_parse_refuses_to_invalidate_committed_layer(tmp_path, capsys, monkeypatch):
    """A --part publish whose content diverges from the committed title hash
    (e.g. after parser changes) must fail closed, not leave a layer that
    validate rejects while reporting success."""
    import far_aim.cli as cli_mod
    from far_aim.models import cfr as cfr_model

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK

    real_build = cli_mod.ecfr_parser.build_part_docs

    def altered_parser(root, source, only=None):
        docs = real_build(root, source, only)
        for doc in docs.values():
            doc["heading"] = doc["heading"] + " CHANGED"
            doc["canonical_hash"] = cfr_model.canonical_hash(doc)
        return docs

    monkeypatch.setattr(cli_mod.ecfr_parser, "build_part_docs", altered_parser)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "would change the published layer's title hash" in err
    assert "Normalized layer left untouched" in err
    assert not (config.normalized_dir / ".ecfr-staging").exists()
    # The committed layer survived intact and still validates.
    monkeypatch.setattr(cli_mod.ecfr_parser, "build_part_docs", real_build)
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_partial_parse_refuses_stale_base_layer(tmp_path, capsys):
    """After a fetch accepts a newer snapshot, the old normalized layer must
    not serve as the base of a --part publish (mixed-provenance corpus)."""
    import hashlib

    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK

    # Simulate `fetch ecfr` accepting a newer snapshot: same bytes under a
    # new issue date, manifest re-pointed, canonical_hash cleared.
    new_version = "2026-09-01"
    xml_bytes = SLICE_XML.read_bytes()
    new_dir = config.raw_dir / "ecfr" / new_version
    new_dir.mkdir(parents=True)
    (new_dir / "title-14.xml").write_bytes(xml_bytes)
    metadata = json.loads(
        (config.raw_dir / "ecfr" / ISSUE_DATE / "metadata.json").read_text("utf-8")
    )
    metadata["source_version"] = new_version
    metadata["url"] = ecfr.full_title14_url(new_version)
    (new_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    manifest = SourceManifest.load(config.manifest_path)
    state = manifest.sources["ecfr_title_14"]
    state.accepted_version = new_version
    state.raw_hash = f"sha256:{hashlib.sha256(xml_bytes).hexdigest()}"
    state.canonical_hash = None
    manifest.save(config.manifest_path)

    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "was not built from the accepted snapshot" in err
    assert "Normalized layer left untouched" in err
    # A full parse resolves it; a partial parse afterwards is fine again.
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "parse", "ecfr", "--part", "91"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


# ---------------------------------------------------------------------------
# build-vault (Phase 3)
# ---------------------------------------------------------------------------


def _built_vault(tmp_path, capsys) -> Config:
    """A root with the slice parsed and the vault generated."""
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    return config


def _vault_bytes(config: Config) -> dict:
    return {
        path: path.read_bytes()
        for path in sorted(config.vault_dir.rglob("*.md"))
    }


def test_build_vault_generates_and_is_idempotent(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "19 written" in out
    assert (config.vault_dir / "FAR" / "Part 091" / "91.155.md").exists()
    assert (config.vault_dir / "FAR" / "Part 091" / "Part 91.md").exists()
    assert (config.vault_dir / "FAR" / "Title 14.md").exists()
    assert (config.vault_dir / "Source Status.md").exists()
    assert (config.vault_dir / "Home.md").exists()

    # Rebuild with unchanged sources: no writes, byte-identical tree (§32.10).
    first = _vault_bytes(config)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "0 written" in out and "0 stale deleted" in out
    assert _vault_bytes(config) == first


def test_build_vault_without_canonical_layer(tmp_path, capsys):
    _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    assert "run `far-aim fetch ecfr` and `far-aim parse ecfr` first" in capsys.readouterr().err


def test_build_vault_without_manifest(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    assert "source registry missing" in capsys.readouterr().err


def test_build_vault_fails_closed_while_lock_held(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    with ecfr.exclusive_lock(ecfr.fetch_lock_path(config)):
        assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    assert "already in progress" in capsys.readouterr().err


def test_build_vault_refuses_curated_note_at_generated_path(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    target = config.vault_dir / "FAR" / "Part 091" / "91.155.md"
    target.parent.mkdir(parents=True)
    target.write_text("# my own 91.155 notes\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    assert "curated note at generated path" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "# my own 91.155 notes\n"


def test_build_vault_preserves_curated_note_in_tree(tmp_path, capsys):
    config = _built_vault(tmp_path, capsys)
    curated = config.vault_dir / "FAR" / "Part 091" / "My Notes.md"
    curated.write_text("mine\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    captured = capsys.readouterr()
    assert curated.read_text(encoding="utf-8") == "mine\n"
    assert "My Notes.md" in captured.err  # warning, not deletion


def test_build_vault_deletes_stale_generated_note(tmp_path, capsys):
    config = _built_vault(tmp_path, capsys)
    stale = config.vault_dir / "FAR" / "Part 091" / "91.999.md"
    real = config.vault_dir / "FAR" / "Part 091" / "91.155.md"
    stale.write_bytes(real.read_bytes())
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    assert "1 stale deleted" in capsys.readouterr().out
    assert not stale.exists()


def test_validate_checks_vault(tmp_path, capsys):
    config = _built_vault(tmp_path, capsys)
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "ok: vault matches canonical layer (19 notes)" in capsys.readouterr().out

    # A hand-edited generated note fails validation.
    note = config.vault_dir / "FAR" / "Part 091" / "91.155.md"
    note.write_bytes(note.read_bytes() + b"\nedited\n")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "differs from the canonical layer" in capsys.readouterr().err


def test_validate_flags_missing_and_stale_vault_notes(tmp_path, capsys):
    config = _built_vault(tmp_path, capsys)
    note = config.vault_dir / "FAR" / "Part 091" / "91.155.md"

    stale = config.vault_dir / "FAR" / "Part 091" / "91.999.md"
    stale.write_bytes(note.read_bytes())
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "stale generated note" in capsys.readouterr().err
    stale.unlink()

    note.unlink()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "missing generated note" in capsys.readouterr().err


def test_validate_ignores_curated_notes_in_vault(tmp_path, capsys):
    config = _built_vault(tmp_path, capsys)
    (config.vault_dir / "FAR" / "Part 091" / "My Notes.md").write_text("mine\n", encoding="utf-8")
    (config.vault_dir / "Topics").mkdir()
    (config.vault_dir / "Topics" / "Airspace.md").write_text("curated\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "vault matches canonical layer" in capsys.readouterr().out


def test_validate_without_vault_still_ok(tmp_path, capsys):
    _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "no generated vault yet" in capsys.readouterr().out


_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
_NORMALIZED = _REPO_ROOT / "data" / "normalized" / "ecfr"
_MANIFEST = _REPO_ROOT / "data" / "manifests" / "sources.json"


@pytest.mark.skipif(
    not (_NORMALIZED.is_dir() and _MANIFEST.exists()),
    reason="requires the locally parsed full-title canonical layer",
)
def test_full_title_vault_build(tmp_path, capsys):
    """Build the entire Title 14 vault from the real canonical layer."""
    config = Config.load(tmp_path)
    config.manifests_dir.mkdir(parents=True)
    manifest = SourceManifest.load(_MANIFEST)
    if manifest.sources["ecfr_title_14"].canonical_hash is None:
        pytest.skip("manifest has no recorded canonical layer")
    manifest.save(config.manifest_path)
    (config.normalized_dir).mkdir(parents=True)
    os.symlink(_NORMALIZED, config.normalized_dir / "ecfr")
    config.links_dir.mkdir(parents=True)
    os.symlink(_MANIFEST.parent.parent / "links" / "pcg-glossary-gate.json", config.pcg_gate_path)
    aim_state = manifest.sources["aim"]
    expected_total = 6773  # + Home.md since Phase 7
    if aim_state.canonical_hash is not None:
        # The AIM layer and its archived figures ride along (Phase 4).
        aim_raw = _NORMALIZED.parent.parent / "raw" / "aim"
        if not (_NORMALIZED.parent / "aim").is_dir() or not aim_raw.is_dir():
            pytest.skip("manifest records an AIM layer that is not on disk")
        os.symlink(_NORMALIZED.parent / "aim", config.normalized_dir / "aim")
        config.raw_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(aim_raw, config.raw_dir / "aim")
        expected_total += _aim_file_count(_NORMALIZED.parent / "aim")
    if manifest.sources["pcg"].canonical_hash is not None:
        # The PCG layer rides along too (Phase 5).
        if not (_NORMALIZED.parent / "pcg").is_dir():
            pytest.skip("manifest records a PCG layer that is not on disk")
        os.symlink(_NORMALIZED.parent / "pcg", config.normalized_dir / "pcg")
        expected_total += _pcg_file_count(_NORMALIZED.parent / "pcg")
    enrichment_dir = _REPO_ROOT / "data" / "enrichment"
    if enrichment_dir.is_dir():
        # The committed enrichment layer rides along (Phase 9): concept
        # notes are planned files; derived links only decorate existing ones.
        # Curated notes are link targets for concepts, so they ride along too.
        os.symlink(enrichment_dir, config.enrichment_dir)
        for sub in ("Collections", "Topics", "Study"):
            if (_REPO_ROOT / "vault" / sub).is_dir():
                config.vault_dir.mkdir(parents=True, exist_ok=True)
                os.symlink(_REPO_ROOT / "vault" / sub, config.vault_dir / sub)
        if (enrichment_dir / "concepts.json").exists():
            concepts = json.loads((enrichment_dir / "concepts.json").read_text(encoding="utf-8"))
            expected_total += len(concepts["concepts"]) + 1  # + Concept Map

    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"{expected_total} notes" in out

    far = config.vault_dir / "FAR"
    part_folders = [d for d in far.iterdir() if d.is_dir()]
    assert len(part_folders) == 226
    # Every part has an index note (Phase 3 exit criterion).
    assert all(any(p.name.startswith("Part ") for p in d.glob("*.md")) for d in part_folders)
    assert len(list(far.rglob("*.md"))) + 2 == 6773  # + Source Status.md and Home.md at the root
    assert (far / "Title 14.md").exists()
    assert (config.vault_dir / "Source Status.md").exists()
    assert (config.vault_dir / "Home.md").exists()

    # Double build is a no-op.
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    assert "0 written" in capsys.readouterr().out

    # And validate agrees byte-for-byte.
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert f"vault matches canonical layer ({expected_total} notes)" in capsys.readouterr().out

    # Phase 7 regression: the committed curated notes must not case-fold-
    # collide with any generated stem or alias — a curated `Currency.md`
    # would shadow § 221.50's "Currency" alias and make Obsidian
    # navigation ambiguous (docs/vault.md naming guidance).
    generated_names: set[str] = set()
    for path in config.vault_dir.rglob("*.md"):
        if path.relative_to(config.vault_dir).parts[0] in ("Collections", "Topics", "Study"):
            continue  # the symlinked curated trees are what we compare against
        generated_names.add(path.stem.casefold())
        generated_names.update(a.casefold() for a in _frontmatter_aliases(path))
    for sub in ("Collections", "Topics", "Study"):
        curated_dir = _REPO_ROOT / "vault" / sub
        if not curated_dir.is_dir():
            continue
        for path in sorted(curated_dir.rglob("*.md")):
            assert path.stem.casefold() not in generated_names, (
                f"curated note {path.name} collides with a generated stem or alias"
            )


def _frontmatter_aliases(path) -> list[str]:
    """Alias values from a generated note's frontmatter block."""
    aliases: list[str] = []
    in_block = False
    with path.open(encoding="utf-8") as fh:
        next(fh, None)  # opening ---
        for raw in fh:
            line = raw.rstrip("\n")
            if line == "---":
                break
            if line == "aliases:":
                in_block = True
            elif in_block and line.startswith('  - "') and line.endswith('"'):
                aliases.append(line[5:-1])
            else:
                in_block = False
    return aliases


def _aim_file_count(layer_dir) -> int:
    """Notes + assets the AIM layer contributes: index, chapters, sections,
    paragraphs, appendices, and distinct figure files."""
    from far_aim.generate.build import aim_asset_hashes

    docs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in layer_dir.glob("*.json")}
    notes = 1  # AIM.md (renders the publication document)
    for doc in docs.values():
        if doc["document_type"] == "aim_publication":
            continue
        notes += 1
        for section in doc.get("sections", []):
            notes += 1 + len(section["paragraphs"])
    return notes + len(aim_asset_hashes(docs)) + 1  # + asset ledger


def _pcg_file_count(layer_dir) -> int:
    """Notes the PCG layer contributes: the index plus one per term."""
    notes = 1  # PCG.md (renders the publication document)
    for path in layer_dir.glob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        notes += len(doc.get("terms", []))
    return notes


def test_validate_detects_deleted_far_tree(tmp_path, capsys):
    # The generated Source Status.md proves a build happened; the missing
    # FAR tree must fail the missing-note checks, not read as "no vault".
    import shutil

    config = _built_vault(tmp_path, capsys)
    shutil.rmtree(config.vault_dir / "FAR")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "missing generated note" in capsys.readouterr().err


def test_validate_ignores_curated_only_far_tree_before_first_build(tmp_path, capsys):
    # A user may organize curated notes under vault/FAR/ before ever running
    # build-vault; that is not a generated vault and must validate cleanly.
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    curated = config.vault_dir / "FAR" / "Part 091" / "My Notes.md"
    curated.parent.mkdir(parents=True)
    curated.write_text("mine\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "no generated vault yet" in capsys.readouterr().out


def test_validate_home_alone_counts_as_built_vault(tmp_path, capsys):
    # A generated Home.md left behind after everything else was deleted is a
    # damaged build that must fail the missing-note checks, not be mistaken
    # for a never-built vault (Phase 7 regression).
    import shutil

    config = _built_vault(tmp_path, capsys)
    shutil.rmtree(config.vault_dir / "FAR")
    (config.vault_dir / "Source Status.md").unlink()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "missing generated note" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# diff (Phase 8)
# ---------------------------------------------------------------------------


def test_diff_reports_structural_changes(tmp_path, capsys):
    config = _built_vault(tmp_path, capsys)
    assert main(["--root", str(tmp_path), "diff"]) == EXIT_OK
    assert "diff: 0 to add, 0 to rewrite, 0 to remove" in capsys.readouterr().out

    # A missing note is an add, a hand-damaged one a rewrite, and a parked
    # generated note the plan no longer produces a remove — differences
    # are a report, never an error.
    victim = config.vault_dir / "FAR" / "Part 091" / "91.155.md"
    stale = config.vault_dir / "FAR" / "Part 091" / "91.999.md"
    stale.write_bytes(victim.read_bytes())
    victim.unlink()
    edited = config.vault_dir / "FAR" / "Part 091" / "91.175.md"
    edited.write_bytes(edited.read_bytes() + b"\nedited\n")
    assert main(["--root", str(tmp_path), "diff"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "add: FAR/Part 091/91.155.md" in out
    assert "rewrite: FAR/Part 091/91.175.md" in out
    assert "remove: FAR/Part 091/91.999.md" in out
    assert "diff: 1 to add, 1 to rewrite, 1 to remove" in out

    # Curated notes are invisible to the diff.
    curated = config.vault_dir / "FAR" / "Part 091" / "My Notes.md"
    curated.write_text("mine\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "diff"]) == EXIT_OK
    assert "diff: 0 to add, 0 to rewrite, 0 to remove" in capsys.readouterr().out


def test_diff_requires_accepted_layer(tmp_path, capsys):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "diff"]) == EXIT_ERROR
    assert "no accepted eCFR canonical layer" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# update (Phase 8)
# ---------------------------------------------------------------------------


def test_update_from_empty_runs_full_pipeline_then_noops(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)

    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    out = capsys.readouterr().out
    for step in (
        "fetch ecfr",
        "fetch aim",
        "fetch pcg",
        "parse ecfr",
        "diff",
        "build-vault",
        "validate",
    ):
        assert f"==> far-aim {step}" in out
    # The structural diff ran against the pre-build (empty) vault.
    assert "to add" in out and "0 to remove" in out
    assert (config.vault_dir / "Home.md").exists()
    assert (config.vault_dir / "AIM" / "AIM.md").exists()
    assert (config.vault_dir / "PCG" / "PCG.md").exists()
    assert f"ecfr_title_14: none → {ISSUE_DATE} (canonical content changed)" in out
    assert f"aim: none → {AIM_VERSION} (canonical content changed)" in out
    assert f"pcg: none → {PCG_VERSION} (canonical content changed)" in out

    # No-change run: exits cleanly and touches nothing — not even the
    # manifest's last_checked_at (Phase 8 exit criterion).
    manifest_bytes = config.manifest_path.read_bytes()
    vault_before = _vault_bytes(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "all sources up to date; nothing to do" in out
    assert "==> far-aim" not in out
    assert config.manifest_path.read_bytes() == manifest_bytes
    assert _vault_bytes(config) == vault_before


def test_update_version_bump_without_content_change(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    # Simulated change: a newer eCFR issue date serving identical XML.
    mock_upstream.issue_date = "2026-09-15"
    mock_upstream.titles = titles_payload("2026-09-15")
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"ecfr_title_14: {ISSUE_DATE} → 2026-09-15 (canonical content unchanged)" in out
    assert f"aim: unchanged ({AIM_VERSION})" in out
    assert f"pcg: unchanged ({PCG_VERSION})" in out
    # Per-note provenance follows the accepted issue (the expected diff).
    note = (config.vault_dir / "FAR" / "Part 001" / "1.1.md").read_text(encoding="utf-8")
    assert 'source_version: "2026-09-15"' in note
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.accepted_version == "2026-09-15"


def test_update_fetch_failure_blocks_publication(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()
    vault_before = _vault_bytes(config)
    manifest_bytes = config.manifest_path.read_bytes()

    # A newer issue whose download fails validation must stop the pipeline
    # and preserve the last known-good output (plan §32.13).
    mock_upstream.issue_date = "2026-09-15"
    mock_upstream.titles = titles_payload("2026-09-15")
    mock_upstream.xml = b"not xml"
    assert main(["--root", str(tmp_path), "update"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "update stopped at `far-aim fetch ecfr`" in err
    assert _vault_bytes(config) == vault_before
    assert SourceManifest.load(config.manifest_path).sources["ecfr_title_14"].raw_hash == (
        SourceManifest.from_dict(json.loads(manifest_bytes)).sources["ecfr_title_14"].raw_hash
    )


def test_update_resumes_after_interrupted_parse(tmp_path, capsys, mock_upstream, monkeypatch):
    # P1 regression: fetch accepts a new version before parse runs, so a
    # parse failure must not let the next run see "versions all current"
    # and no-op forever with a pending canonical layer.
    import far_aim.cli as cli_module

    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    mock_upstream.issue_date = "2026-09-15"
    mock_upstream.titles = titles_payload("2026-09-15")
    with monkeypatch.context() as patch:
        patch.setattr(cli_module, "cmd_parse_ecfr", lambda _config, _parts: EXIT_ERROR)
        assert main(["--root", str(tmp_path), "update"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "update stopped at `far-aim parse ecfr`" in err
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    assert state.accepted_version == "2026-09-15" and state.canonical_hash is None

    # Upstream still matches the accepted versions, but the next run must
    # resume the pipeline, not report "nothing to do".
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "canonical layer is pending for: ecfr_title_14" in out
    assert "resuming the interrupted update" in out
    assert "nothing to do" not in out
    # The summary reports the publication this run completed — not
    # "unchanged", even though the manifest showed 2026-09-15 from the start.
    assert "ecfr_title_14: resumed publication of 2026-09-15" in out
    assert f"aim: unchanged ({AIM_VERSION})" in out
    note = (config.vault_dir / "FAR" / "Part 001" / "1.1.md").read_text(encoding="utf-8")
    assert 'source_version: "2026-09-15"' in note

    # And once resumed, the next run really is a no-op.
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    assert "all sources up to date; nothing to do" in capsys.readouterr().out


def test_update_resumes_after_interrupted_build(tmp_path, capsys, mock_upstream, monkeypatch):
    # Same interruption one step later: every layer parsed (all canonical
    # hashes recorded) but the vault still shows the previous editions.
    import far_aim.cli as cli_module

    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    mock_upstream.issue_date = "2026-09-15"
    mock_upstream.titles = titles_payload("2026-09-15")
    with monkeypatch.context() as patch:
        patch.setattr(cli_module, "cmd_build_vault", lambda _config: EXIT_ERROR)
        assert main(["--root", str(tmp_path), "update"]) == EXIT_ERROR
    assert "update stopped at `far-aim build-vault`" in capsys.readouterr().err

    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Source Status note predates the accepted sources" in out
    assert "resuming the interrupted update" in out
    status = (config.vault_dir / "Source Status.md").read_text(encoding="utf-8")
    assert "2026-09-15" in status

    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    assert "all sources up to date; nothing to do" in capsys.readouterr().out


def test_update_resumes_when_vault_fails_validation(tmp_path, capsys, mock_upstream):
    # With versions current and Source Status intact, damage elsewhere in
    # the vault (a sync killed mid-write, a deleted generated note) must
    # still be caught before the no-op path — the full read-only
    # validation runs whenever the local canonical layers exist.
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    victim = config.vault_dir / "FAR" / "Part 001" / "1.1.md"
    victim.unlink()
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "missing generated note" in captured.err
    assert "the published output failed validation" in captured.out
    assert "resuming the interrupted update" in captured.out
    assert victim.exists()

    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    assert "all sources up to date; nothing to do" in capsys.readouterr().out


def test_update_noop_without_local_layers(tmp_path, capsys, mock_upstream):
    # CI's daily case: a fresh checkout of a validated commit has no local
    # raw or normalized layers (gitignored, reconstructible). That must
    # stay a clean no-op — no resume, no refetch, no manifest churn.
    import shutil

    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    shutil.rmtree(config.normalized_dir)
    shutil.rmtree(config.raw_dir)
    manifest_bytes = config.manifest_path.read_bytes()
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    assert "all sources up to date; nothing to do" in capsys.readouterr().out
    assert config.manifest_path.read_bytes() == manifest_bytes
    assert not config.normalized_dir.exists() and not config.raw_dir.exists()


def test_update_reverify_detects_same_version_content_change(tmp_path, capsys, mock_upstream):
    # P1 regression: the FAA can edit content without bumping the edition.
    # On a fresh runner (no local archive) --reverify re-downloads the
    # accepted editions and must fail on a raw-hash mismatch instead of
    # no-opping on version equality alone.
    import shutil

    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    shutil.rmtree(config.raw_dir)
    mock_upstream.xml = mock_upstream.xml + b"<!-- silently edited upstream -->"
    manifest_bytes = config.manifest_path.read_bytes()
    assert main(["--root", str(tmp_path), "update", "--reverify"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "returned different content" in err
    assert "re-verification failed" in err
    # A failed re-verification accepted nothing, so it must not dirty the
    # manifest either (last_checked_at drift is restored on failure too).
    assert config.manifest_path.read_bytes() == manifest_bytes
    # Without --reverify the same state no-ops (versions match) — the flag
    # is what buys the content check.
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    assert "all sources up to date; nothing to do" in capsys.readouterr().out


def test_update_reverify_clean_match_stays_byte_clean(tmp_path, capsys, mock_upstream):
    # CI's daily case with --reverify: content still matches the accepted
    # editions, the archive is rebuilt locally, and the committed state —
    # manifest included (last_checked_at restored) — stays byte-identical.
    import shutil

    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    shutil.rmtree(config.raw_dir)
    shutil.rmtree(config.normalized_dir)
    manifest_bytes = config.manifest_path.read_bytes()
    vault_before = _vault_bytes(config)
    assert main(["--root", str(tmp_path), "update", "--reverify"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "accepted editions re-verified against upstream content" in out
    assert "all sources up to date; nothing to do" in out
    assert config.manifest_path.read_bytes() == manifest_bytes
    assert _vault_bytes(config) == vault_before
    assert (config.raw_dir / "ecfr" / ISSUE_DATE / "title-14.xml").exists()


def test_update_reverify_publishes_edition_accepted_mid_run(
    tmp_path, capsys, mock_upstream, monkeypatch
):
    # The fetchers run their own discovery: an edition that moves upstream
    # between update's discovery and the fetcher's is accepted during
    # --reverify (canonical hash cleared). The run must then resume into
    # publication from the reloaded manifest — not exit "nothing to do"
    # with the vault behind the freshly accepted state.
    import far_aim.cli as cli_module

    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    real_fetch = cli_module.cmd_fetch_ecfr
    bumped = False

    def bumping_fetch(config_arg, *, force):
        nonlocal bumped
        if not bumped:
            bumped = True
            mock_upstream.issue_date = "2026-09-15"
            mock_upstream.titles = titles_payload("2026-09-15")
        return real_fetch(config_arg, force=force)

    with monkeypatch.context() as patch:
        patch.setattr(cli_module, "cmd_fetch_ecfr", bumping_fetch)
        assert main(["--root", str(tmp_path), "update", "--reverify"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "canonical layer is pending for: ecfr_title_14" in out
    assert "resuming the interrupted update" in out
    assert "nothing to do" not in out
    assert "ecfr_title_14: resumed publication of 2026-09-15" in out
    note = (config.vault_dir / "FAR" / "Part 001" / "1.1.md").read_text(encoding="utf-8")
    assert 'source_version: "2026-09-15"' in note

    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    assert "all sources up to date; nothing to do" in capsys.readouterr().out


def test_update_resumes_after_unpublished_same_version_correction(tmp_path, capsys, mock_upstream):
    # A same-version correction (fetch --force + re-parse) moves only the
    # manifest's canonical hash; if the run dies before build-vault and the
    # local layers are gone, Source Status still matches and the pending
    # probe sees a non-null hash — the corpus index note's pinned
    # canonical_hash is what must catch the stale vault.
    import shutil

    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    capsys.readouterr()

    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["ecfr_title_14"].canonical_hash = "sha256:" + "ab" * 32
    manifest.save(config.manifest_path)
    shutil.rmtree(config.raw_dir)
    shutil.rmtree(config.normalized_dir)

    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Title 14.md was built from a different canonical layer" in out
    assert "resuming the interrupted update" in out
    assert "nothing to do" not in out
    # The pipeline reconciled manifest and vault (re-parse restored the
    # true canonical hash for the accepted raw).
    state = SourceManifest.load(config.manifest_path).sources["ecfr_title_14"]
    title_index = (config.vault_dir / "FAR" / "Title 14.md").read_text(encoding="utf-8")
    assert f'canonical_hash: "{state.canonical_hash}"' in title_index

    assert main(["--root", str(tmp_path), "update"]) == EXIT_OK
    assert "all sources up to date; nothing to do" in capsys.readouterr().out


def test_update_rollback_guard_blocks_before_fetch(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version="2027-01-01")
    manifest.save(config.manifest_path)
    assert main(["--root", str(tmp_path), "update"]) == EXIT_ERROR
    assert "older than accepted 2027-01-01" in capsys.readouterr().err
    assert mock_upstream.xml_requests == 0


def test_update_upstream_failure_leaves_state_untouched(tmp_path, capsys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(
        ecfr, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(ecfr.time, "sleep", lambda _s: None)
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    manifest_bytes = config.manifest_path.read_bytes()
    assert main(["--root", str(tmp_path), "update"]) == EXIT_ERROR
    assert "error" in capsys.readouterr().err
    assert config.manifest_path.read_bytes() == manifest_bytes


# ---------------------------------------------------------------------------
# AIM (Phase 4)
# ---------------------------------------------------------------------------


def test_fetch_aim_end_to_end(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)

    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "accepted: AIM Basic with Change 1, 2 and 3 (effective 2026-07-09)" in out
    assert "7 pages, 7 paragraphs, 5 figures" in out
    assert "archive this snapshot outside the repository" in out
    snapshot = config.raw_dir / "aim" / AIM_VERSION
    assert (snapshot / "pages" / "chap4_section_1.html").exists()
    assert (snapshot / "figures" / "aim0401_fig79_recovered.svg").exists()
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.accepted_version == AIM_VERSION and state.change == 3

    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    assert "unchanged" in capsys.readouterr().out
    requests = mock_upstream.aim.page_requests()
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_OK
    assert "aim: up to date (Basic with Change 1, 2 and 3 (effective 2026-07-09))" in (
        capsys.readouterr().out
    )
    assert mock_upstream.aim.page_requests() == requests


def test_check_remote_reports_aim_update_available(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version=ISSUE_DATE)
    manifest.sources["aim"] = SourceState(
        accepted_version="2026-01-22-change-2", effective_date="2026-01-22", change=2
    )
    manifest.save(config.manifest_path)
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "aim: update available" in out
    assert "accepted 2026-01-22-change-2" in out


def test_check_remote_reports_relabelled_aim_edition(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version=ISSUE_DATE)
    manifest.sources["aim"] = SourceState(
        accepted_version=AIM_VERSION,
        effective_date="2026-07-09",
        change=3,
        edition_label="Basic with Change 1, 2 and 3",
        source_url="https://www.faa.gov/air_traffic/publications/atpubs/aim_html/index.html",
    )
    manifest.save(config.manifest_path)
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_OK
    assert "aim: up to date" in capsys.readouterr().out
    mock_upstream.aim.publications = mock_upstream.aim.publications.replace(
        "(<abbr>AIM</abbr>) Basic with Change 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
        "(<abbr>AIM</abbr>) Basic with Changes 1, 2 and 3</a> <small>(<abbr>HTML</abbr>)",
    )
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "FAA now lists the accepted edition" in captured.err
    assert "aim: up to date" not in captured.out


def test_check_remote_reports_aim_rollback_as_error(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version=ISSUE_DATE)
    manifest.sources["aim"] = SourceState(
        accepted_version="2026-09-01-change-4", effective_date="2026-09-01", change=4
    )
    manifest.save(config.manifest_path)
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "older than accepted effective 2026-09-01 change 4" in captured.err
    assert f"up to date (issue {ISSUE_DATE})" in captured.out


def test_check_remote_one_source_unreachable_still_reports_other(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version=ISSUE_DATE)
    manifest.save(config.manifest_path)
    mock_upstream.aim.status_overrides["/air_traffic/publications/"] = 404
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "error: aim:" in captured.err
    assert f"up to date (issue {ISSUE_DATE})" in captured.out


def test_parse_aim_requires_fetch(tmp_path, capsys):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_ERROR
    assert "run `far-aim fetch aim` first" in capsys.readouterr().err


def test_parse_aim_rejects_part_option(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "parse", "aim", "--part", "91"]) == EXIT_ERROR
    assert "--part applies to `parse ecfr` only" in capsys.readouterr().err


def test_parse_aim_publishes_layer_and_validates(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    capsys.readouterr()

    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_OK
    out = capsys.readouterr().out
    assert (
        "parsed AIM 2026-07-09-change-3: 2 chapters, 2 sections, 7 paragraphs, 2 appendices"
    ) in out
    assert "manifest updated: canonical_hash" in out
    layer = config.normalized_dir / "aim"
    assert sorted(p.name for p in layer.glob("*.json")) == [
        "appendix-1.json",
        "appendix-3.json",
        "chapter-00.json",
        "chapter-04.json",
        "publication.json",
    ]
    state = SourceManifest.load(config.manifest_path).sources["aim"]
    assert state.canonical_hash is not None
    doc = json.loads((layer / "chapter-04.json").read_text(encoding="utf-8"))
    assert doc["source"]["raw_checksum"] == state.raw_hash
    assert doc["source"]["retrieved_at"] == json.loads(
        (config.raw_dir / "aim" / AIM_VERSION / "metadata.json").read_text(encoding="utf-8")
    )["retrieved_at"]

    # Re-parsing is a no-op for the manifest and byte-identical on disk.
    before = {p.name: p.read_bytes() for p in layer.glob("*.json")}
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_OK
    assert "canonical_hash unchanged" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in layer.glob("*.json")} == before

    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "ok: normalized AIM layer verified (5 documents" in out


def test_validate_rejects_tampered_aim_layer(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_OK
    capsys.readouterr()
    path = config.normalized_dir / "aim" / "chapter-04.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["sections"][0]["paragraphs"][0]["content"][0]["text"] += " Tampered."
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    # Parent hashes cover nested content, so the chapter document fails first.
    assert "stored canonical_hash of 'aim-chapter-4' does not match" in capsys.readouterr().err


def test_validate_rejects_altered_aim_provenance(tmp_path, capsys, mock_upstream):
    """Provenance is outside the canonical hash, so every rendered field is pinned."""
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_OK
    capsys.readouterr()
    path = config.normalized_dir / "aim" / "appendix-3.json"
    original = path.read_text(encoding="utf-8")
    base = "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/"
    cases = (
        ("edition_label", "Basic with Change 9", "does not match"),
        ("url", "https://evil.invalid/appendix_3.html", "outside the accepted edition"),
        ("url", base + "x.html", "edition page"),
        # A valid page of the edition that is not this document's own page.
        ("url", base + "chap_4.html", "does not match the document's identity"),
        ("url", base + "appendix_3.html#4-1-9", "does not match the document's identity"),
    )
    for key, value, message in cases:
        doc = json.loads(original)
        doc["source"][key] = value
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
        err = capsys.readouterr().err
        assert f"source {key}" in err and message in err
        assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
        capsys.readouterr()
    path.write_text(original, encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_validate_rejects_paragraph_url_pointing_at_other_paragraph(
    tmp_path, capsys, mock_upstream
):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_OK
    capsys.readouterr()
    path = config.normalized_dir / "aim" / "chapter-04.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    para = doc["sections"][0]["paragraphs"][1]
    assert para["paragraph"] == "4-1-2"
    para["source"]["url"] = para["source"]["url"].replace("#4-1-2", "#4-1-1")
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "'aim-4-1-2' expects page ('section', 4, 1) anchor '4-1-2'" in err
    # Padded-but-equivalent filenames are the same identity and pass.
    para["source"]["url"] = para["source"]["url"].replace(
        "chap4_section_1.html#4-1-1", "chap04_section_01.html#4-1-2"
    )
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_parse_aim_refuses_snapshot_metadata_disagreeing_with_manifest(
    tmp_path, capsys, mock_upstream
):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    manifest = SourceManifest.load(config.manifest_path)
    manifest.sources["aim"].edition_label = "Basic with Change 9"
    manifest.save(config.manifest_path)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_ERROR
    assert "snapshot metadata edition_label" in capsys.readouterr().err


def test_parse_aim_fails_closed_on_grammar_change(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_OK
    capsys.readouterr()
    layer = config.normalized_dir / "aim"
    before = {p.name: p.read_bytes() for p in layer.glob("*.json")}
    # Upstream re-published the same edition with a new element the parser
    # does not know: the archived bytes change, so re-fetch with --force.
    page = mock_upstream.aim.pages["chap4_section_1.html"]
    mock_upstream.aim.pages["chap4_section_1.html"] = page.replace(
        b'<p class="p">Centers are established',
        b'<details>x</details><p class="p">Centers are established',
    )
    assert main(["--root", str(tmp_path), "fetch", "aim", "--force"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "unexpected" in err and "<details>" in err
    assert "last known-good output preserved" in err
    assert {p.name: p.read_bytes() for p in layer.glob("*.json")} == before


def _build_fixture_vault(tmp_path, capsys, mock_upstream) -> Config:
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    for argv in (["fetch", "ecfr"], ["parse", "ecfr"], ["fetch", "aim"], ["parse", "aim"]):
        assert main(["--root", str(tmp_path), *argv]) == EXIT_OK
    capsys.readouterr()
    return config


def test_build_vault_with_aim_layer(tmp_path, capsys, mock_upstream):
    config = _build_fixture_vault(tmp_path, capsys, mock_upstream)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "ok: vault generated" in out
    aim_dir = config.vault_dir / "AIM"
    assert (aim_dir / "AIM.md").exists()
    assert (aim_dir / "Chapter 04" / "AIM Chapter 4.md").exists()
    assert (aim_dir / "Chapter 04" / "AIM 4-1.md").exists()
    assert (aim_dir / "Chapter 04" / "4-1-9.md").exists()
    assert (aim_dir / "Chapter 00" / "AIM 0-0.md").exists()
    assert (aim_dir / "Appendices" / "AIM Appendix 3.md").exists()
    assets = sorted(p.name for p in (aim_dir / "assets").iterdir())
    assert assets == [
        ".generated.json",
        "aim0401_fig79_recovered.svg",
        "aim0401_fig80_recovered.svg",
        "aimapd1_BlankFooter0.png",
        "aimapd1_floating0.png",
        "aimapd1_floating1.png",
    ]
    assert (aim_dir / "assets" / "aim0401_fig79_recovered.svg").read_bytes() == (
        config.raw_dir / "aim" / AIM_VERSION / "figures" / "aim0401_fig79_recovered.svg"
    ).read_bytes()
    status = (config.vault_dir / "Source Status.md").read_text(encoding="utf-8")
    assert "| AIM | Change 3 — effective 2026-07-09 |" in status

    # Idempotent, and validate agrees byte-for-byte.
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    assert "0 written" in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "ok: vault matches canonical layer" in capsys.readouterr().out


def test_validate_detects_stale_or_missing_aim_asset(tmp_path, capsys, mock_upstream):
    config = _build_fixture_vault(tmp_path, capsys, mock_upstream)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    assets = config.vault_dir / "AIM" / "assets"
    # A curated file in the assets directory is not the generator's business.
    curated = assets / "my_figure.png"
    curated.write_bytes(b"\x89PNG")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    capsys.readouterr()
    # A file the ledger attributes to an earlier build but the layer no
    # longer produces is stale.
    stray = assets / "old_figure.png"
    stray.write_bytes(b"\x89PNG old")
    ledger_path = assets / ".generated.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["generated"]["old_figure.png"] = aim_source.sha256_of_bytes(b"\x89PNG old")
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    # The ledger is itself a generated file, so validate trips on it first.
    assert ".generated.json differs from the canonical layer" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    assert "1 stale deleted" in capsys.readouterr().out
    assert not stray.exists() and curated.exists()
    (assets / "aim0401_fig79_recovered.svg").unlink()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "missing generated note" in capsys.readouterr().err
    # Rebuild restores it (from the archived snapshot) and removes nothing else.
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    assert "1 written" in capsys.readouterr().out


def test_build_vault_uses_vault_assets_when_snapshot_missing(tmp_path, capsys, mock_upstream):
    import shutil

    config = _build_fixture_vault(tmp_path, capsys, mock_upstream)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    shutil.rmtree(config.raw_dir / "aim")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    assert "0 written" in capsys.readouterr().out
    # With neither the archive nor the vault copy, the figure cannot be produced.
    (config.vault_dir / "AIM" / "assets" / "aim0401_fig79_recovered.svg").unlink()
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    assert "aim0401_fig79_recovered.svg" in capsys.readouterr().err


def test_build_refuses_to_drop_aim_notes_while_new_edition_unparsed(
    tmp_path, capsys, mock_upstream
):
    """fetch aim clears canonical_hash; a build in that window must not delete AIM output."""
    config = _build_fixture_vault(tmp_path, capsys, mock_upstream)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    aim_note = config.vault_dir / "AIM" / "Chapter 04" / "4-1-9.md"
    before = aim_note.read_bytes()
    # A new upstream edition arrives.
    mock_upstream.aim.publications = mock_upstream.aim.publications.replace(
        "Change 1, 2 and 3", "Change 1, 2, 3 and 4"
    ).replace("7/9/2026", "1/1/2027")
    mock_upstream.aim.pages["index.html"] = (
        mock_upstream.aim.pages["index.html"]
        .replace(b"<strong>Change:</strong> Change 3", b"<strong>Change:</strong> Change 4")
        .replace(b"<strong>Effective:</strong> 7/9/2026", b"<strong>Effective:</strong> 1/1/2027")
    )
    assert main(["--root", str(tmp_path), "fetch", "aim"]) == EXIT_OK
    assert SourceManifest.load(config.manifest_path).sources["aim"].canonical_hash is None
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "has not been parsed yet" in err and "parse aim" in err
    assert aim_note.read_bytes() == before
    assert (config.vault_dir / "AIM" / "assets" / "aim0401_fig79_recovered.svg").exists()
    # validate fails closed one gate earlier (unparsed normalized files).
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "re-run `far-aim parse aim`" in capsys.readouterr().err
    # Once the new edition is parsed, the build proceeds and re-renders the AIM.
    assert main(["--root", str(tmp_path), "parse", "aim"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    assert b"effective 2027-01-01" in aim_note.read_bytes()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


# ---------------------------------------------------------------------------
# PCG (Phase 5)
# ---------------------------------------------------------------------------


def test_fetch_pcg_end_to_end(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)

    assert main(["--root", str(tmp_path), "fetch", "pcg"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "accepted: PCG Basic with Change 1, 2 and 3 (effective 2026-07-09)" in out
    assert "6 pages, 117 term entries" in out
    assert "archive this snapshot outside the repository" in out
    snapshot = config.raw_dir / "pcg" / PCG_VERSION
    assert (snapshot / "pages" / "glossary-o.html").exists()
    state = SourceManifest.load(config.manifest_path).sources["pcg"]
    assert state.accepted_version == PCG_VERSION and state.change == 3

    assert main(["--root", str(tmp_path), "fetch", "pcg"]) == EXIT_OK
    assert "unchanged" in capsys.readouterr().out
    requests = mock_upstream.pcg.page_requests()
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_OK
    assert "pcg: up to date (Basic with Change 1, 2 and 3 (effective 2026-07-09))" in (
        capsys.readouterr().out
    )
    assert mock_upstream.pcg.page_requests() == requests


def test_check_remote_reports_pcg_update_available(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version=ISSUE_DATE)
    manifest.sources["pcg"] = SourceState(
        accepted_version="2026-01-22-change-2", effective_date="2026-01-22", change=2
    )
    manifest.save(config.manifest_path)
    assert main(["--root", str(tmp_path), "check", "--remote"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "pcg: update available" in out
    assert "accepted 2026-01-22-change-2" in out


def test_parse_pcg_requires_fetch(tmp_path, capsys):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "parse", "pcg"]) == EXIT_ERROR
    assert "run `far-aim fetch pcg` first" in capsys.readouterr().err


def test_parse_pcg_rejects_part_option(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "parse", "pcg", "--part", "91"]) == EXIT_ERROR
    assert "--part applies to `parse ecfr` only" in capsys.readouterr().err


def test_parse_pcg_publishes_layer_and_validates(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "pcg"]) == EXIT_OK
    capsys.readouterr()

    assert main(["--root", str(tmp_path), "parse", "pcg"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "parsed PCG 2026-07-09-change-3: 5 letters, 117 terms" in out
    assert "manifest updated: canonical_hash" in out
    layer = config.normalized_dir / "pcg"
    assert sorted(p.name for p in layer.glob("*.json")) == [
        "letter-b.json",
        "letter-k.json",
        "letter-n.json",
        "letter-o.json",
        "letter-t.json",
        "publication.json",
    ]
    state = SourceManifest.load(config.manifest_path).sources["pcg"]
    assert state.canonical_hash is not None
    doc = json.loads((layer / "letter-k.json").read_text(encoding="utf-8"))
    assert doc["source"]["raw_checksum"] == state.raw_hash

    # Re-parsing is a no-op for the manifest and byte-identical on disk.
    before = {p.name: p.read_bytes() for p in layer.glob("*.json")}
    assert main(["--root", str(tmp_path), "parse", "pcg"]) == EXIT_OK
    assert "canonical_hash unchanged" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in layer.glob("*.json")} == before

    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "ok: normalized PCG layer verified (6 documents" in capsys.readouterr().out


def test_validate_rejects_tampered_pcg_layer(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "pcg"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "parse", "pcg"]) == EXIT_OK
    capsys.readouterr()
    path = config.normalized_dir / "pcg" / "letter-k.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["terms"][0]["content"][0]["text"] += " Tampered."
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
    assert "stored canonical_hash of 'pcg-letter-k' does not match" in capsys.readouterr().err


def test_validate_rejects_altered_pcg_provenance(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    assert main(["--root", str(tmp_path), "fetch", "pcg"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "parse", "pcg"]) == EXIT_OK
    capsys.readouterr()
    path = config.normalized_dir / "pcg" / "letter-k.json"
    original = path.read_text(encoding="utf-8")
    base = "https://www.faa.gov/air_traffic/publications/atpubs/pcg_html/"
    cases = (
        ("edition_label", "Basic with Change 9", "does not match"),
        ("url", "https://evil.invalid/glossary-k.html", "outside the accepted edition"),
        ("url", base + "x.html", "edition page"),
        ("url", base + "glossary-b.html", "does not match the document's identity"),
        ("url", base + "glossary-k.html#X", "does not match the document's identity"),
    )
    for key, value, message in cases:
        doc = json.loads(original)
        doc["source"][key] = value
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert main(["--root", str(tmp_path), "validate"]) == EXIT_ERROR
        err = capsys.readouterr().err
        assert f"source {key}" in err and message in err
    path.write_text(original, encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def _write_empty_gate(config: Config) -> None:
    config.links_dir.mkdir(parents=True, exist_ok=True)
    config.pcg_gate_path.write_text(
        '{"deny": [], "allow_words": [], "allow_acronyms": []}\n', encoding="utf-8"
    )


def _build_full_fixture_vault(tmp_path, capsys, mock_upstream) -> Config:
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    _write_empty_gate(config)
    for argv in (
        ["fetch", "ecfr"], ["parse", "ecfr"],
        ["fetch", "aim"], ["parse", "aim"],
        ["fetch", "pcg"], ["parse", "pcg"],
    ):
        assert main(["--root", str(tmp_path), *argv]) == EXIT_OK
    capsys.readouterr()
    return config


def test_build_vault_requires_the_glossary_gate(tmp_path, capsys, mock_upstream):
    config = _build_full_fixture_vault(tmp_path, capsys, mock_upstream)
    config.pcg_gate_path.unlink()
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "glossary gate" in err and "missing" in err
    config.pcg_gate_path.write_text("{not json", encoding="utf-8")
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    assert "cannot read glossary gate" in capsys.readouterr().err
    _write_empty_gate(config)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()


def test_build_vault_with_pcg_layer(tmp_path, capsys, mock_upstream):
    config = _build_full_fixture_vault(tmp_path, capsys, mock_upstream)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    assert "ok: vault generated" in capsys.readouterr().out
    pcg_dir = config.vault_dir / "PCG"
    assert (pcg_dir / "PCG.md").exists()
    assert (pcg_dir / "K" / "KNOWN TRAFFIC.md").exists()
    assert (pcg_dir / "T" / "TRAFFIC PATTERN.md").exists()
    assert (pcg_dir / "O" / "OUTER FIX.md").exists()
    status = (config.vault_dir / "Source Status.md").read_text(encoding="utf-8")
    assert "| Pilot/Controller Glossary | Change 3 — effective 2026-07-09 |" in status

    # Idempotent, and validate agrees byte-for-byte.
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    assert "0 written" in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "ok: vault matches canonical layer" in capsys.readouterr().out


def test_build_refuses_to_drop_pcg_notes_while_new_edition_unparsed(
    tmp_path, capsys, mock_upstream
):
    """fetch pcg clears canonical_hash; a build in that window must not delete PCG output."""
    config = _build_full_fixture_vault(tmp_path, capsys, mock_upstream)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    note = config.vault_dir / "PCG" / "K" / "KNOWN TRAFFIC.md"
    before = note.read_bytes()
    # The publications page is served by the shared FAA fake (`upstream.aim`).
    replaced = mock_upstream.aim.publications.replace(
        "Pilot/Controller Glossary Basic with Change 1, 2 and 3</a> "
        "<small>(<abbr>HTML</abbr>)</small> <small>(effective 7/9/2026)</small>",
        "Pilot/Controller Glossary Basic with Change 1, 2, 3 and 4</a> "
        "<small>(<abbr>HTML</abbr>)</small> <small>(effective 1/1/2027)</small>",
    )
    assert replaced != mock_upstream.aim.publications
    mock_upstream.aim.publications = replaced
    mock_upstream.pcg.pages["index.html"] = (
        mock_upstream.pcg.pages["index.html"]
        .replace(b"<p>Change: 3</p>", b"<p>Change: 4</p>")
        .replace(b"<p>Effective: 7/9/26</p>", b"<p>Effective: 1/1/27</p>")
    )
    assert main(["--root", str(tmp_path), "fetch", "pcg"]) == EXIT_OK
    assert SourceManifest.load(config.manifest_path).sources["pcg"].canonical_hash is None
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "has not been parsed yet" in err and "parse pcg" in err
    assert note.read_bytes() == before
    assert main(["--root", str(tmp_path), "parse", "pcg"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    assert b"effective 2027-01-01" in note.read_bytes()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


def test_build_vault_without_aim_layer_covers_far_only(tmp_path, capsys, mock_upstream):
    config = Config.load(tmp_path)
    SourceManifest.default().save(config.manifest_path)
    for argv in (["fetch", "ecfr"], ["parse", "ecfr"]):
        assert main(["--root", str(tmp_path), *argv]) == EXIT_OK
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "the vault will not cover the AIM" in out
    assert "the vault will not cover the PCG" in out
    assert not (config.vault_dir / "AIM").exists()
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK


# ---------------------------------------------------------------------------
# enrich (Phase 9, plan §36)
# ---------------------------------------------------------------------------


def _fixture_concepts() -> dict:
    return {
        "schema": 1,
        "concepts": [
            {
                "id": "vfr-minimums",
                "title": "VFR Visibility Rules",
                "area": "Weather",
                "description": "Curator's words.",
                "far": ["91.155", "Part 91"],
            },
            {
                "id": "special-vfr",
                "title": "Special VFR Clearances",
                "area": "Weather",
                "prerequisites": ["vfr-minimums"],
                "far": ["91.157"],
            },
        ],
    }


def test_enrich_writes_related_links_and_is_idempotent(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "enrich"]) == EXIT_ERROR
    assert "no accepted eCFR canonical layer" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    capsys.readouterr()

    assert main(["--root", str(tmp_path), "enrich"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "no accepted AIM canonical layer; deriving FAR-only" in out
    assert "created empty review overlay" in out
    assert "related links written" in out and "11 units" in out
    related = json.loads(config.related_path.read_text(encoding="utf-8"))
    manifest = SourceManifest.load(config.manifest_path)
    assert related["inputs"] == {"ecfr": manifest.sources["ecfr_title_14"].canonical_hash}
    assert related["provider"] == {"id": "lexical-tfidf", "version": 1}
    assert related["units"]["cfr-14-91.155"] == [{"target": "cfr-14-91.157", "score": 0.4671}]
    review = json.loads(config.related_review_path.read_text(encoding="utf-8"))
    assert review["deny"] == []
    first = config.related_path.read_bytes()

    assert main(["--root", str(tmp_path), "enrich"]) == EXIT_OK
    assert "related links unchanged" in capsys.readouterr().out
    assert config.related_path.read_bytes() == first

    # Rendered: § 91.175's suggestion is a pure similarity link; § 91.155's
    # sole suggestion is its explicit cross-reference and is not repeated.
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    far = config.vault_dir / "FAR" / "Part 091"
    assert "## Related (derived)" in (far / "91.175.md").read_text(encoding="utf-8")
    assert "## Related (derived)" not in (far / "91.155.md").read_text(encoding="utf-8")
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "vault matches canonical layer (19 notes)" in capsys.readouterr().out


def test_enrich_review_overlay_is_validated_and_applied(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    config.enrichment_dir.mkdir(parents=True)
    config.related_review_path.write_text(
        json.dumps(
            {
                "deny": [
                    {"unit": "cfr-14-91.175", "target": "cfr-14-91.227", "reason": "test"},
                    {"unit": "cfr-14-91.3", "target": "cfr-14-91.999", "reason": "stale"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert main(["--root", str(tmp_path), "enrich"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "warning: review denial cfr-14-91.3 → cfr-14-91.999 is stale" in out
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    note = (config.vault_dir / "FAR" / "Part 091" / "91.175.md").read_text(encoding="utf-8")
    assert "## Related (derived)" not in note  # its only suggestion was denied

    config.related_review_path.write_text(
        json.dumps({"deny": [{"unit": "cfr-14-nope", "target": "cfr-14-91.3", "reason": "x"}]}),
        encoding="utf-8",
    )
    assert main(["--root", str(tmp_path), "enrich"]) == EXIT_ERROR
    assert "unknown unit id(s): cfr-14-nope" in capsys.readouterr().err


def test_build_vault_rejects_stale_related_links(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "enrich"]) == EXIT_OK
    capsys.readouterr()
    doc = json.loads(config.related_path.read_text(encoding="utf-8"))
    doc["inputs"]["ecfr"] = "sha256:" + "0" * 64
    config.related_path.write_text(json.dumps(doc), encoding="utf-8")
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "related-links file is stale" in err and "far-aim enrich" in err
    assert not (config.vault_dir / "FAR").exists()  # nothing written (plan §32.13)

    config.related_review_path.unlink()
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    assert "review file" in capsys.readouterr().err


def test_build_vault_renders_concepts_and_removes_them_cleanly(tmp_path, capsys):
    config = _accepted_snapshot(tmp_path, SLICE_XML.read_bytes())
    assert main(["--root", str(tmp_path), "parse", "ecfr"]) == EXIT_OK
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    capsys.readouterr()
    before = _vault_bytes(config)

    config.enrichment_dir.mkdir(parents=True)
    config.concepts_path.write_text(json.dumps(_fixture_concepts()), encoding="utf-8")
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "22 notes" in out and "4 written" in out  # 3 concept files + Home
    concepts = config.vault_dir / "Concepts"
    assert sorted(p.name for p in concepts.iterdir()) == [
        "Concept Map.md",
        "Special VFR Clearances.md",
        "VFR Visibility Rules.md",
    ]
    concept = (concepts / "VFR Visibility Rules.md").read_text(encoding="utf-8")
    assert 'type: "concept"' in concept and "generated: true" in concept
    assert "- [[91.155|§ 91.155 — Basic VFR weather minimums]]" in concept
    home = (config.vault_dir / "Home.md").read_text(encoding="utf-8")
    assert "[[Concept Map]]" in home
    assert main(["--root", str(tmp_path), "validate"]) == EXIT_OK
    assert "(22 notes)" in capsys.readouterr().out

    # A curated note parked inside Concepts/ survives; a reference to a
    # section the slice lacks fails the build before anything is written.
    (concepts / "My Notes.md").write_text("# mine\n", encoding="utf-8")
    doc = _fixture_concepts()
    doc["concepts"][0]["far"].append("61.109")
    config.concepts_path.write_text(json.dumps(doc), encoding="utf-8")
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_ERROR
    assert "references FAR note '61.109'" in capsys.readouterr().err
    assert (concepts / "VFR Visibility Rules.md").exists()

    # Separability (plan §32.12): remove the layer, rebuild — only the
    # generated concept notes go, and the vault is byte-identical to before.
    shutil.rmtree(config.enrichment_dir)
    assert main(["--root", str(tmp_path), "build-vault"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "3 stale deleted" in out
    assert (concepts / "My Notes.md").exists()
    after = {p: b for p, b in _vault_bytes(config).items() if p.name != "My Notes.md"}
    assert after == before
