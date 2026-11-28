"""How a source is named for a reader: its authors, and where a passage sits.

Both feed the Ask page's source cards. Authors arrive once per source that
listed them, so naming them is deduplication; a passage's place is worked out
from the rendition tree, because node ids mean nothing to a reader.
"""

from __future__ import annotations

from lab_assistant.application.passage_place import place_of
from lab_assistant.application.reconciliation import author_names
from lab_assistant.contracts.parsing import (
    NodeKind,
    RenditionDocument,
    RenditionNode,
    SourceCoordinates,
    TableCell,
)


class TestAuthorNames:
    def test_each_author_once_in_the_first_sources_order(self) -> None:
        pubmed = [
            {"last_name": "Pazuki", "fore_name": "Roozbeh H", "orcid": None},
            {"last_name": "Ledesma-Amaro", "fore_name": "Rodrigo", "orcid": None},
        ]
        crossref = [
            {"family": "Pazuki", "given": "R. H."},
            {"family": "Ledesma-Amaro", "given": "R."},
            {"family": "Smith", "given": "Jane-Ann"},
        ]
        assert author_names([*pubmed, *crossref]) == [
            "Pazuki RH",
            "Ledesma-Amaro R",
            "Smith JA",
        ]

    def test_a_consortium_paper_is_cut_to_the_first_few(self) -> None:
        many = [{"last_name": f"Author{i}", "fore_name": "A"} for i in range(40)]
        assert len(author_names(many)) == 20
        assert author_names(many, limit=3) == ["Author0 A", "Author1 A", "Author2 A"]

    def test_names_without_a_family_name_are_skipped(self) -> None:
        assert author_names([{"fore_name": "Only"}, "Collective Group", 7]) == ["Collective Group"]


def _paragraph(node_id: str, text: str, page: int | None = None) -> RenditionNode:
    return RenditionNode(
        node_id=node_id,
        kind=NodeKind.PARAGRAPH,
        text=text,
        coordinates=SourceCoordinates(kind="pdf", page=page) if page else None,
    )


class TestPlaceOf:
    def test_a_paragraph_is_placed_in_its_sections_and_on_its_page(self) -> None:
        document = RenditionDocument(
            format="pdf",
            nodes=(
                RenditionNode(
                    node_id="sec-1",
                    kind=NodeKind.SECTION,
                    label="Results",
                    children=(
                        _paragraph("p-1", "First.", page=3),
                        _paragraph("p-2", "Second.", page=4),
                        _paragraph("p-3", "Third.", page=4),
                    ),
                ),
                RenditionNode(
                    node_id="sec-2",
                    kind=NodeKind.SECTION,
                    label="Discussion",
                    children=(_paragraph("p-4", "Fourth.", page=4),),
                ),
            ),
        )
        place = place_of(document, ["p-3"])
        assert place["section_path"] == ["Results"]
        assert (place["paragraph"], place["paragraphs"]) == (3, 3)
        assert place["page"] == 4
        # p-2, p-3 and p-4 are on page 4: the cited one is the second of three.
        assert (place["paragraph_on_page"], place["paragraphs_on_page"]) == (2, 3)

    def test_a_table_row_names_its_table_row_and_columns(self) -> None:
        table = RenditionNode(
            node_id="tbl-1",
            kind=NodeKind.TABLE,
            label="Table 2",
            children=(
                RenditionNode(
                    node_id="cap-1", kind=NodeKind.TABLE_CAPTION, text="Lipid titres by strain"
                ),
            ),
            cells=(
                TableCell(node_id="c-0-0", row=0, column=0, is_header=True, text="Strain"),
                TableCell(node_id="c-0-1", row=0, column=1, is_header=True, text="Titre"),
                TableCell(node_id="c-1-0", row=1, column=0, text="YL-A"),
                TableCell(node_id="c-1-1", row=1, column=1, text="4.2 g/L"),
            ),
        )
        document = RenditionDocument(
            format="jats",
            nodes=(
                RenditionNode(
                    node_id="sec-1", kind=NodeKind.SECTION, title="Results", children=(table,)
                ),
            ),
        )
        place = place_of(document, ["c-1-0", "c-1-1"])
        assert place["kind"] == "table"
        assert place["section_path"] == ["Results"]
        assert place["table"] == {
            "label": "Table 2",
            "caption": "Lipid titres by strain",
            "row": 1,
            "columns": ["Strain", "Titre"],
        }
        assert "page" not in place

    def test_an_absent_node_has_no_place(self) -> None:
        assert place_of(RenditionDocument(format="pdf"), ["p-9"]) == {}
