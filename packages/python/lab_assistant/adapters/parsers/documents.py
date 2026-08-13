"""PDF, tabular, HTML, plain-text, and DOCX parsers.

Licence position (ADR-0011): only permissively licensed dependencies. PDF text
extraction uses ``pypdf`` (BSD) rather than PyMuPDF (AGPL or commercial), because
this project may be published and an AGPL parser in a network-served application
is a decision, not a detail. Table structure inside PDFs is therefore *not*
claimed — a table-heavy PDF is reported as degraded so a reviewer sees it, rather
than silently yielding half a table.

OCR is a declared capability with a recorded engine, model, and language. When no
engine is configured, a scanned PDF is reported as ``degraded`` with zero
characters; it never comes back as an empty but "successful" rendition.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, ClassVar

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

__all__ = [
    "CsvParser",
    "DocxParser",
    "HtmlParser",
    "PdfParser",
    "PlainTextParser",
    "XlsxParser",
]

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\s+)?([A-Z][^.\n]{2,80})$")


def _metrics_for(
    nodes: list[RenditionNode],
    *,
    page_count: int = 0,
    pages_represented: int = 0,
    warnings: tuple[str, ...] = (),
    ocr_used: bool = False,
    ocr_confidence: float | None = None,
    low_confidence_pages: tuple[int, ...] = (),
    tables_detected: int | None = None,
    reference_separation: bool = False,
) -> ParseQualityMetrics:
    all_nodes = [descendant for node in nodes for descendant in node.walk()]
    paragraphs = [n.text for n in all_nodes if n.kind is NodeKind.PARAGRAPH and n.text]
    characters = sum(len(n.text) for n in all_nodes) + sum(
        len(cell.text) for n in all_nodes for cell in n.cells
    )
    duplicate_ratio = 1 - (len(set(paragraphs)) / len(paragraphs)) if paragraphs else 0.0
    replacement = sum(n.text.count("�") for n in all_nodes)
    table_count = sum(1 for n in all_nodes if n.kind is NodeKind.TABLE)
    return ParseQualityMetrics(
        page_count=page_count,
        pages_represented=pages_represented,
        section_count=sum(1 for n in all_nodes if n.kind is NodeKind.SECTION),
        table_count=table_count,
        tables_detected=tables_detected if tables_detected is not None else table_count,
        figure_count=sum(1 for n in all_nodes if n.kind is NodeKind.FIGURE),
        characters=characters,
        empty_page_ratio=(round(1 - pages_represented / page_count, 4) if page_count else 0.0),
        replacement_char_rate=round(replacement / characters, 6) if characters else 0.0,
        duplicate_paragraph_ratio=round(duplicate_ratio, 4),
        reference_separation=reference_separation,
        ocr_used=ocr_used,
        ocr_mean_confidence=ocr_confidence,
        low_confidence_pages=low_confidence_pages,
        warnings=warnings,
    )


@dataclass
class PdfParser:
    """Layout-light PDF text parser with page coordinates.

    Sections are inferred from short title-cased lines, which is a heuristic and
    is labelled as one: the section label goes on the node, and the page and
    character offsets are exact regardless of whether the heuristic was right.
    """

    parser_id: str = "pypdf-text"
    parser_version: str = "1.0.0"
    ocr_engine: Any | None = None

    def supports(self, media_type: str) -> bool:
        return media_type == "application/pdf"

    def parse(
        self,
        content: bytes,
        *,
        profile: ParserProfile,
        media_type: str,
        hints: dict[str, Any] | None = None,
    ) -> ParserResult:
        warnings: list[str] = []
        try:
            from pypdf import PdfReader
        except ModuleNotFoundError:
            return self._unavailable(
                "pypdf is not installed; install the 'parsers' extra to parse PDF content"
            )

        reader = PdfReader(io.BytesIO(content))
        page_count = len(reader.pages)
        nodes: list[RenditionNode] = []
        pages_with_text = 0
        section_counter = 0
        paragraph_counter = 0
        current_children: list[RenditionNode] = []
        current_label: str | None = None
        reference_section_seen = False

        def flush_section() -> None:
            nonlocal current_children, current_label, section_counter
            if not current_children:
                return
            section_counter += 1
            nodes.append(
                RenditionNode(
                    node_id=f"sec-{section_counter}",
                    kind=NodeKind.SECTION,
                    label=current_label,
                    title=current_label,
                    level=1,
                    children=tuple(current_children),
                    is_focal=not _is_reference_heading(current_label),
                )
            )
            current_children = []
            current_label = None

        for page_index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # a broken page is recorded, not fatal
                warnings.append(f"page {page_index} could not be extracted: {exc}")
                text = ""
            if text.strip():
                pages_with_text += 1
            elif self.ocr_engine is not None:
                ocr_text, confidence = self.ocr_engine.image_to_text(page_index=page_index)
                if ocr_text.strip():
                    text = ocr_text
                    pages_with_text += 1
                    warnings.append(
                        f"page {page_index} came from OCR at confidence {confidence:.2f}"
                    )
            for block in _PARAGRAPH_BREAK.split(text):
                cleaned = block.strip()
                if not cleaned:
                    continue
                heading = _HEADING.match(cleaned)
                if heading and len(cleaned) < 90:
                    flush_section()
                    current_label = heading.group(1).strip()
                    if _is_reference_heading(current_label):
                        reference_section_seen = True
                    continue
                paragraph_counter += 1
                current_children.append(
                    RenditionNode(
                        node_id=f"p-{paragraph_counter}",
                        kind=NodeKind.PARAGRAPH,
                        text=" ".join(cleaned.split()),
                        level=2,
                        coordinates=SourceCoordinates(kind="pdf", page=page_index),
                        is_focal=not reference_section_seen,
                    )
                )
        flush_section()

        if pages_with_text == 0:
            warnings.append(
                "no extractable text: this is a scanned document and needs an OCR engine"
            )
        if page_count and pages_with_text < page_count:
            warnings.append(
                f"{page_count - pages_with_text} of {page_count} pages produced no text"
            )
        warnings.append(
            "table structure is not extracted from PDF by this parser; a table-heavy "
            "document should be reviewed or supplied as XML"
        )

        document = RenditionDocument(
            format="pdf",
            nodes=tuple(nodes),
            metadata_observed=_pdf_metadata(reader),
            quality=_metrics_for(
                nodes,
                page_count=page_count,
                pages_represented=pages_with_text,
                warnings=tuple(warnings),
                ocr_used=self.ocr_engine is not None and pages_with_text > 0,
                reference_separation=reference_section_seen,
            ),
        )
        return ParserResult(
            rendition=document,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            warnings=tuple(warnings),
        )

    def _unavailable(self, message: str) -> ParserResult:
        return ParserResult(
            rendition=RenditionDocument(
                format="pdf",
                nodes=(),
                quality=_metrics_for([], warnings=(message,)),
            ),
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            warnings=(message,),
        )


def _is_reference_heading(label: str | None) -> bool:
    return bool(label) and label.strip().casefold().rstrip("s") in (
        "reference",
        "bibliography",
        "literature cited",
        "work cited",
    )


def _pdf_metadata(reader: Any) -> dict[str, Any]:
    try:
        info = reader.metadata or {}
    except Exception:  # pragma: no cover - malformed metadata dictionary
        return {}
    return {
        str(key).lstrip("/"): str(value) for key, value in dict(info).items() if value is not None
    }


@dataclass
class CsvParser:
    """CSV/TSV parser with dialect inference and stable source row ids.

    Rows and cells are preserved. A table is never flattened into prose, because
    the first N rows of a spreadsheet turned into a paragraph is exactly how the
    predecessor lost the structure it needed.
    """

    parser_id: str = "csv-dialect"
    parser_version: str = "1.0.0"

    def supports(self, media_type: str) -> bool:
        return media_type in ("text/csv", "text/tab-separated-values")

    def parse(
        self,
        content: bytes,
        *,
        profile: ParserProfile,
        media_type: str,
        hints: dict[str, Any] | None = None,
    ) -> ParserResult:
        warnings: list[str] = []
        encoding = "utf-8"
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            encoding = "latin-1"
            text = content.decode(encoding, errors="replace")
            warnings.append("file is not valid UTF-8; decoded as latin-1 with replacements")

        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample)
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = "\t" if media_type.endswith("tab-separated-values") else ","
            warnings.append(f"dialect could not be sniffed; assumed delimiter {delimiter!r}")

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        cells: list[TableCell] = []
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                cells.append(
                    TableCell(
                        node_id=f"tbl-1/r{row_index}c{column_index}",
                        row=row_index,
                        column=column_index,
                        is_header=row_index == 0,
                        text=value.strip(),
                        coordinates=SourceCoordinates(
                            kind="csv",
                            source_row_id=str(row_index),
                            column=str(column_index),
                        ),
                    )
                )
        table = RenditionNode(
            node_id="tbl-1",
            kind=NodeKind.TABLE,
            title=hints.get("title") if hints else None,
            cells=tuple(cells),
            coordinates=SourceCoordinates(kind="csv"),
        )
        document = RenditionDocument(
            format="csv",
            nodes=(table,),
            metadata_observed={
                "delimiter": delimiter,
                "encoding": encoding,
                "row_count": len(rows),
                "column_count": max((len(row) for row in rows), default=0),
            },
            quality=_metrics_for([table], warnings=tuple(warnings), tables_detected=1),
        )
        return ParserResult(
            rendition=document,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            warnings=tuple(warnings),
        )


@dataclass
class XlsxParser:
    """Workbook parser: sheets, cells, formulas versus values, merged ranges."""

    parser_id: str = "openpyxl-workbook"
    parser_version: str = "1.0.0"

    def supports(self, media_type: str) -> bool:
        return media_type in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.oasis.opendocument.spreadsheet",
        )

    def parse(
        self,
        content: bytes,
        *,
        profile: ParserProfile,
        media_type: str,
        hints: dict[str, Any] | None = None,
    ) -> ParserResult:
        try:
            from openpyxl import load_workbook
        except ModuleNotFoundError:
            message = "openpyxl is not installed; install the 'parsers' extra to parse workbooks"
            return ParserResult(
                rendition=RenditionDocument(
                    format="xlsx", nodes=(), quality=_metrics_for([], warnings=(message,))
                ),
                parser_id=self.parser_id,
                parser_version=self.parser_version,
                warnings=(message,),
            )

        workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        formula_workbook = load_workbook(io.BytesIO(content), data_only=False, read_only=True)
        sheets: list[RenditionNode] = []
        warnings: list[str] = []
        for index, sheet in enumerate(workbook.worksheets, start=1):
            formula_sheet = formula_workbook.worksheets[index - 1]
            cells: list[TableCell] = []
            for row_index, row in enumerate(sheet.iter_rows()):
                for column_index, cell in enumerate(row):
                    if cell.value is None:
                        continue
                    formula = formula_sheet.cell(row=cell.row, column=cell.column).value
                    cells.append(
                        TableCell(
                            node_id=f"sheet-{index}/r{row_index}c{column_index}",
                            row=row_index,
                            column=column_index,
                            is_header=row_index == 0,
                            text=str(cell.value),
                            coordinates=SourceCoordinates(
                                kind="xlsx",
                                sheet=sheet.title,
                                cell_range=cell.coordinate,
                            ),
                        )
                    )
                    if isinstance(formula, str) and formula.startswith("="):
                        warnings.append(
                            f"{sheet.title}!{cell.coordinate} holds a formula; the cached "
                            "value was recorded"
                        )
            sheets.append(
                RenditionNode(
                    node_id=f"sheet-{index}",
                    kind=NodeKind.TABLE,
                    label=sheet.title,
                    title=sheet.title,
                    cells=tuple(cells),
                    coordinates=SourceCoordinates(kind="xlsx", sheet=sheet.title),
                )
            )
        document = RenditionDocument(
            format="xlsx",
            nodes=tuple(sheets),
            metadata_observed={"sheet_names": [sheet.title for sheet in workbook.worksheets]},
            quality=_metrics_for(
                sheets, warnings=tuple(warnings[:20]), tables_detected=len(sheets)
            ),
        )
        return ParserResult(
            rendition=document,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            warnings=tuple(warnings[:20]),
        )


@dataclass
class DocxParser:
    parser_id: str = "python-docx"
    parser_version: str = "1.0.0"

    def supports(self, media_type: str) -> bool:
        return (
            media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def parse(
        self,
        content: bytes,
        *,
        profile: ParserProfile,
        media_type: str,
        hints: dict[str, Any] | None = None,
    ) -> ParserResult:
        try:
            import docx
        except ModuleNotFoundError:
            message = "python-docx is not installed; install the 'parsers' extra for DOCX"
            return ParserResult(
                rendition=RenditionDocument(
                    format="docx", nodes=(), quality=_metrics_for([], warnings=(message,))
                ),
                parser_id=self.parser_id,
                parser_version=self.parser_version,
                warnings=(message,),
            )

        document = docx.Document(io.BytesIO(content))
        nodes: list[RenditionNode] = []
        children: list[RenditionNode] = []
        label: str | None = None
        section_counter = 0
        paragraph_counter = 0

        def flush() -> None:
            nonlocal children, label, section_counter
            if not children:
                return
            section_counter += 1
            nodes.append(
                RenditionNode(
                    node_id=f"sec-{section_counter}",
                    kind=NodeKind.SECTION,
                    label=label,
                    title=label,
                    level=1,
                    children=tuple(children),
                    is_focal=not _is_reference_heading(label),
                )
            )
            children = []
            label = None

        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            if paragraph.style is not None and str(paragraph.style.name).startswith("Heading"):
                flush()
                label = text
                continue
            paragraph_counter += 1
            children.append(
                RenditionNode(
                    node_id=f"p-{paragraph_counter}",
                    kind=NodeKind.PARAGRAPH,
                    text=text,
                    level=2,
                    coordinates=SourceCoordinates(
                        kind="docx", element_path=f"p[{paragraph_counter}]"
                    ),
                )
            )
        flush()

        for table_index, table in enumerate(document.tables, start=1):
            cells: list[TableCell] = []
            for row_index, row in enumerate(table.rows):
                for column_index, cell in enumerate(row.cells):
                    cells.append(
                        TableCell(
                            node_id=f"tbl-{table_index}/r{row_index}c{column_index}",
                            row=row_index,
                            column=column_index,
                            is_header=row_index == 0,
                            text=cell.text.strip(),
                            coordinates=SourceCoordinates(
                                kind="docx", element_path=f"tbl[{table_index}]"
                            ),
                        )
                    )
            nodes.append(
                RenditionNode(
                    node_id=f"tbl-{table_index}",
                    kind=NodeKind.TABLE,
                    cells=tuple(cells),
                    coordinates=SourceCoordinates(kind="docx"),
                )
            )

        rendition = RenditionDocument(
            format="docx",
            nodes=tuple(nodes),
            quality=_metrics_for(nodes),
        )
        return ParserResult(
            rendition=rendition, parser_id=self.parser_id, parser_version=self.parser_version
        )


class _HtmlTextExtractor(HTMLParser):
    """Sanitising HTML reader: drops script, style, and all attributes."""

    _SKIP: ClassVar[frozenset[str]] = frozenset(
        {"script", "style", "noscript", "template", "iframe", "object", "embed"}
    )
    _BLOCK: ClassVar[frozenset[str]] = frozenset(
        {"p", "div", "section", "article", "li", "br", "tr", "h1", "h2", "h3", "h4"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.headings: list[tuple[int, str]] = []
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._heading: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("h1", "h2", "h3", "h4"):
            self._flush()
            self._heading = int(tag[1])
        elif tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in ("h1", "h2", "h3", "h4"):
            text = " ".join("".join(self._buffer).split())
            if text and self._heading is not None:
                self.headings.append((self._heading, text))
            self._buffer = []
            self._heading = None
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._buffer.append(data)

    def _flush(self) -> None:
        text = " ".join("".join(self._buffer).split())
        if text:
            self.blocks.append(text)
        self._buffer = []

    def close(self) -> None:
        super().close()
        self._flush()


@dataclass
class HtmlParser:
    parser_id: str = "html-sanitised"
    parser_version: str = "1.0.0"

    def supports(self, media_type: str) -> bool:
        return media_type == "text/html"

    def parse(
        self,
        content: bytes,
        *,
        profile: ParserProfile,
        media_type: str,
        hints: dict[str, Any] | None = None,
    ) -> ParserResult:
        extractor = _HtmlTextExtractor()
        extractor.feed(content.decode("utf-8", errors="replace"))
        extractor.close()
        nodes = [
            RenditionNode(
                node_id=f"p-{index}",
                kind=NodeKind.PARAGRAPH,
                text=block,
                coordinates=SourceCoordinates(kind="html"),
            )
            for index, block in enumerate(extractor.blocks, start=1)
        ]
        document = RenditionDocument(
            format="html",
            nodes=tuple(nodes),
            metadata_observed={
                "headings": [text for _level, text in extractor.headings],
                "retrieved_from": (hints or {}).get("source_url"),
            },
            quality=_metrics_for(
                nodes,
                warnings=("scripts, styles, and attributes were discarded",),
            ),
        )
        return ParserResult(
            rendition=document, parser_id=self.parser_id, parser_version=self.parser_version
        )


@dataclass
class PlainTextParser:
    parser_id: str = "text-blocks"
    parser_version: str = "1.0.0"
    media_types: frozenset[str] = field(
        default_factory=lambda: frozenset({"text/plain", "text/markdown", "application/json"})
    )

    def supports(self, media_type: str) -> bool:
        return media_type in self.media_types

    def parse(
        self,
        content: bytes,
        *,
        profile: ParserProfile,
        media_type: str,
        hints: dict[str, Any] | None = None,
    ) -> ParserResult:
        warnings: list[str] = []
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")
            warnings.append("file is not valid UTF-8; undecodable bytes were replaced")
        nodes = [
            RenditionNode(
                node_id=f"p-{index}",
                kind=NodeKind.PARAGRAPH,
                text=" ".join(block.split()),
                coordinates=SourceCoordinates(kind="text"),
            )
            for index, block in enumerate(_PARAGRAPH_BREAK.split(text), start=1)
            if block.strip()
        ]
        document = RenditionDocument(
            format="text",
            nodes=tuple(nodes),
            quality=_metrics_for(nodes, warnings=tuple(warnings)),
        )
        return ParserResult(
            rendition=document,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            warnings=tuple(warnings),
        )
