"""Minimal deterministic HTML DOM on top of :mod:`html.parser`.

The FAA publishes the AIM and its publication index as well-formed,
DITA-generated HTML5 with explicit closing tags, so a small tree — no
external dependency — is enough to parse it deterministically (plan §20.2:
the parser of record is plain code). Parsing is strict by default: a
mismatched or missing closing tag raises :class:`HTMLStructureError`, so a
truncated response (which can still carry every page marker and clear the
aggregate floors) is rejected instead of being accepted with its tail
silently missing (plan §25.4). Text is kept exactly as the parser delivers
it (entities decoded, whitespace untouched); callers decide how to
normalize.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from html.parser import HTMLParser

VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class HTMLStructureError(ValueError):
    """The document is not well-formed: unbalanced or unclosed elements."""


class Node:
    """An element (``tag`` set) with ``attrs`` and ordered ``children``.

    Children are :class:`Node` elements or plain ``str`` text runs.
    """

    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str | None], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} {self.attrs}>"

    @property
    def classes(self) -> tuple[str, ...]:
        value = self.attrs.get("class") or ""
        return tuple(value.split())

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)

    def elements(self) -> Iterator[Node]:
        """Direct element children (text skipped)."""
        for child in self.children:
            if isinstance(child, Node):
                yield child

    def iter(self) -> Iterator[Node]:
        """This node and every descendant element, in document order."""
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def find_all(self, pred: Callable[[Node], bool]) -> list[Node]:
        return [node for node in self.iter() if pred(node)]

    def find(self, pred: Callable[[Node], bool]) -> Node | None:
        for node in self.iter():
            if pred(node):
                return node
        return None

    def text(self) -> str:
        """Concatenated text of the subtree, exactly as parsed."""
        parts: list[str] = []
        self._collect_text(parts)
        return "".join(parts)

    def _collect_text(self, parts: list[str]) -> None:
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                child._collect_text(parts)


def by_tag(tag: str, cls: str | None = None) -> Callable[[Node], bool]:
    def pred(node: Node) -> bool:
        return node.tag == tag and (cls is None or node.has_class(cls))

    return pred


class _TreeBuilder(HTMLParser):
    def __init__(self, *, strict: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.current = self.root
        self.strict = strict

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, dict(attrs), self.current)
        self.current.children.append(node)
        if tag not in VOID_ELEMENTS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.current.children.append(Node(tag, dict(attrs), self.current))

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        if self.strict:
            if self.current is self.root or self.current.tag != tag:
                line, column = self.getpos()
                open_tag = None if self.current is self.root else self.current.tag
                raise HTMLStructureError(
                    f"unexpected </{tag}> at line {line}, column {column} "
                    f"(open element: {open_tag!r})"
                )
            self.current = self.current.parent  # type: ignore[assignment]
            return
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent  # type: ignore[assignment]
        if node is not self.root:
            self.current = node.parent  # type: ignore[assignment]

    def finish(self) -> None:
        self.close()
        if self.strict and self.current is not self.root:
            open_tags = []
            node = self.current
            while node is not self.root:
                open_tags.append(node.tag)
                node = node.parent  # type: ignore[assignment]
            raise HTMLStructureError(
                f"document ended with unclosed elements {open_tags[::-1]}; "
                "the response may be truncated"
            )

    def handle_data(self, data: str) -> None:
        if data:
            self.current.children.append(data)


def parse_html(text: str, *, strict: bool = True) -> Node:
    """Parse ``text`` into a document root node.

    ``strict`` (the default) requires every element to be closed in order
    and the document to end at the root; ``strict=False`` closes mismatched
    tags by walking up, for callers that only need a best-effort tree.
    """
    builder = _TreeBuilder(strict=strict)
    builder.feed(text)
    builder.finish()
    return builder.root
