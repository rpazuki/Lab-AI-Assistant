"""Fixture-keyed fake connectors.

**The rule that makes the deterministic suite meaningful:** a fake raises on an
unmatched request. It never answers with a default record, and it never returns
"the closest thing it has".

This is the single most important lesson carried over from ``mistakes.md``: a
blanket-matching fake answered the next query with the wrong entity, which made
an entire end-to-end suite pass while proving nothing. The plan's whole test
strategy rests on fakes, so the rule is enforced in code here rather than
described in a document.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lab_assistant.adapters.connectors.base import (
    compile_query_fingerprint,
    cursor_for,
    make_response,
)
from lab_assistant.contracts.connector import (
    AcquisitionRequest,
    CompiledQuery,
    ConnectorCapabilities,
    ConnectorOperation,
    Cursor,
    DiscoveryRequest,
    Identifier,
    IdentifierScheme,
    RatePolicy,
    RawAcquisition,
    RawPage,
)
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = [
    "FakeAcquisitionConnector",
    "FakeSourceConnector",
    "FixtureMiss",
    "load_fixture_pages",
]


class FixtureMiss(LabAssistantError):
    """No fixture matched this request.

    Deliberately loud: the alternative is a fake that satisfies every call and a
    suite that proves nothing.
    """

    error_class = ErrorClass.INVALID_QUERY
    http_status = 500


@dataclass(frozen=True, slots=True)
class FixturePage:
    """One recorded page keyed by the exact query and cursor it answers."""

    query: str
    cursor_in: str | None
    cursor_out: str | None
    body: bytes
    status: int = 200
    declared_total: int | None = None
    exhausted: bool = False
    retry_after_seconds: int | None = None
    error_class: ErrorClass | None = None


def load_fixture_pages(path: Path) -> tuple[FixturePage, ...]:
    """Load pages from a JSON fixture file.

    Fixture shape::

        {"pages": [{"query": "...", "cursor_in": null, "cursor_out": "2",
                    "declared_total": 3, "body": {"records": [...]}}]}
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    pages: list[FixturePage] = []
    for page in payload.get("pages", []):
        body = page.get("body")
        pages.append(
            FixturePage(
                query=page["query"],
                cursor_in=page.get("cursor_in"),
                cursor_out=page.get("cursor_out"),
                body=json.dumps(body, sort_keys=True).encode("utf-8")
                if isinstance(body, dict)
                else str(body).encode("utf-8"),
                status=page.get("status", 200),
                declared_total=page.get("declared_total"),
                exhausted=page.get("exhausted", page.get("cursor_out") is None),
                retry_after_seconds=page.get("retry_after_seconds"),
                error_class=(ErrorClass(page["error_class"]) if page.get("error_class") else None),
            )
        )
    return tuple(pages)


@dataclass
class FakeSourceConnector:
    """A discovery connector backed entirely by recorded fixtures."""

    connector_id: str
    pages: tuple[FixturePage, ...]
    connector_version: str = "1.0.0"
    resource_kinds: frozenset[str] = frozenset({"article"})
    identifier_schemes: frozenset[IdentifierScheme] = frozenset(
        {IdentifierScheme.DOI, IdentifierScheme.PMID}
    )
    data_classification: str = "public_metadata"
    #: Optional error the connector raises instead of serving pages, so degraded
    #: source behaviour is testable.
    fail_with: ErrorClass | None = None
    fail_detail: str = "fixture-configured failure"

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            resource_kinds=self.resource_kinds,
            operations=frozenset({ConnectorOperation.DISCOVER, ConnectorOperation.RESOLVE}),
            query_schema_id="fake-query/1",
            output_schema_id="canonical-record/1",
            pagination_modes=frozenset({"cursor"}),
            supports_bulk_snapshot=False,
            supports_incremental_cursor=True,
            content_formats=frozenset({"application/json"}),
            identifier_schemes=self.identifier_schemes,
            terms_url="https://example.invalid/terms",
            rate_policy=RatePolicy(requests_per_second=1000.0, burst=100, max_concurrent=4),
            data_classification=self.data_classification,
            owner="test-fixtures",
            terms_last_reviewed="2026-08-12",
        )

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery:
        query = str(scope_payload.get("query", ""))
        parameters = dict(scope_payload.get("parameters", {}))
        if not query:
            raise FixtureMiss(f"{self.connector_id}: scope supplied no query to compile")
        return CompiledQuery(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            query_text=query,
            parameters=parameters,
            query_fingerprint=compile_query_fingerprint(
                connector_id=self.connector_id,
                connector_version=self.connector_version,
                query=query,
                parameters=parameters,
            ),
        )

    async def discover(self, request: DiscoveryRequest) -> AsyncIterator[RawPage]:
        if self.fail_with is not None:
            raise LabAssistantError(
                f"{self.connector_id}: {self.fail_detail}",
                context={"retry_after_seconds": 30},
            ).with_class(self.fail_with)

        query = request.compiled_query.query_text
        cursor = request.cursor.position
        served = 0
        while True:
            page = self._match(query, cursor)
            if page.error_class is not None:
                raise LabAssistantError(
                    f"{self.connector_id}: fixture page reports {page.error_class.value}",
                    context={"retry_after_seconds": page.retry_after_seconds},
                ).with_class(page.error_class)
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=f"https://{self.connector_id}.invalid/search?cursor={cursor or 0}",
                    status=page.status,
                    body=page.body,
                ),
                cursor_before=cursor_for(request.compiled_query, position=cursor),
                cursor_after=cursor_for(
                    request.compiled_query,
                    position=page.cursor_out,
                    exhausted=page.exhausted,
                ),
                declared_total=page.declared_total,
                record_locators=_locators(page.body),
            )
            served += 1
            cursor = page.cursor_out
            if page.exhausted or cursor is None:
                return
            if request.max_pages is not None and served >= request.max_pages:
                return

    async def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]:
        for identifier in identifiers:
            query = f"resolve:{identifier.scheme.value}:{identifier.value}"
            page = self._match(query, None)
            compiled = CompiledQuery(
                connector_id=self.connector_id,
                connector_version=self.connector_version,
                query_text=query,
                query_fingerprint=compile_query_fingerprint(
                    connector_id=self.connector_id,
                    connector_version=self.connector_version,
                    query=query,
                    parameters={},
                ),
            )
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=f"https://{self.connector_id}.invalid/resolve",
                    status=page.status,
                    body=page.body,
                ),
                cursor_before=cursor_for(compiled, position=None),
                cursor_after=cursor_for(compiled, position=None, exhausted=True),
                record_locators=_locators(page.body),
            )

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        raise FixtureMiss(f"{self.connector_id} does not implement acquire")

    async def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]:
        raise FixtureMiss(f"{self.connector_id} does not implement a change feed")
        yield  # pragma: no cover - keeps the signature an async generator

    def _match(self, query: str, cursor: str | None) -> FixturePage:
        for page in self.pages:
            if page.query == query and page.cursor_in == cursor:
                return page
        raise FixtureMiss(
            f"{self.connector_id}: no fixture page for query {query!r} at cursor {cursor!r}. "
            f"Recorded pages: " + ", ".join(f"({p.query!r}, {p.cursor_in!r})" for p in self.pages)
        )


