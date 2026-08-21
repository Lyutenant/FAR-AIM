from pathlib import Path

from far_aim.config import Config


def test_paths_derive_from_root(tmp_path):
    config = Config.load(tmp_path)
    assert config.root == tmp_path.resolve()
    assert config.data_dir == config.root / "data"
    assert config.raw_dir == config.root / "data" / "raw"
    assert config.normalized_dir == config.root / "data" / "normalized"
    assert config.manifest_path == config.root / "data" / "manifests" / "sources.json"
    assert config.vault_dir == config.root / "vault"


def test_default_root_is_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = Config.load()
    assert config.root == Path(tmp_path).resolve()
