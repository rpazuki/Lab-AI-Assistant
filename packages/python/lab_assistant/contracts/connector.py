"""Connector contract.

A connector is a versioned adapter with declared capabilities and policy. An
operation a connector does not support is an absent capability, not a runtime
surprise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "AcquisitionRequest",
    "BodyKind",
    "CompiledQuery",
    "ConnectorCapabilities",
    "ConnectorOperation",
    "Cursor",
    "DiscoveryRequest",
    "Identifier",
    "IdentifierScheme",
    "RatePolicy",
    "RawAcquisition",
    "RawPage",
    "RawResponse",
    "SiteGate",
    "SourceConnector",
]


class ConnectorOperation(StrEnum):
    DISCOVER = "discover"
    RESOLVE = "resolve"
    ACQUIRE = "acquire"
    DELTA = "delta"


class IdentifierScheme(StrEnum):
    """Strong schemes reconcile identity; weak ones only propose."""

    DOI = "doi"
    PMID = "pmid"
    PMCID = "pmcid"
    ARXIV = "arxiv"
    BIORXIV_DOI = "biorxiv_doi"
    DATACITE_DOI = "datacite_doi"
    ACCESSION = "accession"
    OPENALEX = "openalex"
    INTERNAL = "internal"
    CONTENT_DIGEST = "content_digest"
    #: Weak: proposes a review, never satisfies an automatic merge.
    TITLE_NORMALISED = "title_normalised"

    @property
    def is_strong(self) -> bool:
        return self is not IdentifierScheme.TITLE_NORMALISED


class Identifier(BaseModel):
    model_config = {"frozen": True}

    scheme: IdentifierScheme
    value: str

    @field_validator("value")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier value must not be blank")
        return value


class RatePolicy(BaseModel):
    """Request budget for one connector credential/host pair.

    Values live in versioned connector policy, not in architecture prose;
    connector docs link the source's official current guidance.
    """

    model_config = {"frozen": True}

    requests_per_second: float = 1.0
    burst: int = 1
    max_concurrent: int = 1
    #: Contact identity some scholarly services require on every request.
    requires_request_identity: bool = True
    respect_retry_after: bool = True


@runtime_checkable
class SiteGate(Protocol):
    """Paces requests to each site a connector contacts, and rests one that refuses.

    A connector that downloads from many sites (an open-access resolver following
    links to publishers) calls ``enter`` before each request to a site and
    ``leave`` after it. ``enter`` waits briefly for the site's turn or raises
    ``rate_limited`` with ``context["deferred"] = True`` when the wait is long: the
    request was never sent, so the paper is tried again later at no cost. It
    raises ``policy_blocked`` when the site has refused so often it is treated as
    blocking automated downloads. ``leave`` reports how the site answered.
    """

    async def enter(self, host: str, *, policy: RatePolicy | None = None) -> None: ...

    async def leave(
        self,
        host: str,
        *,
        pushed_back: bool = False,
        succeeded: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None: ...


class ConnectorCapabilities(BaseModel):
    model_config = {"frozen": True}

    connector_id: str
    connector_version: str
    resource_kinds: frozenset[str]
    operations: frozenset[ConnectorOperation]
    query_schema_id: str | None
    output_schema_id: str
    pagination_modes: frozenset[str]
    supports_bulk_snapshot: bool
    supports_incremental_cursor: bool
    content_formats: frozenset[str]
    identifier_schemes: frozenset[IdentifierScheme]
    terms_url: str
    rate_policy: RatePolicy
    data_classification: str = "public_metadata"
    #: Names, never values (connectors §8.1).
    required_secret_names: frozenset[str] = frozenset()
    owner: str = "unassigned"
    terms_last_reviewed: str | None = None

    def supports(self, operation: ConnectorOperation) -> bool:
        return operation in self.operations


class CompiledQuery(BaseModel):
    """A scope compiled for one source's query language."""

    model_config = {"frozen": True}

    connector_id: str
    connector_version: str
    query_text: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: Digest over query text plus parameters plus connector version. Part of
    #: the cursor lineage key, so a changed query never resumes an old cursor.
    query_fingerprint: str


