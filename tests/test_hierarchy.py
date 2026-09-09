"""Nested-list composition shared by the FAR, AIM and PCG renderers.

These tests pin the structural guarantee the vault's readability rests on:
a paragraph tree becomes a nested Markdown list whose every child line is
inside its parent item (at least the item's content column) and short of
CommonMark's indented-code threshold (fewer than four columns past it), at
any depth and with any block kind — tables, callouts, extracts, hard-break
continuations — nested inside.
"""

from __future__ import annotations

import re

import pytest

from far_aim.generate import BuildError
from far_aim.generate.aim_markdown import render_aim_blocks
from far_aim.generate.hierarchy import INDENT, TEXT_CSS_CLASS, indent, list_item
from far_aim.generate.markdown import escape_md, render_blocks

_MARKER = re.compile(r"^( *)- ")


def _check_item_columns(text: str) -> None:
    """Every line of a nested list sits inside its item and is not code.

    Walks the rendered text keeping a stack of open items (marker indent →
    content column). A non-marker, non-blank line must be indented at least
    to the innermost open item's content column and less than four columns
    past it; a marker line opens a new item at its own indent (popping items
    it does not belong to).
    """
    stack: list[int] = []  # content columns of open items
    for line in text.split("\n"):
        if not line.strip():
            continue
        lead = len(line) - len(line.lstrip(" "))
        marker = _MARKER.match(line)
        if marker:
            while stack and lead < stack[-1]:
                stack.pop()
            if stack:
                assert stack[-1] <= lead < stack[-1] + 4, line
            stack.append(lead + 2)
            continue
        while stack and lead < stack[-1]:
            stack.pop()
        if stack:
            assert stack[-1] <= lead < stack[-1] + 4, line


def _para(label: str, text: str, *children: dict) -> dict:
    return {
        "type": "paragraph",
        "label": label,
        "designator": label.strip("()"),
        "subject": None,
        "text": text,
        "children": list(children),
    }


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def test_indent_prefixes_non_empty_lines_only():
    assert indent("a\n\nb") == f"{INDENT}a\n\n{INDENT}b"
    assert INDENT == "    "
    assert TEXT_CSS_CLASS == "far-aim-text"


def test_list_item_nests_children_under_head():
    assert list_item("**(a)** Head.", ["child one", "> quoted\n> body"]) == (
        "- **(a)** Head.\n\n    child one\n\n    > quoted\n    > body"
    )


def test_list_item_indents_hard_break_continuations():
    assert list_item("first\\\nsecond", []) == "- first\\\n    second"


def test_list_item_refuses_empty_head():
    with pytest.raises(BuildError):
        list_item("   ", ["child"])


def test_composition_is_relative_not_absolute():
    inner = list_item("**(i)** deep", [])
    middle = list_item("**(1)** mid", [inner])
    outer = list_item("**(a)** top", [middle])
    assert outer == "- **(a)** top\n\n    - **(1)** mid\n\n        - **(i)** deep"
    _check_item_columns(outer)


# ---------------------------------------------------------------------------
# FAR paragraph trees
# ---------------------------------------------------------------------------


def test_six_level_cfr_tree_renders_nested():
    tree = _para(
        "(a)",
        "Level one.",
        _para(
            "(1)",
            "Level two.",
            _para(
                "(i)",
                "Level three.",
                _para(
                    "(A)",
                    "Level four.",
                    _para("(1)", "Level five.", _para("(i)", "Level six.")),
                ),
            ),
        ),
    )
    [chunk] = render_blocks([tree])
    lines = [line for line in chunk.split("\n") if line]
    assert [len(line) - len(line.lstrip()) for line in lines] == [0, 4, 8, 12, 16, 20]
    assert lines[-1] == f"{INDENT * 5}- **(i)** Level six."
    _check_item_columns(chunk)


