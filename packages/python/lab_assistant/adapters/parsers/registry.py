"""Parser registry and default profiles.

Selection is by detected media type. An unsupported type raises
:class:`UnknownParser` rather than falling through to a text parser that would
turn a PDF into mojibake and call it a rendition.

Profile thresholds are per media type because achievable quality differs by
format. They are set from what these parsers actually deliver (spike S1's
purpose): JATS can be held to full structural coverage, a PDF cannot be held to
table fidelity by a text-only extractor, and a scanned page with no OCR engine is
degraded by definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lab_assistant.adapters.parsers.documents import (
    CsvParser,
    DocxParser,
    HtmlParser,
    PdfParser,
    PlainTextParser,
    XlsxParser,
)
from lab_assistant.adapters.parsers.jats import JatsParser
from lab_assistant.application.registries import UnknownParser
from lab_assistant.contracts.parsing import DocumentParser, ParserProfile
from lab_assistant.domain.normalisation import SPAN_NORMALISATION_VERSION

__all__ = ["DEFAULT_PROFILES", "ParserRegistryImpl", "default_parser_registry"]


def _profile(
    profile_id: str,
    parser_id: str,
    parser_version: str,
    media_types: frozenset[str],
    **overrides: object,
) -> ParserProfile:
    return ParserProfile(
        profile_id=profile_id,
        profile_version="1.0.0",
        parser_id=parser_id,
        parser_version=parser_version,
        media_types=media_types,
        normalisation_version=SPAN_NORMALISATION_VERSION,
        **overrides,  # type: ignore[arg-type]
    )


#: Achievable thresholds per format, recorded rather than aspirational.
DEFAULT_PROFILES: dict[str, ParserProfile] = {
    "application/xml": _profile(
        "jats-default",
        "jats-etree",
        "1.0.0",
        frozenset({"application/xml", "text/xml"}),
        min_page_coverage=0.0,  # XML has no pages
        max_duplicate_paragraph_ratio=0.02,
        require_reference_separation=True,
        min_characters=200,
    ),
    "application/pdf": _profile(
        "pdf-born-digital",
        "pypdf-text",
        "1.0.0",
        frozenset({"application/pdf"}),
        # A page that yields no text means OCR is required; the rendition is
        # degraded and a reviewer decides, rather than it passing silently.
        min_page_coverage=1.0,
        max_duplicate_paragraph_ratio=0.10,
        max_replacement_char_rate=0.02,
        min_characters=500,
    ),
    "text/csv": _profile(
        "tabular-default",
        "csv-dialect",
        "1.0.0",
        frozenset({"text/csv", "text/tab-separated-values"}),
        min_page_coverage=0.0,
        min_characters=1,
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": _profile(
        "workbook-default",
        "openpyxl-workbook",
        "1.0.0",
        frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
        min_page_coverage=0.0,
        min_characters=1,
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _profile(
        "docx-default",
        "python-docx",
        "1.0.0",
        frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
        min_page_coverage=0.0,
        min_characters=100,
    ),
    "text/html": _profile(
        "html-default",
        "html-sanitised",
        "1.0.0",
        frozenset({"text/html"}),
        min_page_coverage=0.0,
        min_characters=100,
    ),
    "text/plain": _profile(
        "text-default",
        "text-blocks",
        "1.0.0",
        frozenset({"text/plain", "text/markdown", "application/json"}),
        min_page_coverage=0.0,
        min_characters=1,
    ),
}

#: Media types that share a profile with another type.
_PROFILE_ALIASES = {
    "text/xml": "application/xml",
    "application/jats+xml": "application/xml",
    "text/tab-separated-values": "text/csv",
    "text/markdown": "text/plain",
    "application/json": "text/plain",
    "application/vnd.oasis.opendocument.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}


@dataclass
class ParserRegistryImpl:
    parsers: list[DocumentParser] = field(default_factory=list)
    profiles: dict[str, ParserProfile] = field(default_factory=lambda: dict(DEFAULT_PROFILES))

    def for_media_type(self, media_type: str) -> DocumentParser:
        for parser in self.parsers:
            if parser.supports(media_type):
                return parser
        raise UnknownParser(
            f"no parser declares support for {media_type!r}; "
            f"registered: {[p.parser_id for p in self.parsers]}"
        )

    def profile_for(self, media_type: str) -> ParserProfile:
        key = _PROFILE_ALIASES.get(media_type, media_type)
        try:
            return self.profiles[key]
        except KeyError as exc:
            raise UnknownParser(f"no parser profile for {media_type!r}") from exc

    def list_ids(self) -> tuple[str, ...]:
        return tuple(sorted(parser.parser_id for parser in self.parsers))

    def register(self, parser: DocumentParser) -> None:
        self.parsers.append(parser)


def default_parser_registry(*, ocr_engine: object | None = None) -> ParserRegistryImpl:
    """The registry used by the parsing worker and the tests."""
    return ParserRegistryImpl(
        parsers=[
            JatsParser(),
            PdfParser(ocr_engine=ocr_engine),
            XlsxParser(),
            DocxParser(),
            CsvParser(),
            HtmlParser(),
            PlainTextParser(),
        ]
    )
