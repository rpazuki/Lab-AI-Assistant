"""Registries and small infrastructure protocols.

A registry maps a declared capability to an implementation. Selection is by
capability, never by a ``if source == "pubmed"`` switch — that switch is how the
predecessor ended up with PubMed as an implicit centre.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lab_assistant.contracts.connector import (
    ConnectorCapabilities,
    ConnectorOperation,
    SourceConnector,
)
from lab_assistant.contracts.parsing import DocumentParser, ParserProfile

__all__ = [
    "ConnectorRegistry",
    "MalwareScanResult",
    "MalwareScanner",
    "ParserRegistry",
    "UnknownConnector",
    "UnknownParser",
]


class UnknownConnector(LookupError):
    """Raised instead of returning a permissive default.

    A registry that silently substitutes something is the same failure mode as a
    test fake that answers an unmatched request: everything appears to work and
    nothing is proved.
    """


class UnknownParser(LookupError):
    """No parser declares support for this media type."""


@runtime_checkable
class ConnectorRegistry(Protocol):
    def get(self, connector_id: str) -> SourceConnector:
        """Return the connector, or raise :class:`UnknownConnector`."""
        ...

    def capabilities(self, connector_id: str) -> ConnectorCapabilities: ...

    def list_ids(self, *, operation: ConnectorOperation | None = None) -> tuple[str, ...]: ...


@runtime_checkable
class ParserRegistry(Protocol):
    def for_media_type(self, media_type: str) -> DocumentParser:
        """Return a parser that declares support, or raise :class:`UnknownParser`."""
        ...

    def profile_for(self, media_type: str) -> ParserProfile: ...

    def list_ids(self) -> tuple[str, ...]: ...


class MalwareScanResult(Protocol):
    clean: bool
    scanner: str
    signature_version: str | None
    detail: str | None


@runtime_checkable
class MalwareScanner(Protocol):
    scanner_id: str

    async def scan(self, data: bytes, *, filename: str | None = None) -> MalwareScanResult:
        """Scan bytes.

        An unavailable scanner reports itself unavailable. It never reports
        ``clean``: an out-of-date scanner passing everything is worse than a
        recorded exception.
        """
        ...

    async def signature_age_days(self) -> float | None: ...
