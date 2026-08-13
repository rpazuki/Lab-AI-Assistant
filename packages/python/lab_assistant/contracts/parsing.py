"""Parser contract and the rendition node model.

The rendition is the parsed boundary. Everything downstream — evidence spans,
claims, chunks, citations — addresses rendition nodes, never raw asset bytes and
never a filesystem path.

Node identifiers are structural and deterministic: parsing the same asset with
the same parser version yields the same ids, which is what lets a chunk digest
and an evidence span survive re-serialisation.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "DocumentParser",
    "NodeKind",
    "ParseQualityMetrics",
    "ParserProfile",
    "ParserResult",
    "RenditionDocument",
    "RenditionNode",
    "SourceCoordinates",
    "TableCell",
]


class NodeKind(StrEnum):
    DOCUMENT = "document"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    LIST = "list"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_CAPTION = "table_caption"
    TABLE_FOOTNOTE = "table_footnote"
    FIGURE = "figure"
    FIGURE_CAPTION = "figure_caption"
    REFERENCE_LIST = "reference_list"
    REFERENCE = "reference"
    ABSTRACT = "abstract"
    TITLE = "title"
    SHEET = "sheet"
    ROW = "row"
    CELL = "cell"
    PAGE = "page"


class SourceCoordinates(BaseModel):
    """Where a node came from inside the asset.

    Which fields are populated depends on the source format. A PDF node has a
    page and a bounding box; a JATS node has an element path; a workbook cell
    has a sheet and an A1 reference.
    """

    model_config = {"frozen": True}

    kind: str
    element_path: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    sheet: str | None = None
    cell_range: str | None = None
    source_row_id: str | None = None
    column: str | None = None
    #: OCR provenance, per page or block (lifecycle §8).
    ocr_engine: str | None = None
    ocr_model: str | None = None
    ocr_language: str | None = None
    ocr_confidence: float | None = None


class TableCell(BaseModel):
    model_config = {"frozen": True}

    node_id: str
    row: int
    column: int
    row_span: int = 1
    column_span: int = 1
    is_header: bool = False
    text: str = ""
    coordinates: SourceCoordinates | None = None


class RenditionNode(BaseModel):
    """One addressable node in the rendition tree."""

    model_config = {"frozen": True}

    node_id: str
    kind: NodeKind
    text: str = ""
    label: str | None = None
    title: str | None = None
    level: int = 0
    children: tuple[RenditionNode, ...] = ()
    cells: tuple[TableCell, ...] = ()
    coordinates: SourceCoordinates | None = None
    #: False for reference lists and other non-focal content. Evidence for a
    #: claim about the focal study must not come from a cited study.
    is_focal: bool = True
    attributes: dict[str, Any] = Field(default_factory=dict)

    def walk(self) -> Iterator[RenditionNode]:
        yield self
        for child in self.children:
            yield from child.walk()

    def cell_by_id(self, node_id: str) -> TableCell | None:
        for cell in self.cells:
            if cell.node_id == node_id:
                return cell
        return None


class ParseQualityMetrics(BaseModel):
    """Metrics the parse gate reads (quality §2.4)."""

    model_config = {"frozen": True}

    page_count: int = 0
    pages_represented: int = 0
    section_count: int = 0
    table_count: int = 0
    tables_detected: int = 0
    figure_count: int = 0
    characters: int = 0
    empty_page_ratio: float = 0.0
    replacement_char_rate: float = 0.0
    duplicate_paragraph_ratio: float = 0.0
    reference_separation: bool = False
    ocr_used: bool = False
    ocr_mean_confidence: float | None = None
    low_confidence_pages: tuple[int, ...] = ()
    embedded_identifier_match: bool | None = None
    warnings: tuple[str, ...] = ()
    dropped_object_count: int = 0

    @property
    def page_coverage(self) -> float:
        if self.page_count == 0:
            return 1.0
        return self.pages_represented / self.page_count


class RenditionDocument(BaseModel):
    """The rendition payload stored as an immutable object."""

    model_config = {"frozen": True}

    schema_version: str = "1.0"
    format: str
    language: str | None = None
    nodes: tuple[RenditionNode, ...] = ()
    metadata_observed: dict[str, Any] = Field(default_factory=dict)
    quality: ParseQualityMetrics = ParseQualityMetrics()

    def walk(self) -> Iterator[RenditionNode]:
        for node in self.nodes:
            yield from node.walk()

    def node_by_id(self, node_id: str) -> RenditionNode | None:
        for node in self.walk():
            if node.node_id == node_id:
                return node
        return None

    def cell_by_id(self, node_id: str) -> TableCell | None:
        for node in self.walk():
            cell = node.cell_by_id(node_id)
            if cell is not None:
                return cell
        return None

    def text_of(self, node_id: str) -> str | None:
        """Text addressed by ``node_id``, whether it is a node or a table cell."""
        node = self.node_by_id(node_id)
        if node is not None:
            return node.text
        cell = self.cell_by_id(node_id)
        return cell.text if cell is not None else None

    def focal_node_ids(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.walk() if node.is_focal)


class ParserProfile(BaseModel):
    """Versioned parser configuration and its quality thresholds."""

    model_config = {"frozen": True}

    profile_id: str
    profile_version: str
    parser_id: str
    parser_version: str
    media_types: frozenset[str]
    ocr_enabled: bool = False
    ocr_engine: str | None = None
    ocr_language: str | None = None
    normalisation_version: str = "1"
    #: Gate thresholds. A rendition below any of these is degraded and needs a
    #: reviewer decision before it becomes extraction-eligible.
    min_page_coverage: float = 1.0
    max_duplicate_paragraph_ratio: float = 0.05
    max_replacement_char_rate: float = 0.01
    min_characters: int = 1
    require_reference_separation: bool = False
    #: Resource limits for the sandboxed parse worker.
    max_bytes: int = 64 * 1024 * 1024
    timeout_seconds: int = 120


class ParserResult(BaseModel):
    model_config = {"frozen": True}

    rendition: RenditionDocument
    parser_id: str
    parser_version: str
    warnings: tuple[str, ...] = ()
    log: str | None = None


@runtime_checkable
class DocumentParser(Protocol):
    parser_id: str
    parser_version: str

    def supports(self, media_type: str) -> bool: ...

    def parse(
        self,
        content: bytes,
        *,
        profile: ParserProfile,
        media_type: str,
        hints: dict[str, Any] | None = None,
    ) -> ParserResult: ...


def iter_table_cells(nodes: Sequence[RenditionNode]) -> Iterator[TableCell]:
    for node in nodes:
        for descendant in node.walk():
            yield from descendant.cells