def test_blocks_inside_a_nested_item_stay_inside_it():
    table = {
        "type": "table",
        "caption": "Table 1",
        "header_rows": [[{"header": True, "text": "A"}, {"header": True, "text": "B"}]],
        "rows": [[{"text": "1"}, {"text": "2"}]],
        "foot_rows": [],
    }
    html_table = {
        "type": "table",
        "caption": None,
        "header_rows": [[{"header": True, "text": "A", "colspan": 2}]],
        "rows": [[{"text": "1"}, {"text": "2"}]],
        "foot_rows": [],
    }
    note = {
        "type": "note",
        "heading": "Note:",
        "blocks": [{"type": "text", "style": "plain", "text": "Body."}],
    }
    extract = {
        "type": "extract",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Inside"},
            {"type": "text", "style": "plain", "text": "Quoted."},
        ],
    }
    tree = _para(
        "(a)",
        "Top.",
        _para("(1)", "Mid.", table, html_table, note, extract, _para("(i)", "Deep.")),
    )
    [chunk] = render_blocks([tree])
    _check_item_columns(chunk)
    expected_inner = "\n".join(
        [
            "    - **(1)** Mid.",
            "",
            "        *Table 1*",
            "",
            "        | A | B |",
            "        | --- | --- |",
            "        | 1 | 2 |",
            "",
            "        <table>",
            "        <thead>",
            '        <tr><th colspan="2">A</th></tr>',
            "        </thead>",
            "        <tbody>",
            "        <tr><td>1</td><td>2</td></tr>",
            "        </tbody>",
            "        </table>",
            "",
            "        > [!note] Note:",
            "        > Body.",
            "",
            "        > ### Inside",
            "        >",
            "        > Quoted.",
            "",
            "        - **(i)** Deep.",
        ]
    )
    assert chunk == "- **(a)** Top.\n\n" + expected_inner


def test_definition_with_children_nests():
    block = {
        "type": "definition",
        "term": "Category:",
        "text": "",
        "children": [_para("(1)", "As used with airmen."), _para("(2)", "As used with aircraft.")],
    }
    assert render_blocks([block]) == [
        "- *Category:*\n\n"
        "    - **(1)** As used with airmen.\n\n"
        "    - **(2)** As used with aircraft."
    ]


def test_headless_paragraph_splices_children_at_its_level():
    block = {
        "type": "paragraph",
        "label": None,
        "designator": None,
        "subject": None,
        "text": "",
        "children": [_para("(1)", "Only child.")],
    }
    assert render_blocks([block]) == ["- **(1)** Only child."]


def test_escaped_text_cannot_open_markup_inside_an_item():
    for text in ("- dash first", "# hash first", "1. ordered", "1) ordered", "[ ] task"):
        [chunk] = render_blocks([_para("(a)", text)])
        assert chunk == f"- **(a)** {escape_md(text)}"
        body = chunk[len("- **(a)** ") :]
        assert body[0] == "\\" or body.startswith("1\\")


# ---------------------------------------------------------------------------
# AIM lists
# ---------------------------------------------------------------------------


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def test_aim_list_inside_a_note_box_nests_within_the_callout():
    # A level-two list inside a NOTE- box must start at the callout's own
    # column, not at an absolute offset that would read as an indented code
    # block; ``level`` chooses only the marker glyph.
    blocks = [
        {
            "type": "list",
            "level": 1,
            "items": [
                {
                    "blocks": [
                        _text("Item"),
                        {
                            "type": "note",
                            "kind": "note",
                            "title": "NOTE-",
                            "blocks": [
                                {
                                    "type": "list",
                                    "level": 2,
                                    "items": [{"blocks": [_text("Boxed")]}],
                                }
                            ],
                        },
                    ]
                }
            ],
        }
    ]
    assert render_aim_blocks(blocks) == [
        "- **a.** Item\n\n    > [!note] NOTE-\n    > - **1.** Boxed",
    ]
    _check_item_columns(render_aim_blocks(blocks)[0])


def test_aim_six_levels_nest_with_figure_and_table_inside():
    figure = {
        "type": "figure",
        "number": "FIG 1-1-1",
        "title": "A figure",
        "image": {"alt": "", "sha256": "sha256:0", "source": {"src": "images/fig.svg"}},
    }

    def lst(level: int, *blocks: dict) -> dict:
        return {"type": "list", "level": level, "items": [{"blocks": list(blocks)}]}

    tree = lst(
        1, _text("L1"), lst(2, _text("L2"), figure, lst(3, _text("L3"), lst(4, _text("L4"),
        lst(5, _text("L5"), lst(6, _text("L6"))))))
    )
    [chunk] = render_aim_blocks([tree])
    _check_item_columns(chunk)
    assert f"{INDENT * 5}- **[1]** L6" in chunk
    assert f"{INDENT * 2}**FIG 1-1-1** *A figure*\n\n{INDENT * 2}![[fig.svg|A figure]]" in chunk
