"""Hierarchy → nested Markdown list items (shared by the FAR, AIM and PCG renderers).

Regulatory hierarchy — CFR paragraphs ``(a)(1)(i)(A)``, the AIM's six list
levels, PCG sub-lists — is emitted as native nested Markdown bullet lists:
one ``- `` item per paragraph or list item, with every child block (nested
paragraphs, continuation text, tables, callouts, extracts, figures) rendered
first and then indented under it. Obsidian's Reading View turns that into
real ``ul > li > ul > li`` nesting, which the committed CSS snippet
(``vault/.obsidian/snippets/far-aim-hierarchy.css``) indents, guides and
lets the reader fold; without the snippet the note is still an ordinary
nested list. The official label (``**(a)**``) stays in the text as the
visible marker, so nothing about the wording depends on presentation.

Why this is safe at every depth (CommonMark §5.2): a ``- `` marker at column
``4d`` puts the item's content column at ``4d + 2``; a child line indented by
``INDENT`` sits at ``4d + 4``, which is inside the item (``>= 4d + 2``) and
short of the indented-code threshold (``< 4d + 6``). After the list
container strips its indentation, every child line has exactly two residual
spaces — fine for HTML blocks (which allow three), pipe tables, ``>``
callouts, embeds and hard-break continuations. Composition is purely
relative: an item indents chunks that are already rendered, so a grandchild
ends up at eight columns only because its parent was wrapped first, and a
list rendered inside a callout body starts at the callout's own column
rather than at an absolute offset that would read as code.

An empty head is refused: ``- `` followed by a blank line and indented text
is an *empty* item followed by a top-level paragraph in CommonMark, which
would silently flatten the tree (plan §32.13: fail rather than degrade).
"""

from __future__ import annotations

from far_aim.generate import BuildError

INDENT = "    "
"""Indentation of one nesting level: four spaces under a two-wide ``- `` marker."""

TEXT_CSS_CLASS = "far-aim-text"
"""``cssclasses`` value carried by every note that renders official text; the
CSS snippet scopes its list styling to this class."""


def indent(chunk: str) -> str:
    """Indent every non-empty line of ``chunk`` by one level (blank lines stay empty).

    Blank lines must stay empty so that a blank line inside a child chunk
    still separates blocks and never becomes a whitespace-only "paragraph".
    """
    return "\n".join(f"{INDENT}{line}" if line else line for line in chunk.split("\n"))


def list_item(head: str, children: list[str]) -> str:
    """One Markdown list item: ``- head`` followed by each child chunk indented under it.

    ``head`` may span several lines (a hard-break continuation); every line
    after the first is indented so it stays inside the item. Children are
    already-rendered chunks and keep their internal blank lines; each is
    separated from the head and from its siblings by a blank line, so the
    list is *loose* and Obsidian renders item content as paragraphs.
    """
    if not head.strip():
        raise BuildError("list item with an empty head (would flatten the hierarchy)")
    first, *rest = head.split("\n")
    lines = [f"- {first}", *(indent(line) if line else line for line in rest)]
    for child in children:
        lines.append("")
        lines.append(indent(child))
    return "\n".join(lines)
