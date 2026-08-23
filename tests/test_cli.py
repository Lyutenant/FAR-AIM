import httpx
import pytest

from far_aim.cli import EXIT_ERROR, EXIT_NOT_IMPLEMENTED, EXIT_OK, main
from far_aim.config import Config
from far_aim.manifest import SourceManifest, SourceState
from far_aim.sources import ecfr
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
        ["parse", "ecfr"],
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
