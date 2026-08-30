"""Canonical AIM content blocks → Obsidian Markdown (plan §10.2, §3.1).

Official wording is reproduced exactly; the only transformations are
deterministic formatting ones. Design mirrors the FAR renderer:

- **Lists render flat**, each item its own paragraph led by its bold marker.
  Markers follow the FAA HTML edition's six-level convention (``a.`` ``1.``
  ``(a)`` ``(1)`` ``[a]`` ``[1]``) and are derived from list level and item
  position — in the source they are CSS-generated, never text. The HTML
  ``type`` attribute many ``<ol>`` elements carry (``type="a"`` on level
  two, ``type="i"`` on level three) is a DITA export artifact that the
  edition's own stylesheet overrides, and the official wording confirms the
  stylesheet: AIM 4-1-20 refers to the fifth item of a ``type="i"``
  level-three list as "(e) above", not "(v)". ``html_type`` is therefore
  preserved in canonical JSON for losslessness but deliberately ignored
  here. Nesting reaches depth six and items contain tables, figures, and
  callouts, which break inside Markdown list indentation; the printed AIM
  is read flat via its markers anyway.
- **Boxes** (NOTE-, EXAMPLE-, REFERENCE-, PHRASEOLOGY-) become Obsidian
  callouts titled with the box's verbatim label.
- **Figures** embed the archived asset (``![[file]]``) beneath a caption
  line; bare images (form reproductions) embed the same way.
- **Tables** render as pipe tables when every cell is a single line of text
  (single header row, no spans) and as inline HTML otherwise.

Every block type must have a renderer; an unknown type raises (plan §32.2).
"""

from __future__ import annotations

from far_aim.generate import BuildError
from far_aim.generate.markdown import escape_md, html_escape

CALLOUT_KINDS = {
    "note": "note",
    "example": "example",
    "reference": "cite",
    "phraseology": "quote",
}


def _alpha(index: int) -> str:
    """0 → ``a`` … 25 → ``z``, 26 → ``aa`` (CSS ``lower-alpha`` progression)."""
    letters = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("a") + rem) + letters
    return letters


def list_marker(level: int, index: int) -> str:
    """The FAA HTML edition's marker for item ``index`` (0-based) at ``level``.

    Level alone decides the style (see the module docstring on ``html_type``).
    """
    if level == 1:
        return f"{_alpha(index)}."
    if level == 2:
        return f"{index + 1}."
    if level == 3:
        return f"({_alpha(index)})"
    if level == 4:
        return f"({index + 1})"
    if level == 5:
        return f"[{_alpha(index)}]"
    if level == 6:
        return f"[{index + 1}]"
    raise BuildError(f"unsupported list level {level!r}")


def asset_name(src: str) -> str:
    """``images/aim0401_fig79.svg`` → ``aim0401_fig79.svg`` (the vault asset filename)."""
    return src.rsplit("/", 1)[-1]


def _quote(chunks: list[str]) -> str:
    body = "\n\n".join(chunks)
    return "\n".join(">" if not line else f"> {line}" for line in body.split("\n"))


def text_md(text: str) -> str:
    """Escaped official text with source ``<br>`` breaks kept visible.

    The parser preserves a ``<br>`` as ``\\n``; a bare newline is only a soft
    break in CommonMark (rendered as a space), so each one becomes a
    backslash hard break, which CommonMark and Obsidian both honor — inside
    callouts too.
    """
    return escape_md(text).replace("\n", "\\\n")


def _render_text(block: dict) -> list[str]:
    text = block.get("text") or ""
    return [text_md(text)] if text else []


def _render_heading(block: dict) -> list[str]:
    return [f"### {escape_md(block['text'].replace(chr(10), ' '))}"]


def _render_list(block: dict, asset_prefix: str) -> list[str]:
    chunks: list[str] = []
    level = block["level"]
    for index, item in enumerate(block["items"]):
        # The marker sits inside ``**…**`` so it can never open a Markdown
        # list; left unescaped, ``1.`` stays readable in the source.
        marker = f"**{list_marker(level, index)}**"
        blocks = item["blocks"]
        if blocks and blocks[0]["type"] == "text":
            chunks.append(f"{marker} {text_md(blocks[0]['text'])}")
            chunks.extend(render_aim_blocks(blocks[1:], asset_prefix))
        else:
            chunks.append(marker)
            chunks.extend(render_aim_blocks(blocks, asset_prefix))
    return chunks


def _render_note(block: dict, asset_prefix: str) -> list[str]:
    callout = CALLOUT_KINDS.get(block["kind"])
    if callout is None:
        raise BuildError(f"unknown note kind {block['kind']!r}")
    lines = [f"> [!{callout}] {escape_md(block['title'].replace(chr(10), ' '))}"]
    inner = render_aim_blocks(block["blocks"], asset_prefix)
    if inner:
        lines.append(_quote(inner))
    return ["\n".join(lines)]


