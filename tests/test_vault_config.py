"""The committed Obsidian configuration the generated notes rely on.

``vault/.obsidian/`` is curated, never written by ``build-vault``; these
checks keep the presentation contract from drifting: the hierarchy snippet
exists, is enabled, and targets the class every official-text note carries.
"""

from __future__ import annotations

import json
from pathlib import Path

from far_aim.generate.hierarchy import TEXT_CSS_CLASS

_OBSIDIAN = Path(__file__).resolve().parent.parent / "vault" / ".obsidian"
SNIPPET = "far-aim-hierarchy"


def test_hierarchy_snippet_is_committed_and_enabled():
    css = (_OBSIDIAN / "snippets" / f"{SNIPPET}.css").read_text(encoding="utf-8")
    assert f".markdown-preview-view.{TEXT_CSS_CLASS}" in css
    for prop in ("--far-aim-indent", "--far-aim-guide-width", "--far-aim-guide-color"):
        assert prop in css
    appearance = json.loads((_OBSIDIAN / "appearance.json").read_text(encoding="utf-8"))
    assert SNIPPET in appearance["enabledCssSnippets"]
