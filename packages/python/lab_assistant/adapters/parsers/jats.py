"""JATS/NLM XML parser.

Two defects in the predecessor's JATS handling are fixed here by construction:

* **nested sections no longer duplicate body text.** A section's own text is the
  text of its direct children only; a paragraph belongs to exactly one node, so
  the same sentence cannot appear at two levels of the tree.
* **``.//`` no longer reaches into the record's own reference list.** Traversal is
  explicit and child-wise, and the reference list is marked ``is_focal=False`` so
  it can never supply evidence for a claim about the focal study.

Implemented on the standard library's ``xml.etree`` with namespace-aware tag
matching. It is deliberately not ``lxml``: the ruff configuration bans that
import so the licence position stays simple (see ADR-0011).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from lab_assistant.contracts.parsing import (
    NodeKind,
    ParseQualityMetrics,
    ParserProfile,
    ParserResult,
    RenditionDocument,
    RenditionNode,
    SourceCoordinates,
    TableCell,
)

__all__ = ["JatsParser"]

_REFERENCE_TAGS = {"ref-list", "back"}
_WHITESPACE = re.compile(r"\s+")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _block_text(element: ElementTree.Element) -> str:
    """All text under ``element``, including its block children.

    Used for captions and footnotes, whose text lives inside <p>.
    """
    parts = [_text_of(element)]
    parts.extend(_text_of(child) for child in element.iter() if _localname(child.tag) == "p")
    return _WHITESPACE.sub(" ", " ".join(part for part in parts if part)).strip()


def _text_of(element: ElementTree.Element) -> str:
    """Inline text of one element, excluding block children.

    Inline formatting (italic, bold, sup, sub, xref) is flattened into the run
    because that is how a reader sees it; block children are separate nodes.
    """
    block_tags = {"sec", "p", "table-wrap", "fig", "list", "ref-list"}
    parts: list[str] = [element.text or ""]
    for child in element:
        if _localname(child.tag) in block_tags:
            parts.append(child.tail or "")
            continue
        parts.append(_text_of(child))
        parts.append(child.tail or "")
    return _WHITESPACE.sub(" ", "".join(parts)).strip()


@dataclass
class JatsParser:
    parser_id: str = "jats-etree"
    parser_version: str = "1.0.0"

    def supports(self, media_type: str) -> bool:
        return media_type in ("application/xml", "text/xml", "application/jats+xml")

    def parse(
        self,
        content: bytes,
        *,
        profile: ParserProfile,
        media_type: str,
        hints: dict[str, Any] | None = None,
    ) -> ParserResult:
        root = ElementTree.fromstring(content)
        warnings: list[str] = []
        nodes: list[RenditionNode] = []
        counters = {"sec": 0, "p": 0, "tbl": 0, "fig": 0, "ref": 0}

        front = self._find(root, "front")
        metadata = self._metadata(front) if front is not None else {}

        title = metadata.get("title")
        if title:
            nodes.append(
                RenditionNode(
                    node_id="title",
                    kind=NodeKind.TITLE,
                    text=title,
                    coordinates=SourceCoordinates(kind="jats", element_path="front/article-title"),
                )
            )

        abstract = self._find(front, "abstract") if front is not None else None
        if abstract is not None:
            paragraphs = [_text_of(child) for child in abstract if _localname(child.tag) == "p"]
            text = " ".join(part for part in paragraphs if part) or _text_of(abstract)
            nodes.append(
                RenditionNode(
                    node_id="abstract",
                    kind=NodeKind.ABSTRACT,
                    text=text,
                    label="Abstract",
                    coordinates=SourceCoordinates(kind="jats", element_path="front/abstract"),
                )
            )

        body = self._find(root, "body")
        if body is not None:
            for child in body:
                node = self._element_node(child, counters, path="body", level=1, focal=True)
                if node is not None:
                    nodes.append(node)
        else:
            warnings.append("no <body> element: this is metadata or an abstract-only record")

        back = self._find(root, "back")
        if back is not None:
            reference_list = self._reference_list(back, counters)
            if reference_list is not None:
                nodes.append(reference_list)

        document = RenditionDocument(
            format="jats",
            language=metadata.get("language"),
            nodes=tuple(nodes),
            metadata_observed=metadata,
            quality=self._metrics(nodes, warnings=warnings, has_references=back is not None),
        )
        return ParserResult(
            rendition=document,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            warnings=tuple(warnings),
        )

    def _element_node(
        self,
        element: ElementTree.Element,
        counters: dict[str, int],
        *,
        path: str,
        level: int,
        focal: bool,
    ) -> RenditionNode | None:
        tag = _localname(element.tag)
        if tag in _REFERENCE_TAGS:
            return None
        if tag == "sec":
            counters["sec"] += 1
            node_id = f"sec-{counters['sec']}"
            section_title = None
            children: list[RenditionNode] = []
            for child in element:
                child_tag = _localname(child.tag)
                if child_tag == "title":
                    section_title = _text_of(child)
                    continue
                built = self._element_node(
                    child, counters, path=f"{path}/{tag}[{node_id}]", level=level + 1, focal=focal
                )
                if built is not None:
                    children.append(built)
            return RenditionNode(
                node_id=node_id,
                kind=NodeKind.SECTION,
                #: A section carries no text of its own: its paragraphs do. This
                #: is what prevents the duplicate-text defect.
                text="",
                label=section_title,
                title=section_title,
                level=level,
                children=tuple(children),
                coordinates=SourceCoordinates(kind="jats", element_path=f"{path}/sec"),
                is_focal=focal,
            )
        if tag == "p":
            counters["p"] += 1
            text = _text_of(element)
            if not text:
                return None
            return RenditionNode(
                node_id=f"p-{counters['p']}",
                kind=NodeKind.PARAGRAPH,
                text=text,
                level=level,
                coordinates=SourceCoordinates(kind="jats", element_path=f"{path}/p"),
                is_focal=focal,
            )
        if tag in ("table-wrap", "table"):
            return self._table_node(element, counters, path=path, level=level, focal=focal)
        if tag == "fig":
            counters["fig"] += 1
            caption = self._caption(element)
            return RenditionNode(
                node_id=f"fig-{counters['fig']}",
                kind=NodeKind.FIGURE,
                text=caption,
                label=element.get("id"),
                level=level,
                children=(
                    (
                        RenditionNode(
                            node_id=f"fig-{counters['fig']}-caption",
                            kind=NodeKind.FIGURE_CAPTION,
                            text=caption,
                            is_focal=focal,
                        ),
                    )
                    if caption
                    else ()
                ),
                coordinates=SourceCoordinates(kind="jats", element_path=f"{path}/fig"),
                is_focal=focal,
            )
        if tag == "list":
            items: list[RenditionNode] = []
            for child in element:
                if _localname(child.tag) != "list-item":
                    continue
                counters["p"] += 1
                text = _text_of(child)
                if not text:
                    continue
                items.append(
                    RenditionNode(
                        node_id=f"li-{counters['p']}",
                        kind=NodeKind.LIST_ITEM,
                        text=text,
                        level=level + 1,
                        is_focal=focal,
                    )
                )
            if not items:
                return None
            return RenditionNode(
                node_id=f"list-{counters['p']}",
                kind=NodeKind.LIST,
                children=tuple(items),
                level=level,
                coordinates=SourceCoordinates(kind="jats", element_path=f"{path}/list"),
                is_focal=focal,
            )
        return None

    def _table_node(
        self,
        element: ElementTree.Element,
        counters: dict[str, int],
        *,
        path: str,
        level: int,
        focal: bool,
    ) -> RenditionNode:
        counters["tbl"] += 1
        table_id = f"tbl-{counters['tbl']}"
        caption = self._caption(element)
        # An Element's truth value is deprecated and misleading; test for None.
        found_table = self._find(element, "table")
        table = found_table if found_table is not None else element
        cells: list[TableCell] = []
        row_index = 0
        for section in table:
            section_tag = _localname(section.tag)
            if section_tag not in ("thead", "tbody", "tfoot", "tr"):
                continue
            rows = [section] if section_tag == "tr" else list(section)
            for row in rows:
                if _localname(row.tag) != "tr":
                    continue
                column_index = 0
                for cell in row:
                    cell_tag = _localname(cell.tag)
                    if cell_tag not in ("td", "th"):
                        continue
                    cells.append(
                        TableCell(
                            node_id=f"{table_id}/r{row_index}c{column_index}",
                            row=row_index,
                            column=column_index,
                            row_span=int(cell.get("rowspan", "1") or 1),
                            column_span=int(cell.get("colspan", "1") or 1),
                            is_header=cell_tag == "th" or section_tag == "thead",
                            text=_text_of(cell),
                            coordinates=SourceCoordinates(
                                kind="jats",
                                element_path=f"{path}/table-wrap[{table_id}]/tr[{row_index}]",
                            ),
                        )
                    )
                    column_index += 1
                row_index += 1

        footnotes = [
            RenditionNode(
                node_id=f"{table_id}-fn{index}",
                kind=NodeKind.TABLE_FOOTNOTE,
                text=_block_text(footnote),
                is_focal=focal,
            )
            for index, footnote in enumerate(
                child for child in element if _localname(child.tag) == "table-wrap-foot"
            )
        ]
        children: list[RenditionNode] = []
        if caption:
            children.append(
                RenditionNode(
                    node_id=f"{table_id}-caption",
                    kind=NodeKind.TABLE_CAPTION,
                    text=caption,
                    is_focal=focal,
                )
            )
        children.extend(footnotes)
        return RenditionNode(
            node_id=table_id,
            kind=NodeKind.TABLE,
            text="",
            label=element.get("id"),
            title=caption or None,
            level=level,
            children=tuple(children),
            cells=tuple(cells),
            coordinates=SourceCoordinates(kind="jats", element_path=f"{path}/table-wrap"),
            is_focal=focal,
        )

    def _reference_list(
        self, back: ElementTree.Element, counters: dict[str, int]
    ) -> RenditionNode | None:
        ref_list = self._find(back, "ref-list")
        if ref_list is None:
            return None
        references: list[RenditionNode] = []
        for ref in ref_list:
            if _localname(ref.tag) != "ref":
                continue
            counters["ref"] += 1
            references.append(
                RenditionNode(
                    node_id=f"ref-{counters['ref']}",
                    kind=NodeKind.REFERENCE,
                    text=_text_of(ref),
                    #: Not focal: a claim about the focal study cannot cite this.
                    is_focal=False,
                )
            )
        if not references:
            return None
        return RenditionNode(
            node_id="ref-list",
            kind=NodeKind.REFERENCE_LIST,
            label="References",
            children=tuple(references),
            coordinates=SourceCoordinates(kind="jats", element_path="back/ref-list"),
            is_focal=False,
        )

    @staticmethod
    def _find(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
        if element is None:
            return None
        for child in element.iter():
            if _localname(child.tag) == name:
                return child
        return None

    def _caption(self, element: ElementTree.Element) -> str:
        """Caption text, including its block children.

        A JATS caption wraps its text in <p>, which the inline-only reader skips
        deliberately; a caption is the one place we do want the block text.
        """
        caption = self._find(element, "caption")
        if caption is None:
            return ""
        return _block_text(caption)

    def _metadata(self, front: ElementTree.Element) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        title = self._find(front, "article-title")
        if title is not None:
            metadata["title"] = _text_of(title)
        journal = self._find(front, "journal-title")
        if journal is not None:
            metadata["journal"] = _text_of(journal)
        for element in front.iter():
            if _localname(element.tag) != "article-id":
                continue
            id_type = element.get("pub-id-type")
            if id_type and element.text:
                metadata.setdefault("identifiers", {})[id_type] = element.text.strip()
        year = self._find(front, "year")
        if year is not None and year.text:
            metadata["year"] = year.text.strip()
        licence = self._find(front, "license")
        if licence is not None:
            metadata["licence"] = licence.get("{http://www.w3.org/1999/xlink}href") or _text_of(
                licence
            )
        return metadata

    @staticmethod
    def _metrics(
        nodes: list[RenditionNode], *, warnings: list[str], has_references: bool
    ) -> ParseQualityMetrics:
        all_nodes = [descendant for node in nodes for descendant in node.walk()]
        paragraphs = [n.text for n in all_nodes if n.kind is NodeKind.PARAGRAPH and n.text]
        characters = sum(len(n.text) for n in all_nodes)
        duplicate_ratio = 0.0
        if paragraphs:
            duplicate_ratio = 1 - (len(set(paragraphs)) / len(paragraphs))
        replacement = sum(n.text.count("�") for n in all_nodes)
        return ParseQualityMetrics(
            page_count=0,
            pages_represented=0,
            section_count=sum(1 for n in all_nodes if n.kind is NodeKind.SECTION),
            table_count=sum(1 for n in all_nodes if n.kind is NodeKind.TABLE),
            tables_detected=sum(1 for n in all_nodes if n.kind is NodeKind.TABLE),
            figure_count=sum(1 for n in all_nodes if n.kind is NodeKind.FIGURE),
            characters=characters,
            duplicate_paragraph_ratio=round(duplicate_ratio, 4),
            replacement_char_rate=round(replacement / characters, 6) if characters else 0.0,
            reference_separation=has_references,
            warnings=tuple(warnings),
        )
