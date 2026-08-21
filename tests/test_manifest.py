from pathlib import Path

import pytest

from far_aim.manifest import KNOWN_SOURCES, ManifestError, SourceManifest, SourceState


def full_sources(**overrides):
    """A complete 'sources' dict with empty entries, selectively overridden."""
    sources = {name: {} for name in KNOWN_SOURCES}
    sources.update(overrides)
    return sources


def test_committed_registry_is_valid():
    """The registry shipped in the repo must always load (Phase 0 exit criterion)."""
    path = Path(__file__).resolve().parent.parent / "data" / "manifests" / "sources.json"
    assert path.exists(), "data/manifests/sources.json must be committed"
    manifest = SourceManifest.load(path)
    assert set(manifest.sources) == set(KNOWN_SOURCES)


def test_default_contains_all_known_sources():
    manifest = SourceManifest.default()
    assert set(manifest.sources) == set(KNOWN_SOURCES)
    assert all(state == SourceState() for state in manifest.sources.values())


def test_save_load_roundtrip(tmp_path):
    manifest = SourceManifest.default()
    manifest.sources["aim"] = SourceState(
        last_checked_at="2026-08-20T00:00:00Z",
        effective_date="2026-07-09",
        change=3,
        raw_hash="abc",
        canonical_hash="def",
    )
    path = tmp_path / "sources.json"
    manifest.save(path)
    loaded = SourceManifest.load(path)
    assert loaded == manifest


def test_serialization_is_deterministic(tmp_path):
    manifest = SourceManifest.default()
    manifest.sources["ecfr_title_14"] = SourceState(accepted_version="2026-08-19")
    first = manifest.to_json()
    second = manifest.to_json()
    assert first == second
    assert first.endswith("\n")
    # Insertion order must not affect output.
    reordered = SourceManifest(sources=dict(reversed(list(manifest.sources.items()))))
    assert reordered.to_json() == first


def test_none_fields_are_omitted():
    state = SourceState(accepted_version="2026-08-19")
    assert state.to_dict() == {"accepted_version": "2026-08-19"}


@pytest.mark.parametrize("version", [99, True, 1.0, "1", None])
def test_unsupported_schema_version_rejected(version):
    with pytest.raises(ManifestError, match="schema_version"):
        SourceManifest.from_dict({"schema_version": version, "sources": full_sources()})


def test_unknown_source_field_rejected():
    data = {"schema_version": 1, "sources": full_sources(aim={"bogus": "x"})}
    with pytest.raises(ManifestError, match="unknown fields"):
        SourceManifest.from_dict(data)


def test_wrong_field_types_rejected():
    data = {"schema_version": 1, "sources": full_sources(aim={"change": "three"})}
    with pytest.raises(ManifestError, match="integer"):
        SourceManifest.from_dict(data)


def test_boolean_change_rejected():
    data = {"schema_version": 1, "sources": full_sources(aim={"change": True})}
    with pytest.raises(ManifestError, match="integer"):
        SourceManifest.from_dict(data)


def test_unknown_top_level_field_rejected():
    data = {"schema_version": 1, "sources": full_sources(), "extra": {}}
    with pytest.raises(ManifestError, match="unknown top-level fields"):
        SourceManifest.from_dict(data)


def test_unknown_source_name_rejected():
    sources = full_sources()
    sources["ecfr_title14"] = sources.pop("ecfr_title_14")  # typo'd name
    data = {"schema_version": 1, "sources": sources}
    with pytest.raises(ManifestError, match="unknown sources"):
        SourceManifest.from_dict(data)


def test_missing_source_rejected():
    sources = full_sources()
    del sources["pcg"]
    data = {"schema_version": 1, "sources": sources}
    with pytest.raises(ManifestError, match="missing sources"):
        SourceManifest.from_dict(data)


def test_invalid_json_raises_manifest_error(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        SourceManifest.load(path)


def test_invalid_utf8_raises_manifest_error(tmp_path):
    path = tmp_path / "sources.json"
    path.write_bytes(b'{"schema_version": 1, \xff\xfe}')
    with pytest.raises(ManifestError, match="cannot read manifest"):
        SourceManifest.load(path)


def test_failed_save_preserves_existing_registry(tmp_path, monkeypatch):
    path = tmp_path / "sources.json"
    SourceManifest.default().save(path)
    original = path.read_text(encoding="utf-8")

    def broken_fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr("far_aim.manifest.os.fsync", broken_fsync)
    changed = SourceManifest.default()
    changed.sources["aim"] = SourceState(accepted_version="new")
    with pytest.raises(OSError, match="disk full"):
        changed.save(path)
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_leaves_no_temp_files(tmp_path):
    path = tmp_path / "sources.json"
    SourceManifest.default().save(path)
    SourceManifest.default().save(path)
    assert list(tmp_path.iterdir()) == [path]
