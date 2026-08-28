"""Obsidian vault generation from the canonical eCFR layer (plan §22 Phase 3)."""


class BuildError(Exception):
    """Vault generation failed; nothing has been written (plan §32.13)."""
