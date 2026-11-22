"""Live connectors against the shapes the real sources send.

Each case is one a deployment met: a Crossref work with no issue date, and OpenAlex
refusing to page a keyless search. Offline: the HTTP client is a mock transport.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from lab_assistant.adapters.connectors.registry import live_connector_registry
from lab_assistant.adapters.connectors.scholarly import (
    CrossrefConnector,
    DataCiteConnector,
    EuropePmcConnector,
    OpenAlexConnector,
)
from lab_assistant.config import Settings
from lab_assistant.contracts.connector import Cursor, DiscoveryRequest
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError


def _years(record: dict) -> list:
    return [item["value"] for item in record["assertions"] if item["predicate"] == "year"]


class TestMissingYears:
    """A work without a year is a work without a year assertion, not a failed page."""

    @pytest.mark.parametrize(
        "issued",
        [{"date-parts": [[None]]}, {"date-parts": [[]]}, {"date-parts": []}, {}, None],
    )
    def test_crossref_without_an_issue_date(self, issued) -> None:
        record = CrossrefConnector._record({"DOI": "10.1234/no-date", "issued": issued})
        assert record["locator"] == "doi:10.1234/no-date"
        assert _years(record) == []

    def test_crossref_with_an_issue_date(self) -> None:
        record = CrossrefConnector._record(
            {"DOI": "10.1234/dated", "issued": {"date-parts": [[2021, 4, 2]]}}
        )
        assert _years(record) == [2021]

    def test_other_sources_ignore_a_year_that_is_not_one(self) -> None:
        assert _years(EuropePmcConnector._record({"id": "1", "pubYear": "n.d."})) == []
        assert _years(OpenAlexConnector._record({"id": "W1", "publication_year": None})) == []
        assert _years(OpenAlexConnector._record({"id": "W1", "publication_year": 2019})) == [2019]


def _request(connector: OpenAlexConnector) -> DiscoveryRequest:
    from lab_assistant.adapters.connectors.base import compile_query_fingerprint
    from lab_assistant.contracts.connector import CompiledQuery

    compiled = CompiledQuery(
        connector_id=connector.connector_id,
        connector_version=connector.connector_version,
        query_text="yarrowia",
        query_fingerprint=compile_query_fingerprint(
            connector_id=connector.connector_id,
            connector_version=connector.connector_version,
            query="yarrowia",
            parameters={},
        ),
    )
    return DiscoveryRequest(
        compiled_query=compiled,
        cursor=Cursor(
            connector_id=compiled.connector_id, query_fingerprint=compiled.query_fingerprint
        ),
    )


def _openalex(api_key: str | None, seen: list[dict]) -> OpenAlexConnector:
    """OpenAlex as it behaves now: page one for anyone, page two only with a key."""

    def handle(request: httpx.Request) -> httpx.Response:
        params = {name: values[0] for name, values in parse_qs(request.url.query.decode()).items()}
        seen.append(params)
        if params.get("cursor") != "*" and "api_key" not in params:
            return httpx.Response(401, json={"error": "API key required for cursor paging"})
        last = params.get("cursor") == "page-2"
        return httpx.Response(
            200,
            json={
                "meta": {"count": 2, "next_cursor": None if last else "page-2"},
                "results": [{"id": f"https://openalex.org/W{len(seen)}", "title": "A work"}],
            },
        )

    return OpenAlexConnector(
        request_identity="test",
        api_key=api_key,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


class TestOpenAlexKey:
    async def test_a_keyed_search_pages_and_never_stores_the_key(self) -> None:
        seen: list[dict] = []
        connector = _openalex("openalex-test-key", seen)
        pages = [page async for page in connector.discover(_request(connector))]
        assert len(pages) == 2
        assert all(params.get("api_key") == "openalex-test-key" for params in seen)
        for page in pages:
            stored = urlparse(page.response.url_redacted)
            assert "api_key" not in parse_qs(stored.query)

    async def test_a_keyless_search_says_which_setting_is_missing(self) -> None:
        seen: list[dict] = []
        connector = _openalex(None, seen)
        pages = []
        with pytest.raises(LabAssistantError) as raised:
            async for page in connector.discover(_request(connector)):
                pages.append(page)
        assert len(pages) == 1
        assert raised.value.error_class is ErrorClass.AUTHENTICATION_REQUIRED
        assert "LAB_OPENALEX_API_KEY" in str(raised.value)

    def test_the_setting_reaches_the_connector(self) -> None:
        settings = Settings(
            database_url="memory://",
            object_store_root="/tmp/lab-assistant-connector-test",
            openalex_api_key="openalex-test-key",
        )
        connector = live_connector_registry(settings).get("openalex")
        assert connector.api_key == "openalex-test-key"
        assert connector.capabilities.required_secret_names == frozenset({"OPENALEX_API_KEY"})

    def test_the_key_is_a_secret_setting(self) -> None:
        # Refused in committed config files, like every other credential.
        assert "openalex_api_key" in Settings.secret_field_names


def test_datacite_without_a_year() -> None:
    record = DataCiteConnector._record({"id": "10.5/x", "attributes": {"publicationYear": ""}})
    assert _years(record) == []
