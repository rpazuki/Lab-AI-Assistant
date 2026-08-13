"""Document parsers and their profiles."""

from lab_assistant.adapters.parsers.documents import (
    CsvParser,
    DocxParser,
    HtmlParser,
    PdfParser,
    PlainTextParser,
    XlsxParser,
)
from lab_assistant.adapters.parsers.jats import JatsParser
from lab_assistant.adapters.parsers.registry import (
    DEFAULT_PROFILES,
    ParserRegistryImpl,
    default_parser_registry,
)

__all__ = [
    "DEFAULT_PROFILES",
    "CsvParser",
    "DocxParser",
    "HtmlParser",
    "JatsParser",
    "ParserRegistryImpl",
    "PdfParser",
    "PlainTextParser",
    "XlsxParser",
    "default_parser_registry",
]