def _embed(src: str, alt: str) -> str:
    name = asset_name(src)
    if alt:
        safe_alt = alt.replace("[", "").replace("]", "").replace("|", "/").replace("\n", " ")
        return f"![[{name}|{safe_alt}]]"
    return f"![[{name}]]"


def _caption(number: str | None, title: str | None) -> list[str]:
    parts = []
    if number:
        parts.append(f"**{escape_md(number.replace(chr(10), ' '))}**")
    if title:
        parts.append(f"*{escape_md(title.replace(chr(10), ' '))}*")
    return parts


def _render_figure(block: dict) -> list[str]:
    caption_parts = _caption(block.get("number"), block.get("title"))
    image = block["image"]
    chunks = []
    if caption_parts:
        chunks.append(" ".join(caption_parts))
    chunks.append(_embed(image["source"]["src"], block.get("title") or image.get("alt") or ""))
    return chunks


def _render_image(block: dict) -> list[str]:
    return [_embed(block["source"]["src"], block.get("alt") or "")]


def _cell_is_simple(cell: dict) -> bool:
    """One line of text, one image, or empty — renderable inside a pipe-table cell."""
    blocks = cell["blocks"]
    if "colspan" in cell or "rowspan" in cell:
        return False
    if not blocks:
        return True
    if len(blocks) != 1:
        return False
    block = blocks[0]
    if block["type"] == "text":
        return "\n" not in block["text"]
    return block["type"] in ("image", "figure")


def _pipe_safe(block: dict) -> bool:
    header_rows = block["header_rows"]
    if len(header_rows) != 1 or block["foot_rows"]:
        return False
    width = len(header_rows[0])
    if width == 0:
        return False
    for row in [*header_rows, *block["rows"]]:
        if len(row) != width or not all(_cell_is_simple(cell) for cell in row):
            return False
    return True


def _pipe_cell(cell: dict) -> str:
    """Pipe-table cell content; image cells embed the asset (Obsidian renders
    ``![[file]]`` inside table cells)."""
    blocks = cell["blocks"]
    if not blocks:
        return ""
    block = blocks[0]
    if block["type"] == "text":
        return escape_md(block["text"]).replace("|", "\\|")
    if block["type"] == "image":
        return _embed(block["source"]["src"], block.get("alt") or "").replace("|", "\\|")
    # A figure keeps its visible caption (number and title) ahead of the embed.
    image = block["image"]
    alt = block.get("title") or image.get("alt") or ""
    parts = _caption(block.get("number"), block.get("title"))
    parts.append(_embed(image["source"]["src"], alt))
    return " ".join(parts).replace("|", "\\|")


def _render_pipe_table(block: dict) -> str:
    header = block["header_rows"][0]
    lines = [
        "| " + " | ".join(_pipe_cell(cell) for cell in header) + " |",
        "|" + " --- |" * len(header),
    ]
    lines.extend(
        "| " + " | ".join(_pipe_cell(cell) for cell in row) + " |" for row in block["rows"]
    )
    return "\n".join(lines)


def _html_inline(text: str) -> str:
    return html_escape(text).replace("\n", "<br>")


def _html_attr(value: str) -> str:
    """Attribute-safe escaping: text escaping plus both quote characters."""
    return html_escape(value).replace('"', "&quot;").replace("'", "&#39;")


def _html_blocks(blocks: list[dict], asset_prefix: str) -> str:
    """Cell content as HTML: text paragraphs, flat marker-led lists, boxes."""
    parts: list[str] = []
    for block in blocks:
        kind = block["type"]
        if kind == "text":
            parts.append(f"<p>{_html_inline(block['text'])}</p>")
        elif kind == "list":
            for index, item in enumerate(block["items"]):
                marker = html_escape(list_marker(block["level"], index))
                inner = _html_blocks(item["blocks"], asset_prefix)
                if inner.startswith("<p>"):
                    inner = f"<p><b>{marker}</b> " + inner[len("<p>") :]
                else:
                    inner = f"<p><b>{marker}</b></p>{inner}"
                parts.append(inner)
        elif kind == "note":
            inner = _html_blocks(block["blocks"], asset_prefix)
            parts.append(f"<p><b>{html_escape(block['title'])}</b></p>{inner}")
        elif kind == "heading":
            parts.append(f"<p><b>{_html_inline(block['text'])}</b></p>")
        elif kind in ("image", "figure"):
            # Wikilink embeds do not render inside raw HTML, so image cells
            # reference the asset by a path relative to the note itself. A
            # figure's visible caption (number, title) precedes the image.
            image = block if kind == "image" else block["image"]
            alt = block.get("title") if kind == "figure" else block.get("alt")
            if kind == "figure":
                caption = []
                if block.get("number"):
                    caption.append(f"<b>{_html_inline(block['number'])}</b>")
                if block.get("title"):
                    caption.append(f"<i>{_html_inline(block['title'])}</i>")
                if caption:
                    parts.append(f"<p>{' '.join(caption)}</p>")
            src = f"{asset_prefix}/{asset_name(image['source']['src'])}"
            parts.append(f'<p><img src="{_html_attr(src)}" alt="{_html_attr(alt or "")}"></p>')
        else:
            # Nested tables never occur inside AIM table cells; refuse rather
            # than drop one if an edition adds it.
            raise BuildError(f"unsupported block type {kind!r} inside a table cell")
    return "".join(parts)