@dataclass
class FakeAcquisitionConnector:
    """An acquisition connector keyed by identifier.

    ``assets`` maps ``"<scheme>:<value>"`` to the bytes it will return. An
    identifier that is not in the map raises :class:`FixtureMiss`, so a test can
    never accidentally acquire "some PDF".
    """

    connector_id: str = "fake-acquire"
    connector_version: str = "1.0.0"
    assets: dict[str, tuple[bytes, str, str]] = field(default_factory=dict)
    #: Identifiers configured to fail, with the error class to raise.
    failures: dict[str, ErrorClass] = field(default_factory=dict)

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            resource_kinds=frozenset({"article", "dataset"}),
            operations=frozenset({ConnectorOperation.ACQUIRE}),
            query_schema_id=None,
            output_schema_id="raw-asset/1",
            pagination_modes=frozenset(),
            supports_bulk_snapshot=False,
            supports_incremental_cursor=False,
            content_formats=frozenset({"application/pdf", "application/xml"}),
            identifier_schemes=frozenset(
                {IdentifierScheme.DOI, IdentifierScheme.PMCID, IdentifierScheme.PMID}
            ),
            terms_url="https://example.invalid/terms",
            rate_policy=RatePolicy(requests_per_second=1000.0, burst=100),
            owner="test-fixtures",
            terms_last_reviewed="2026-08-12",
        )

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery:
        raise FixtureMiss(f"{self.connector_id} does not implement discovery")

    async def discover(self, request: DiscoveryRequest) -> AsyncIterator[RawPage]:
        raise FixtureMiss(f"{self.connector_id} does not implement discovery")
        yield  # pragma: no cover

    async def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]:
        raise FixtureMiss(f"{self.connector_id} does not implement resolve")
        yield  # pragma: no cover

    async def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]:
        raise FixtureMiss(f"{self.connector_id} does not implement a change feed")
        yield  # pragma: no cover

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        for identifier in request.identifiers:
            key = f"{identifier.scheme.value}:{identifier.value}"
            if key in self.failures:
                raise LabAssistantError(
                    f"{self.connector_id}: configured failure for {key}",
                    context={"retry_after_seconds": 30},
                ).with_class(self.failures[key])
            if key in self.assets:
                content, media_type, licence = self.assets[key]
                return RawAcquisition(
                    response=make_response(
                        method="GET",
                        url_redacted=f"https://{self.connector_id}.invalid/{key}",
                        status=200,
                        body=b"",
                        headers={"content-type": media_type},
                    ),
                    content=content,
                    declared_media_type=media_type,
                    licence_expression=licence,
                    access_basis="open_access_repository",
                    source_url_redacted=f"https://{self.connector_id}.invalid/{key}",
                    embedded_identifiers=(identifier,),
                )
        raise FixtureMiss(
            f"{self.connector_id}: no fixture asset for "
            f"{[f'{i.scheme.value}:{i.value}' for i in request.identifiers]}. "
            f"Recorded: {sorted(self.assets)}"
        )


def _locators(body: bytes) -> tuple[str, ...]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    return tuple(
        str(record.get("locator") or record.get("id") or "")
        for record in payload.get("records", [])
    )


def fixture_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"records": list(records)}
