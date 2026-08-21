import pytest

from far_aim.cli import EXIT_ERROR, EXIT_NOT_IMPLEMENTED, EXIT_OK, main
from far_aim.config import Config
from far_aim.manifest import SourceManifest, SourceState


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
        ["fetch", "ecfr"],
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
