"""Parser gold fixtures.

The JATS cases exist because the predecessor duplicated nested section text and
let ``.//`` reach into the record's own reference list. Both are named fixtures
here so a regression is a failing test, not a subtly wrong rendition.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from lab_assistant.adapters.parsers.registry import default_parser_registry
from lab_assistant.application.registries import UnknownParser
from lab_assistant.contracts.parsing import NodeKind
from lab_assistant.domain.renditions import build_evidence_span, rendition_quality_blockers
from lab_assistant.testing import sample_jats

WORKSPACE = uuid4()


@pytest.fixture
def registry():
    return default_parser_registry()


class TestJatsParser:
    def _parse(self, registry, content: bytes):
        parser = registry.for_media_type("application/xml")
        profile = registry.profile_for("application/xml")
        return parser.parse(content, profile=profile, media_type="application/xml"), profile

    def test_nested_sections_do_not_duplicate_body_text(self, registry) -> None:
        result, _ = self._parse(registry, sample_jats())
        paragraphs = [
            node.text
            for node in result.rendition.walk()
            if node.kind is NodeKind.PARAGRAPH and node.text
        ]
        assert len(paragraphs) == len(set(paragraphs))
        assert result.rendition.quality.duplicate_paragraph_ratio == 0.0
        # A section carries no text of its own; its paragraphs do.
        sections = [n for n in result.rendition.walk() if n.kind is NodeKind.SECTION]
        assert sections and all(section.text == "" for section in sections)

    def test_nested_section_is_a_child_not_a_sibling(self, registry) -> None:
        result, _ = self._parse(registry, sample_jats())
        top_sections = [n for n in result.rendition.nodes if n.kind is NodeKind.SECTION]
        introduction = next(s for s in top_sections if s.label == "Introduction")
        assert any(child.kind is NodeKind.SECTION for child in introduction.children)

    def test_reference_list_is_present_but_not_focal(self, registry) -> None:
        result, _ = self._parse(registry, sample_jats())
        references = [n for n in result.rendition.walk() if n.kind is NodeKind.REFERENCE]
        assert references
        assert all(not node.is_focal for node in references)
        assert result.rendition.quality.reference_separation

    def test_a_number_from_the_reference_list_is_not_focal_evidence(self, registry) -> None:
        """The `.//` defect: a cited study's number must not become evidence."""
        result, _ = self._parse(registry, sample_jats())
        reference = next(n for n in result.rendition.walk() if n.kind is NodeKind.REFERENCE)
        assert "9.99" in reference.text
        span, verification = build_evidence_span(
            span_id=uuid4(),
            workspace_id=WORKSPACE,
            rendition_id=uuid4(),
            rendition=result.rendition,
            node_id=reference.node_id,
            quote="Unrelated lipid yield of 9.99 g/g",
        )
        # The span verifies against the node, and is marked non-focal so claim
        # validation rejects it for a claim about the focal study.
        assert verification.verified
        assert span is not None and span.is_focal is False

    def test_table_cells_keep_coordinates_and_stable_ids(self, registry) -> None:
        result, _ = self._parse(registry, sample_jats())
        table = next(n for n in result.rendition.walk() if n.kind is NodeKind.TABLE)
        assert table.cells
        headers = [cell.text for cell in table.cells if cell.is_header]
        assert "Strain" in headers
        yields = [cell.text for cell in table.cells if cell.text == "0.42"]
        assert yields
        assert all(cell.node_id.startswith(table.node_id) for cell in table.cells)
        assert any(child.kind is NodeKind.TABLE_CAPTION for child in table.children)
        assert any(child.kind is NodeKind.TABLE_FOOTNOTE for child in table.children)

    def test_a_cell_is_addressable_as_evidence(self, registry) -> None:
        result, _ = self._parse(registry, sample_jats())
        table = next(n for n in result.rendition.walk() if n.kind is NodeKind.TABLE)
        cell = next(c for c in table.cells if c.text == "0.42")
        span, verification = build_evidence_span(
            span_id=uuid4(),
            workspace_id=WORKSPACE,
            rendition_id=uuid4(),
            rendition=result.rendition,
            node_id=cell.node_id,
            quote="0.42",
        )
        assert verification.verified
        assert span is not None
        assert span.locator["row"] == cell.row
        assert span.locator["column"] == cell.column

    def test_metadata_and_licence_are_observed(self, registry) -> None:
        result, _ = self._parse(registry, sample_jats())
        metadata = result.rendition.metadata_observed
        assert metadata["identifiers"]["doi"] == "10.1234/example"
        assert metadata["licence"] == "cc-by-4.0"
        assert metadata["journal"] == "Journal of Applied Microbiology"

    def test_metadata_only_record_is_degraded_not_silently_empty(self, registry) -> None:
        body_less = b"""<?xml version="1.0"?><article><front>
        <article-meta><title-group><article-title>T</article-title></title-group></article-meta>
        </front></article>"""
        result, profile = self._parse(registry, body_less)
        blockers = rendition_quality_blockers(result.rendition.quality, profile)
        assert blockers
        assert any("characters" in b for b in blockers)
        assert any("no <body>" in warning for warning in result.warnings)


