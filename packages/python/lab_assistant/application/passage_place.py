"""Where a cited passage sits in its paper, in words a reader can follow.

A citation points at rendition nodes. Their ids ("p-14") mean nothing to a
reader, so this works out what does: the sections the passage is under, which
paragraph of its section it is, and, for a PDF, the page and which paragraph of
that page. Parsers give a PDF paragraph its page but no position on it, so the
place on the page is "the 2nd of 5 paragraphs on page 4", not a measurement.
"""

from __future__ import annotations

from typing import Any

from lab_assistant.contracts.parsing import NodeKind, RenditionDocument, RenditionNode

__all__ = ["place_of"]

#: Nodes a reader counts as paragraphs when finding their way through a paper.
_TEXT_KINDS = frozenset(
    {
        NodeKind.PARAGRAPH,
        NodeKind.LIST_ITEM,
        NodeKind.ABSTRACT,
        NodeKind.FIGURE_CAPTION,
        NodeKind.TABLE_CAPTION,
    }
)
#: Nodes that name the part of the paper below them.
_HEADING_KINDS = frozenset({NodeKind.SECTION, NodeKind.ABSTRACT, NodeKind.SHEET})


def _walk(document: RenditionDocument) -> list[tuple[RenditionNode, list[RenditionNode]]]:
    """Every node with its ancestors, in reading order."""
    ordered: list[tuple[RenditionNode, list[RenditionNode]]] = []

    def visit(node: RenditionNode, ancestry: list[RenditionNode]) -> None:
        ordered.append((node, ancestry))
        for child in node.children:
            visit(child, [*ancestry, node])

    for top in document.nodes:
        visit(top, [])
    return ordered


def _page(node: RenditionNode) -> int | None:
    return node.coordinates.page if node.coordinates is not None else None


def place_of(document: RenditionDocument, node_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """The place of the first of ``node_ids`` found, or ``{}`` when none is."""
    wanted = set(node_ids)
    ordered = _walk(document)
    hit: tuple[RenditionNode, list[RenditionNode]] | None = None
    for node, ancestry in ordered:
        if node.node_id in wanted or any(cell.node_id in wanted for cell in node.cells):
            hit = (node, ancestry)
            break
    if hit is None:
        return {}
    node, ancestry = hit

    place: dict[str, Any] = {
        "kind": node.kind.value,
        "section_path": [
            str(parent.label or parent.title)
            for parent in ancestry
            if parent.kind in _HEADING_KINDS and (parent.label or parent.title)
        ],
    }

    if node.kind is NodeKind.TABLE:
        cited = [cell for cell in node.cells if cell.node_id in wanted]
        headers = {cell.column: cell.text for cell in node.cells if cell.is_header}
        caption = next(
            (child.text for child in node.children if child.kind is NodeKind.TABLE_CAPTION),
            None,
        )
        place["table"] = {
            "label": node.label or node.title,
            "caption": caption,
            "row": min((cell.row for cell in cited), default=None),
            "columns": [headers.get(cell.column) or "" for cell in cited],
        }
    else:
        siblings = [
            child
            for child in (ancestry[-1].children if ancestry else document.nodes)
            if child.kind in _TEXT_KINDS
        ]
        position = next(
            (index for index, sibling in enumerate(siblings) if sibling.node_id == node.node_id),
            None,
        )
        if position is not None:
            place["paragraph"] = position + 1
            place["paragraphs"] = len(siblings)

    page = _page(node)
    if page is not None:
        on_page = [
            other
            for other, _ in ordered
            if _page(other) == page and (other.kind in _TEXT_KINDS or other.kind is NodeKind.TABLE)
        ]
        place["page"] = page
        position = next(
            (index for index, other in enumerate(on_page) if other.node_id == node.node_id), None
        )
        if position is not None:
            place["paragraph_on_page"] = position + 1
            place["paragraphs_on_page"] = len(on_page)
    return place
