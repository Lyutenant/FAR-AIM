"""Canonical content blocks → Obsidian Markdown (plan §10, §3.1).

Official wording is reproduced exactly; the only transformations are
deterministic formatting ones (escaping, labels bolded, block layout).
Paragraph hierarchy renders *flat* — each paragraph is its own Markdown
paragraph led by its bold CFR label, children following in order — because
depth-5 nesting containing tables and extracts breaks inside Markdown list
indentation (4-space continuation text becomes a code block), and printed
CFR is read flat via its ``(a)(1)(i)`` labels anyway.

Every block type must have a renderer: an unknown type raises instead of
being dropped (plan §32.2).
"""

from __future__ import annotations

import re

from far_aim.generate import BuildError

ECFR_BASE_URL = "https://www.ecfr.gov"

# Characters that could turn official text into unintended markup anywhere
# in a line; line-leading forms (#, >, list markers, ordered-list numbers)
# are escaped separately. $ is included because Obsidian treats paired
# dollar signs as inline-math delimiters — two currency amounts in one
# paragraph would otherwise render the span between them as math.
_INLINE_ESCAPE_RE = re.compile(r"[\\`*_\[<$]")
_LEADING_ESCAPE_RE = re.compile(r"^([#>+-])", re.MULTILINE)
_LEADING_ORDERED_RE = re.compile(r"^([0-9]+)\.", re.MULTILINE)

_HEADING_PREFIX = {1: "###", 2: "####", 3: "#####", 5: "######"}


def escape_md(text: str) -> str:
    """Escape Markdown syntax; the rendered wording stays exactly the source's."""
    escaped = _INLINE_ESCAPE_RE.sub(r"\\\g<0>", text)
    escaped = _LEADING_ESCAPE_RE.sub(r"\\\1", escaped)
    return _LEADING_ORDERED_RE.sub(r"\1\\.", escaped)


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def graphic_url(src: str) -> str:
    if not src.startswith("/"):
        src = "/" + src
    return ECFR_BASE_URL + src


def _quote(chunks: list[str]) -> str:
    """Prefix every line of the joined chunks with ``> `` (callout/quote body)."""
    body = "\n\n".join(chunks)
    return "\n".join(">" if not line else f"> {line}" for line in body.split("\n"))


def _run_in(*pieces: str) -> str:
    return " ".join(piece for piece in pieces if piece)


def _render_paragraph(block: dict) -> list[str]:
    label = block.get("label")
    subject = block.get("subject")
    text = block.get("text") or ""
    head = _run_in(
        f"**{escape_md(label)}**" if label else "",
        f"*{escape_md(subject)}*" if subject else "",
        escape_md(text) if text else "",
    )
    chunks = [head] if head else []
    chunks.extend(render_blocks(block.get("children") or []))
    return chunks


def _render_definition(block: dict) -> list[str]:
    head = _run_in(f"*{escape_md(block['term'])}*", escape_md(block["text"]))
    chunks = [head]
    chunks.extend(render_blocks(block.get("children") or []))
    return chunks


def _render_text(block: dict) -> list[str]:
    text = block.get("text") or ""
    if not text:
        return []
    if block.get("style") == "table-caption":
        return [f"*{escape_md(text)}*"]
    return [escape_md(text)]


def _render_heading(block: dict) -> list[str]:
    prefix = _HEADING_PREFIX.get(block["level"])
    if prefix is None:
        raise BuildError(f"unknown heading level {block['level']!r}")
    return [f"{prefix} {escape_md(block['text'])}"]


def _render_example(block: dict) -> list[str]:
    heading = block.get("heading")
    return [_run_in(f"*{escape_md(heading)}*" if heading else "", escape_md(block["text"]))]


def _render_image(block: dict) -> list[str]:
    # A link, not an embed: hotlinking source graphics is off-limits
    # (plan §4.2); archiving them as vault assets is later-phase work.
    src = block["src"]
    basename = src.rsplit("/", 1)[-1]
    return [f"[eCFR graphic {basename}]({graphic_url(src)})"]