class Cursor(BaseModel):
    model_config = {"frozen": True}

    connector_id: str
    query_fingerprint: str
    #: Opaque source cursor: a token, a page number, or a high-water timestamp.
    position: str | None = None
    cursor_semantics_version: str = "1"
    exhausted: bool = False


class BodyKind(StrEnum):
    """What ``RawResponse.body`` actually holds.

    Some sources need two requests to produce one page (PubMed's esearch then
    efetch), so a connector's page body is a *replay envelope* built from more than
    one upstream response. That is useful and legitimate — and it is not the same
    thing as the source's own bytes, which is exactly the distinction an auditor
    checking provenance needs. Saying which one it is beats claiming both.
    """

    #: ``body`` is byte-for-byte what the source returned.
    UPSTREAM_BYTES = "upstream_bytes"
    #: ``body`` is a system-built envelope; ``upstream_body`` holds the source bytes.
    REPLAY_ENVELOPE = "replay_envelope"


class RawResponse(BaseModel):
    """One connector page body, plus the upstream bytes behind it.

    Credentials and sensitive query parameters are redacted before storage.
    Never store cookies, bearer tokens, or API keys here.
    """

    model_config = {"frozen": True}

    method: str
    url_redacted: str
    status: int
    headers_allowed: dict[str, str] = Field(default_factory=dict)
    body: bytes
    body_kind: BodyKind = BodyKind.UPSTREAM_BYTES
    #: The source's own bytes when ``body`` is an envelope. Stored as its own object
    #: so replay determinism never costs provenance.
    upstream_body: bytes | None = None
    requested_at: datetime
    completed_at: datetime
    retry_after_seconds: int | None = None

    @property
    def byte_length(self) -> int:
        return len(self.body)

    @property
    def source_bytes(self) -> bytes:
        """What the source returned, whichever field carries it."""
        if self.body_kind is BodyKind.UPSTREAM_BYTES:
            return self.body
        return self.upstream_body or b""


class RawPage(BaseModel):
    """One upstream response plus the cursor state it produced."""

    model_config = {"frozen": True}

    response: RawResponse
    cursor_before: Cursor
    cursor_after: Cursor
    #: Source-declared total, when the source declares one. Used for the
    #: pagination integrity gate, not for progress cosmetics.
    declared_total: int | None = None
    record_locators: tuple[str, ...] = ()


class DiscoveryRequest(BaseModel):
    model_config = {"frozen": True}

    compiled_query: CompiledQuery
    cursor: Cursor
    page_size: int = 50
    max_pages: int | None = None
    request_identity: str = "lab-ai-assistant (contact configured per deployment)"


class AcquisitionRequest(BaseModel):
    model_config = {"frozen": True}

    identifiers: tuple[Identifier, ...]
    #: The route class the plan authorised for this attempt.
    route: str
    target_url: str | None = None
    preferred_formats: tuple[str, ...] = ("application/xml", "application/pdf")
    max_bytes: int = 64 * 1024 * 1024
    request_identity: str = "lab-ai-assistant (contact configured per deployment)"


class RawAcquisition(BaseModel):
    """Bytes plus the evidence needed to make a rights decision about them."""

    model_config = {"frozen": True}

    response: RawResponse
    content: bytes
    declared_media_type: str | None = None
    licence_expression: str | None = None
    access_basis: str | None = None
    source_url_redacted: str | None = None
    embedded_identifiers: tuple[Identifier, ...] = ()


@runtime_checkable
class SourceConnector(Protocol):
    capabilities: ConnectorCapabilities

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery: ...

    def discover(self, request: DiscoveryRequest) -> AsyncIterator[RawPage]: ...

    def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]: ...

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition: ...

    def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]: ...
