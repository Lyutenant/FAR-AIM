"""Source manifest: the machine-readable registry of upstream source state.

The manifest (plan §15) is the only home for volatile timestamps such as
`last_checked_at` — generated notes must never carry them (plan §3.4).
Serialization is deterministic: sorted keys, stable indentation, trailing
newline, `None` fields omitted.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

SCHEMA_VERSION = 1

# Errors meaning "this platform/filesystem cannot fsync a directory".
_FSYNC_DIR_UNSUPPORTED = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF, errno.EACCES, errno.EPERM}
)

KNOWN_SOURCES = ("ecfr_title_14", "aim", "pcg")


class ManifestError(ValueError):
    """A manifest file could not be read or failed schema validation."""


@dataclass
class SourceState:
    """Tracked state for one upstream source."""

    last_checked_at: str | None = None
    accepted_version: str | None = None
    effective_date: str | None = None
    change: int | None = None
    raw_hash: str | None = None
    canonical_hash: str | None = None
    # FAA publications only: the edition label and HTML index URL the
    # accepted snapshot was fetched from. Generated notes render both, and
    # canonical hashes exclude provenance, so `validate` needs them pinned
    # here to detect an altered `source` block.
    edition_label: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, str | int]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_dict(cls, data: object, *, source_name: str) -> SourceState:
        if not isinstance(data, dict):
            raise ManifestError(f"source {source_name!r}: entry must be a JSON object")
        allowed = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ManifestError(f"source {source_name!r}: unknown fields {unknown}")
        change = data.get("change")
        if change is not None and (isinstance(change, bool) or not isinstance(change, int)):
            raise ManifestError(f"source {source_name!r}: 'change' must be an integer")
        for key in allowed - {"change"}:
            if key in data and not isinstance(data[key], str):
                raise ManifestError(f"source {source_name!r}: {key!r} must be a string")
        return cls(**data)


@dataclass
class SourceManifest:
    sources: dict[str, SourceState] = field(default_factory=dict)

    @classmethod
    def default(cls) -> SourceManifest:
        return cls(sources={name: SourceState() for name in KNOWN_SOURCES})

    @classmethod
    def from_dict(cls, data: object) -> SourceManifest:
        if not isinstance(data, dict):
            raise ManifestError("manifest must be a JSON object")
        unknown_top = sorted(set(data) - {"schema_version", "sources"})
        if unknown_top:
            raise ManifestError(f"unknown top-level fields {unknown_top}")
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
            raise ManifestError(f"unsupported schema_version: {version!r}")
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, dict):
            raise ManifestError("'sources' must be a JSON object")
        unknown = sorted(set(raw_sources) - set(KNOWN_SOURCES))
        if unknown:
            raise ManifestError(
                f"unknown sources {unknown}; known sources: {sorted(KNOWN_SOURCES)}"
            )
        missing = sorted(set(KNOWN_SOURCES) - set(raw_sources))
        if missing:
            raise ManifestError(f"missing sources {missing}")
        sources = {
            name: SourceState.from_dict(entry, source_name=name)
            for name, entry in raw_sources.items()
        }
        return cls(sources=sources)

    @classmethod
    def load(cls, path: Path) -> SourceManifest:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "sources": {name: state.to_dict() for name, state in sorted(self.sources.items())},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def save(self, path: Path) -> None:
        """Write atomically: an interrupted save must never destroy the last good registry."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.to_json())
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        # The rename itself is only durable once the directory entry is
        # synced. Platforms that cannot open/fsync a directory are tolerated;
        # real I/O errors propagate.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError as exc:
            if exc.errno in _FSYNC_DIR_UNSUPPORTED:
                return
            raise
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            if exc.errno not in _FSYNC_DIR_UNSUPPORTED:
                raise
        finally:
            os.close(dir_fd)