def _render_math(block: dict) -> list[str]:
    chunks = [_render_image({"src": src})[0] for src in block.get("images") or []]
    text = block.get("text") or ""
    if text:
        chunks.append(escape_md(text))
    return chunks


def _render_extract(block: dict) -> list[str]:
    inner = render_blocks(block["blocks"])
    return [_quote(inner)] if inner else []


def _render_footnote(block: dict) -> list[str]:
    inner = render_blocks(block["blocks"])
    return [_quote(inner)] if inner else []


def _render_note(block: dict) -> list[str]:
    heading = block.get("heading")
    title = f" {escape_md(heading)}" if heading else ""
    lines = [f"> [!note]{title}"]
    inner = render_blocks(block.get("blocks") or [])
    if inner:
        lines.append(_quote(inner))
    return ["\n".join(lines)]


def _pipe_safe(block: dict) -> bool:
    header_rows = block["header_rows"]
    if len(header_rows) != 1 or block["foot_rows"]:
        return False
    width = len(header_rows[0])
    if width == 0:
        return False
    for row in [*header_rows, *block["rows"]]:
        if len(row) != width:
            return False
        for cell in row:
            if "colspan" in cell or "rowspan" in cell or "\n" in cell["text"]:
                return False
    return True


def _pipe_cell(text: str) -> str:
    return escape_md(text).replace("|", "\\|")


def _render_pipe_table(block: dict) -> str:
    header = block["header_rows"][0]
    lines = [
        "| " + " | ".join(_pipe_cell(cell["text"]) for cell in header) + " |",
        "|" + " --- |" * len(header),
    ]
    lines.extend(
        "| " + " | ".join(_pipe_cell(cell["text"]) for cell in row) + " |"
        for row in block["rows"]
    )
    return "\n".join(lines)


def _html_cell(cell: dict, *, header_row: bool) -> str:
    tag = "th" if (header_row or cell.get("header")) else "td"
    attrs = ""
    if "colspan" in cell:
        attrs += f' colspan="{cell["colspan"]}"'
    if "rowspan" in cell:
        attrs += f' rowspan="{cell["rowspan"]}"'
    text = html_escape(cell["text"]).replace("\n", "<br>")
    return f"<{tag}{attrs}>{text}</{tag}>"


def _render_html_table(block: dict) -> str:
    lines = ["<table>"]

    def emit(rows: list[list[dict]], wrapper: str, *, header_row: bool) -> None:
        if not rows:
            return
        lines.append(f"<{wrapper}>")
        for row in rows:
            cells = "".join(_html_cell(cell, header_row=header_row) for cell in row)
            lines.append(f"<tr>{cells}</tr>")
        lines.append(f"</{wrapper}>")

    emit(block["header_rows"], "thead", header_row=True)
    emit(block["rows"], "tbody", header_row=False)
    emit(block["foot_rows"], "tfoot", header_row=False)
    lines.append("</table>")
    return "\n".join(lines)


def _render_table(block: dict) -> list[str]:
    chunks = []
    caption = block.get("caption")
    if caption:
        chunks.append(f"*{escape_md(caption)}*")
    if _pipe_safe(block):
        chunks.append(_render_pipe_table(block))
    else:
        chunks.append(_render_html_table(block))
    return chunks


_RENDERERS = {
    "paragraph": _render_paragraph,
    "definition": _render_definition,
    "text": _render_text,
    "heading": _render_heading,
    "example": _render_example,
    "image": _render_image,
    "math": _render_math,
    "extract": _render_extract,
    "footnote": _render_footnote,
    "note": _render_note,
    "table": _render_table,
}


def render_blocks(blocks: list[dict]) -> list[str]:
    """Render blocks to Markdown chunks (each chunk joined with blank lines)."""
    chunks: list[str] = []
    for block in blocks:
        renderer = _RENDERERS.get(block.get("type"))
        if renderer is None:
            raise BuildError(f"no renderer for block type {block.get('type')!r}")
        chunks.extend(renderer(block))
    return chunks
