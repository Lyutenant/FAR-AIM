import json
import os

import httpx
import pytest

from far_aim.cli import EXIT_ERROR, EXIT_NOT_IMPLEMENTED, EXIT_OK, main
from far_aim.config import Config
from far_aim.manifest import SourceManifest, SourceState
from far_aim.sources import ecfr
from tests.test_ecfr_source import FIXTURES as FIXTURES_DIR
from tests.test_ecfr_source import ISSUE_DATE, Upstream


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
        ["fetch", "aim"],
        ["fetch", "pcg"],
        ["parse", "aim"],
        ["parse", "pcg"],
        ["normalize"],
        ["diff"],
        ["build-vault"],
        ["update"],
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
    upstream = Upstream()
    monkeypatch.setattr(ecfr, "make_client", upstream.client)
    monkeypatch.setattr(ecfr, "MIN_XML_BYTES", 100)
    monkeypatch.setattr(ecfr, "MIN_SECTION_COUNT", 3)
    monkeypatch.setattr(ecfr.time, "sleep", lambda _s: None)
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
    assert "update available" not in captured.out


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
