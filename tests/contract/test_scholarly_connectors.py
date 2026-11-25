"""Live connectors against the shapes the real sources send.

Each case is one a deployment met: a Crossref work with no issue date, and OpenAlex
refusing to page a keyless search. Offline: the HTTP client is a mock transport.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from lab_assistant.adapters.connectors.base import cursor_for
from lab_assistant.adapters.connectors.registry import live_connector_registry
from lab_assistant.adapters.connectors.scholarly import (
    CrossrefConnector,
    DataCiteConnector,
    EuropePmcConnector,
    OpenAlexConnector,
    PubMedConnector,
    UnpaywallResolver,
    _markup_text,
    _openalex_abstract,
)
from lab_assistant.config import Settings
from lab_assistant.contracts.connector import (
    AcquisitionRequest,
    Cursor,
    DiscoveryRequest,
    Identifier,
    IdentifierScheme,
)
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


# ── fetching full text ─────────────────────────────────────────────────────
PDF = b"%PDF-1.7\n a paper"
DOI = Identifier(scheme=IdentifierScheme.DOI, value="10.1000/yeast")


class TestEuropePmcFullText:
    async def test_asks_for_xml_which_is_what_the_endpoint_serves(self) -> None:
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["accept"])
            if request.headers["accept"] != "application/xml":
                return httpx.Response(406)
            return httpx.Response(200, content=b"<article/>")

        connector = EuropePmcConnector(
            request_identity="test", client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        raw = await connector.acquire(
            AcquisitionRequest(
                identifiers=(Identifier(scheme=IdentifierScheme.PMCID, value="PMC123"),),
                route="open_access_api",
            )
        )
        assert raw.content == b"<article/>" and seen == ["application/xml"]


def _unpaywall(routes: dict[str, httpx.Response], seen: list[str]) -> UnpaywallResolver:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).split("?")[0])
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response
        return httpx.Response(404)

    return UnpaywallResolver(
        request_identity="test",
        contact="lab@example.invalid",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


class TestUnpaywallFetch:
    async def test_the_link_discovery_found_is_fetched_first(self) -> None:
        seen: list[str] = []
        connector = _unpaywall(
            {"https://journal.example/paper.pdf": httpx.Response(200, content=PDF)}, seen
        )
        raw = await connector.acquire(
            AcquisitionRequest(
                identifiers=(DOI,),
                route="open_repository",
                target_url="https://journal.example/paper.pdf",
            )
        )
        assert raw.content == PDF and raw.declared_media_type == "application/pdf"
        assert seen == ["https://journal.example/paper.pdf"]  # no lookup was needed

    async def test_without_a_link_unpaywall_is_asked_for_one(self) -> None:
        seen: list[str] = []
        lookup = httpx.Response(
            200,
            json={
                "best_oa_location": {"url_for_pdf": "https://repo.example/a.pdf"},
                "oa_locations": [],
            },
        )
        connector = _unpaywall(
            {
                "https://api.unpaywall.org/v2/10.1000/yeast": lookup,
                "https://repo.example/a.pdf": httpx.Response(200, content=PDF),
            },
            seen,
        )
        raw = await connector.acquire(
            AcquisitionRequest(identifiers=(DOI,), route="open_repository")
        )
        assert raw.content == PDF
        assert seen == ["https://api.unpaywall.org/v2/10.1000/yeast", "https://repo.example/a.pdf"]

    async def test_a_redirect_is_followed_and_checked_again(self) -> None:
        seen: list[str] = []
        connector = _unpaywall(
            {
                "https://doi.example/x": httpx.Response(
                    302, headers={"location": "https://cdn.example/x.pdf"}
                ),
                "https://cdn.example/x.pdf": httpx.Response(200, content=PDF),
            },
            seen,
        )
        raw = await connector.acquire(
            AcquisitionRequest(
                identifiers=(), route="open_repository", target_url="https://doi.example/x"
            )
        )
        assert raw.content == PDF and seen == ["https://doi.example/x", "https://cdn.example/x.pdf"]

        to_local = _unpaywall(
            {
                "https://doi.example/y": httpx.Response(
                    302, headers={"location": "https://127.0.0.1/secret"}
                )
            },
            [],
        )
        with pytest.raises(LabAssistantError) as refused:
            await to_local.acquire(
                AcquisitionRequest(
                    identifiers=(), route="open_repository", target_url="https://doi.example/y"
                )
            )
        assert refused.value.error_class is ErrorClass.POLICY_BLOCKED

    async def test_a_landing_page_is_not_a_pdf(self) -> None:
        connector = _unpaywall(
            {
                "https://journal.example/landing": httpx.Response(
                    200, content=b"<html>Sign in</html>"
                )
            },
            [],
        )
        with pytest.raises(LabAssistantError) as refused:
            await connector.acquire(
                AcquisitionRequest(
                    identifiers=(),
                    route="open_repository",
                    target_url="https://journal.example/landing",
                )
            )
        assert refused.value.error_class is ErrorClass.INVALID_CONTENT


# ── abstracts ──────────────────────────────────────────────────────────────
PUBMED_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>40836088</PMID>
      <Article>
        <Journal>
          <Title>Nature</Title>
          <JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Engineered yeast provides rare but essential pollen sterols
          for honeybees.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Honeybees need <i>sterols</i>.</AbstractText>
          <AbstractText Label="RESULTS">Yarrowia made them.</AbstractText>
        </Abstract>
        <AuthorList><Author><LastName>Moore</LastName><ForeName>Elynor</ForeName></Author></AuthorList>
      </Article>
      <MeshHeadingList>
        <MeshHeading><DescriptorName>Yarrowia</DescriptorName></MeshHeading>
        <MeshHeading><DescriptorName>Sterols</DescriptorName></MeshHeading>
      </MeshHeadingList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">40836088</ArticleId>
        <ArticleId IdType="doi">10.1038/s41586-025-09431-y</ArticleId>
        <ArticleId IdType="pmc">PMC12507675</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""


def _values(record: dict, predicate: str) -> list:
    return [item["value"] for item in record["assertions"] if item["predicate"] == predicate]


class TestAbstracts:
    async def test_pubmed_fetches_each_pages_records_after_the_search(self) -> None:
        seen: list[tuple[str, str]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path.rsplit("/", 1)[-1], request.headers["accept"]))
            if request.url.path.endswith("esearch.fcgi"):
                return httpx.Response(
                    200, json={"esearchresult": {"idlist": ["40836088"], "count": "1"}}
                )
            return httpx.Response(200, content=PUBMED_XML)

        connector = PubMedConnector(
            request_identity="test", client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        compiled = await connector.compile_query({"query": "yarrowia sterols"})
        [page] = [
            page
            async for page in connector.discover(
                DiscoveryRequest(
                    compiled_query=compiled, cursor=cursor_for(compiled, position=None)
                )
            )
        ]
        assert seen == [("esearch.fcgi", "application/json"), ("efetch.fcgi", "application/xml")]
        [record] = json.loads(page.response.body)["records"]
        assert _values(record, "title") == [
            "Engineered yeast provides rare but essential pollen sterols for honeybees"
        ]
        assert _values(record, "abstract") == [
            "Background: Honeybees need sterols.\n\nResults: Yarrowia made them."
        ]
        assert _values(record, "subject_term") == ["Yarrowia", "Sterols"]
        assert _values(record, "journal") == ["Nature"] and _values(record, "year") == [2025]
        schemes = {
            item["value"]["scheme"]
            for item in record["assertions"]
            if item["predicate"] == "identifier"
        }
        assert schemes == {"pmid", "doi", "pmcid"}

    def test_openalex_rebuilds_the_text_from_its_word_index(self) -> None:
        index = {"Yarrowia": [0], "makes": [1], "lipids": [2, 5], "and": [3], "more": [4]}
        assert _openalex_abstract(index) == "Yarrowia makes lipids and more lipids"
        assert _openalex_abstract(None) is None and _openalex_abstract({}) is None
        record = OpenAlexConnector._record(
            {"id": "https://openalex.org/W1", "title": "T", "abstract_inverted_index": index}
        )
        assert _values(record, "abstract") == ["Yarrowia makes lipids and more lipids"]

    def test_crossref_abstracts_lose_their_markup(self) -> None:
        jats = (
            "<jats:title>Abstract</jats:title><jats:p>Lipids &amp; "
            "<jats:italic>Y. lipolytica</jats:italic></jats:p>"
            "<jats:p>Second   paragraph.</jats:p>"
        )
        assert _markup_text(jats) == "Lipids & Y. lipolytica\n\nSecond paragraph."
        record = CrossrefConnector._record({"DOI": "10.1/x", "title": ["T"], "abstract": jats})
        assert _values(record, "abstract") == ["Lipids & Y. lipolytica\n\nSecond paragraph."]
        assert _markup_text("") is None