def _html_cell(cell: dict, *, header_row: bool, asset_prefix: str) -> str:
    tag = "th" if (header_row or cell.get("header")) else "td"
    attrs = ""
    if "colspan" in cell:
        attrs += f' colspan="{cell["colspan"]}"'
    if "rowspan" in cell:
        attrs += f' rowspan="{cell["rowspan"]}"'
    return f"<{tag}{attrs}>{_html_blocks(cell['blocks'], asset_prefix)}</{tag}>"


def _render_html_table(block: dict, asset_prefix: str) -> str:
    lines = ["<table>"]

    def emit(rows: list[list[dict]], wrapper: str, *, header_row: bool) -> None:
        if not rows:
            return
        lines.append(f"<{wrapper}>")
        for row in rows:
            cells = "".join(
                _html_cell(cell, header_row=header_row, asset_prefix=asset_prefix) for cell in row
            )
            lines.append(f"<tr>{cells}</tr>")
        lines.append(f"</{wrapper}>")

    emit(block["header_rows"], "thead", header_row=True)
    emit(block["rows"], "tbody", header_row=False)
    emit(block["foot_rows"], "tfoot", header_row=False)
    lines.append("</table>")
    return "\n".join(lines)


def _render_table(block: dict, asset_prefix: str) -> list[str]:
    chunks = []
    caption_parts = _caption(block.get("number"), block.get("title"))
    if caption_parts:
        chunks.append(" ".join(caption_parts))
    if _pipe_safe(block):
        chunks.append(_render_pipe_table(block))
    else:
        chunks.append(_render_html_table(block, asset_prefix))
    return chunks


def asset_prefix_for(path_parts: tuple[str, ...]) -> str:
    """Relative path from a note's directory to ``AIM/assets``.

    ``("AIM", "Chapter 07", "7-1-10.md")`` → ``../assets``; the AIM index at
    ``("AIM", "AIM.md")`` → ``assets``. Raw-HTML image references cannot use
    wikilinks, so they must be resolvable from the note's own location.
    """
    if not path_parts or path_parts[0] != "AIM":
        raise BuildError(f"not an AIM note path: {path_parts!r}")
    return "../" * (len(path_parts) - 2) + "assets"


def render_aim_blocks(blocks: list[dict], asset_prefix: str = "../assets") -> list[str]:
    """Render AIM blocks to Markdown chunks (each chunk joined with blank lines).

    ``asset_prefix`` is the note-relative path to the assets directory, used
    only where wikilink embeds cannot be (images inside HTML tables).
    """
    chunks: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "table":
            chunks.extend(_render_table(block, asset_prefix))
            continue
        if kind == "list":
            chunks.extend(_render_list(block, asset_prefix))
            continue
        if kind == "note":
            chunks.extend(_render_note(block, asset_prefix))
            continue
        renderer = _RENDERERS.get(kind)
        if renderer is None:
            raise BuildError(f"no renderer for AIM block type {kind!r}")
        chunks.extend(renderer(block))
    return chunks


_RENDERERS = {
    "text": _render_text,
    "heading": _render_heading,
    "figure": _render_figure,
    "image": _render_image,
}


def collect_asset_names(blocks: list[dict], out: set[str]) -> None:
    """Every figure/image asset filename referenced by ``blocks`` (recursive)."""
    for block in blocks:
        kind = block["type"]
        if kind == "figure":
            out.add(asset_name(block["image"]["source"]["src"]))
        elif kind == "image":
            out.add(asset_name(block["source"]["src"]))
        elif kind == "list":
            for item in block["items"]:
                collect_asset_names(item["blocks"], out)
        elif kind == "note":
            collect_asset_names(block["blocks"], out)
        elif kind == "table":
            for rows in (block["header_rows"], block["rows"], block["foot_rows"]):
                for row in rows:
                    for cell in row:
                        collect_asset_names(cell["blocks"], out)