class TestTabularParsers:
    def test_csv_rows_and_cells_survive(self, registry) -> None:
        content = b"strain,yield\nYL-A,0.42\nYL-B,0.28\n"
        parser = registry.for_media_type("text/csv")
        result = parser.parse(
            content, profile=registry.profile_for("text/csv"), media_type="text/csv"
        )
        table = result.rendition.nodes[0]
        assert table.kind is NodeKind.TABLE
        assert len(table.cells) == 6
        assert result.rendition.metadata_observed["delimiter"] == ","
        header = [cell.text for cell in table.cells if cell.is_header]
        assert header == ["strain", "yield"]
        # Stable source row ids, not positions in a flattened string.
        assert all(cell.coordinates.source_row_id is not None for cell in table.cells)

    def test_tsv_is_detected_and_not_split_on_commas(self, registry) -> None:
        content = b"strain\tnote\nYL-A\tgrown at 30 C, then harvested\n"
        parser = registry.for_media_type("text/tab-separated-values")
        result = parser.parse(
            content,
            profile=registry.profile_for("text/tab-separated-values"),
            media_type="text/tab-separated-values",
        )
        cells = result.rendition.nodes[0].cells
        assert any("30 C, then harvested" in cell.text for cell in cells)


class TestTextAndHtmlParsers:
    def test_html_scripts_are_dropped(self, registry) -> None:
        content = b"<html><body><script>steal()</script><p>Yield was 0.42 g/g.</p></body></html>"
        parser = registry.for_media_type("text/html")
        result = parser.parse(
            content, profile=registry.profile_for("text/html"), media_type="text/html"
        )
        text = " ".join(node.text for node in result.rendition.walk())
        assert "0.42" in text
        assert "steal" not in text

    def test_plain_text_paragraph_blocks(self, registry) -> None:
        parser = registry.for_media_type("text/plain")
        result = parser.parse(
            b"First block.\n\nSecond block.",
            profile=registry.profile_for("text/plain"),
            media_type="text/plain",
        )
        assert [n.text for n in result.rendition.nodes] == ["First block.", "Second block."]

    def test_undecodable_bytes_are_reported(self, registry) -> None:
        parser = registry.for_media_type("text/plain")
        result = parser.parse(
            b"caf\xe9 latte",
            profile=registry.profile_for("text/plain"),
            media_type="text/plain",
        )
        assert any("not valid UTF-8" in warning for warning in result.warnings)


class TestRegistrySelection:
    def test_selection_is_by_detected_media_type(self, registry) -> None:
        assert registry.for_media_type("application/xml").parser_id == "jats-etree"
        assert registry.for_media_type("application/pdf").parser_id == "pypdf-text"
        assert registry.for_media_type("text/csv").parser_id == "csv-dialect"

    def test_unsupported_type_raises_rather_than_falling_back(self, registry) -> None:
        with pytest.raises(UnknownParser, match="no parser declares support"):
            registry.for_media_type("application/x-tar")

    def test_profiles_record_the_normalisation_version(self, registry) -> None:
        from lab_assistant.domain.normalisation import SPAN_NORMALISATION_VERSION

        profile = registry.profile_for("application/pdf")
        assert profile.normalisation_version == SPAN_NORMALISATION_VERSION

    def test_pdf_profile_demands_full_page_coverage(self, registry) -> None:
        # A scanned page with no OCR yields no text, so the rendition is degraded
        # and a reviewer sees it rather than it passing as complete.
        assert registry.profile_for("application/pdf").min_page_coverage == 1.0

    def test_aliases_share_a_profile(self, registry) -> None:
        assert registry.profile_for("text/xml") is registry.profile_for("application/xml")
