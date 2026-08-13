"""Connector base helpers and the registry.

Shared machinery: canonical request construction, response envelope building,
rate-limit cooperation, and the assertion-shaping helpers every connector uses so
that "one source record" means the same thing across sources.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lab_assistant.application.registries import UnknownConnector
from lab_assistant.contracts.canonical import fingerprint
from lab_assistant.contracts.connector import (
    BodyKind,
    CompiledQuery,
    ConnectorCapabilities,
    ConnectorOperation,
    Cursor,
    Identifier,
    IdentifierScheme,
    RawResponse,
    SourceConnector,
)
from lab_assistant.domain.observation import Predicate

__all__ = [
    "ConnectorRegistryImpl",
    "assertion",
    "build_page_body",
    "compile_query_fingerprint",
    "make_response",
]


def compile_query_fingerprint(
    *, connector_id: str, connector_version: str, query: str, parameters: dict[str, Any]
) -> str:
    return fingerprint(
        kind="compiled_query",
        parts={
            "connector": f"{connector_id}@{connector_version}",
            "query": query,
            "parameters": parameters,
        },
    )


def make_response(
    *,
    method: str,
    url_redacted: str,
    status: int,
    body: bytes,
    upstream_body: bytes | None = None,
    requested_at: datetime | None = None,
    completed_at: datetime | None = None,
    headers: dict[str, str] | None = None,
    retry_after_seconds: int | None = None,
) -> RawResponse:
    """Build a response record with credentials already excluded.

    ``url_redacted`` must never contain an API key or a token; connectors pass
    credentials as headers and strip them here.

    Pass ``upstream_body`` whenever ``body`` is a page envelope the connector built
    rather than the source's own bytes. The two are then stored as separate objects
    and an auditor can tell them apart — a claim of "exact upstream bytes" that is
    only sometimes true is worse than no claim.
    """
    now = requested_at or datetime.now(tz=UTC)
    return RawResponse(
        method=method,
        url_redacted=url_redacted,
        status=status,
        headers_allowed=headers or {"content-type": "application/json"},
        body=body,
        body_kind=(
            BodyKind.REPLAY_ENVELOPE if upstream_body is not None else BodyKind.UPSTREAM_BYTES
        ),
        upstream_body=upstream_body,
        requested_at=now,
        completed_at=completed_at or now,
        retry_after_seconds=retry_after_seconds,
    )


def assertion(predicate: Predicate, value: Any, *, authority: str) -> dict[str, Any]:
    """One field-level assertion in a connector's canonical page body."""
    return {"predicate": predicate.value, "value": value, "authority": authority}


def identifier_assertion(scheme: IdentifierScheme, value: str, *, authority: str) -> dict[str, Any]:
    return assertion(
        Predicate.IDENTIFIER, {"scheme": scheme.value, "value": value}, authority=authority
    )


def build_page_body(records: Iterable[dict[str, Any]], *, total: int | None = None) -> bytes:
    """Serialise a connector's canonical page.

    The *raw* upstream bytes are stored before this shaping in a live connector;
    for fixture-backed connectors the fixture body **is** the raw response, so the
    replay boundary is preserved either way.
    """
    payload: dict[str, Any] = {"records": list(records)}
    if total is not None:
        payload["total"] = total
    return json.dumps(payload, sort_keys=True).encode("utf-8")


@dataclass(slots=True)
class ConnectorRegistryImpl:
    """Registry over concrete connectors.

    An unknown id raises. It never falls back to "the first connector that looks
    similar", which is the registry equivalent of a permissive test fake.
    """

    connectors: dict[str, SourceConnector]

    def get(self, connector_id: str) -> SourceConnector:
        try:
            return self.connectors[connector_id]
        except KeyError as exc:
            raise UnknownConnector(
                f"connector {connector_id!r} is not registered; "
                f"registered: {sorted(self.connectors)}"
            ) from exc

    def capabilities(self, connector_id: str) -> ConnectorCapabilities:
        return self.get(connector_id).capabilities

    def list_ids(self, *, operation: ConnectorOperation | None = None) -> tuple[str, ...]:
        return tuple(
            sorted(
                connector_id
                for connector_id, connector in self.connectors.items()
                if operation is None or connector.capabilities.supports(operation)
            )
        )

    def register(self, connector: SourceConnector) -> None:
        self.connectors[connector.capabilities.connector_id] = connector


def cursor_for(compiled: CompiledQuery, *, position: str | None, exhausted: bool = False) -> Cursor:
    return Cursor(
        connector_id=compiled.connector_id,
        query_fingerprint=compiled.query_fingerprint,
        position=position,
        exhausted=exhausted,
    )


def strong_identifiers(identifiers: Sequence[Identifier]) -> tuple[Identifier, ...]:
    return tuple(identifier for identifier in identifiers if identifier.scheme.is_strong)
