"""Project configuration: filesystem layout rooted at the repository root.

All pipeline paths derive from a single root so tests and tooling can point
the whole system at a temporary directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    root: Path

    @classmethod
    def load(cls, root: Path | None = None) -> Config:
        return cls(root=(root or Path.cwd()).resolve())

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def raw_dir(self) -> Path:
        """Local raw-source cache. Gitignored; see plan §6.2."""
        return self.data_dir / "raw"

    @property
    def normalized_dir(self) -> Path:
        return self.data_dir / "normalized"

    @property
    def manifests_dir(self) -> Path:
        return self.data_dir / "manifests"

    @property
    def manifest_path(self) -> Path:
        return self.manifests_dir / "sources.json"

    @property
    def vault_dir(self) -> Path:
        return self.root / "vault"
