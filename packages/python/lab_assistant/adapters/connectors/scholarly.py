"""Live scholarly connectors: PubMed, Europe PMC, Crossref, OpenAlex, DataCite.

Each connector owns its query language, its pagination contract, its rate policy,
and the mapping from its fields onto assertions. None of them is privileged: the
domain has no PubMed-shaped centre, and adding a source is adding a package, not
editing a switch statement.

Rate limits and request-identity requirements are **not** copied as constants
into this file beyond conservative defaults; each connector's documentation page
links the source's current official guidance, and the runtime values live in
versioned connector configuration.

Every connector hands the upstream response bytes to the caller unmodified. The
canonical record shaping happens after those bytes are stored, so a mapping bug is
replayable without contacting the source again.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from lab_assistant.adapters.connectors.base import (
    assertion,
    compile_query_fingerprint,
    cursor_for,
    identifier_assertion,
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
from lab_assistant.domain.observation import Predicate

__all__ = [
    "CrossrefConnector",
    "DataCiteConnector",
    "EuropePmcConnector",
    "OpenAlexConnector",
    "PubMedConnector",
    "UnpaywallResolver",
    "map_http_error",
]


def _year(value: Any) -> int | None:
    """A publication year as a number, or None when the source sent none.

    Sources send years as numbers, digit strings, or a placeholder: Crossref's
    ``date-parts`` is ``[[null]]`` for a work with no issue date. One such record
    must not fail its whole page, so anything that is not a year is no year.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def map_http_error(status: int, *, retry_after: str | None = None) -> LabAssistantError:
    """Map an upstream status onto the outcome taxonomy.

    A 200 is not a licence and a 403 is not a bug — each status means something
    operationally different, and collapsing them into "failed" is what made the
    predecessor's retries unhelpful.
    """
    context: dict[str, Any] = {}
    if retry_after and retry_after.isdigit():
        context["retry_after_seconds"] = int(retry_after)
    if status == 429:
        return LabAssistantError("upstream rate limited the request", context=context).with_class(
            ErrorClass.RATE_LIMITED
        )
    if status in (401, 403):
        return LabAssistantError("upstream requires authentication", context=context).with_class(
            ErrorClass.AUTHENTICATION_REQUIRED
        )
    if status == 404:
        return LabAssistantError("upstream has no such record", context=context).with_class(
            ErrorClass.NOT_FOUND
        )
    if status == 400:
        return LabAssistantError("upstream rejected the query", context=context).with_class(
            ErrorClass.INVALID_QUERY
        )
    if 500 <= status < 600:
        return LabAssistantError("upstream is unavailable", context=context).with_class(
            ErrorClass.SOURCE_UNAVAILABLE
        )
    return LabAssistantError(f"unexpected upstream status {status}", context=context).with_class(
        ErrorClass.PROVIDER_ERROR
    )


@dataclass
class _HttpConnector:
    """Shared HTTP plumbing: identity headers, timeouts, and status mapping."""

    request_identity: str
    contact: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 30.0
    client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        # Several scholarly services require an identifying User-Agent and a
        # contact address. Credentials never go in the URL.
        agent = self.request_identity
        if self.contact:
            agent = f"{agent} (mailto:{self.contact})"
        return {"User-Agent": agent, "Accept": "application/json"}

    async def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        client = self.client or httpx.AsyncClient(timeout=self.timeout_seconds)
        owns_client = self.client is None
        try:
            response = await client.get(url, params=params, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise LabAssistantError(f"timeout contacting {url}").with_class(
                ErrorClass.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise LabAssistantError(f"transport error contacting {url}: {exc}").with_class(
                ErrorClass.SOURCE_UNAVAILABLE
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code != 200:
            raise map_http_error(
                response.status_code, retry_after=response.headers.get("retry-after")
            )
        return response

    @staticmethod
    def _redact(url: str) -> str:
        base, _, query = url.partition("?")
        if not query:
            return url
        safe = [
            pair
            for pair in query.split("&")
            if not pair.lower().startswith(("api_key=", "apikey=", "token=", "email="))
        ]
        return f"{base}?{'&'.join(safe)}" if safe else base


@dataclass
class PubMedConnector(_HttpConnector):
    """NCBI E-utilities.

    Two-step contract: ``esearch`` returns identifiers and a history cursor,
    ``efetch`` returns records. Rate and identification rules come from NCBI's
    current usage guidance, which the connector documentation page links rather
    than this file restating.
    """

    connector_id: str = "pubmed"
    connector_version: str = "1.0.0"
    base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    page_size: int = 100

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            resource_kinds=frozenset({"article", "review"}),
            operations=frozenset({ConnectorOperation.DISCOVER, ConnectorOperation.RESOLVE}),
            query_schema_id="pubmed-term/1",
            output_schema_id="canonical-record/1",
            pagination_modes=frozenset({"retstart"}),
            supports_bulk_snapshot=False,
            supports_incremental_cursor=True,
            content_formats=frozenset({"application/xml", "application/json"}),
            identifier_schemes=frozenset(
                {IdentifierScheme.PMID, IdentifierScheme.PMCID, IdentifierScheme.DOI}
            ),
            terms_url="https://www.ncbi.nlm.nih.gov/books/NBK25497/",
            rate_policy=RatePolicy(
                requests_per_second=3.0 if not self.api_key else 9.0,
                burst=3,
                max_concurrent=2,
            ),
            data_classification="public_metadata",
            required_secret_names=frozenset({"NCBI_API_KEY"}) if self.api_key else frozenset(),
            owner="platform",
        )

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery:
        query = str(scope_payload.get("query", "")).strip()
        if not query:
            raise LabAssistantError("PubMed scope entry has no term").with_class(
                ErrorClass.INVALID_QUERY
            )
        scope = scope_payload.get("scope") or {}
        parameters: dict[str, Any] = {"db": "pubmed"}
        if scope.get("date_from") or scope.get("date_to"):
            parameters["mindate"] = str(scope.get("date_from") or "1900/01/01").replace("-", "/")
            parameters["maxdate"] = str(scope.get("date_to") or "3000/01/01").replace("-", "/")
            parameters["datetype"] = "pdat"
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
        start = int(request.cursor.position or 0)
        pages = 0
        while True:
            params: dict[str, Any] = {
                **request.compiled_query.parameters,
                "term": request.compiled_query.query_text,
                "retmode": "json",
                "retstart": start,
                "retmax": min(request.page_size, self.page_size),
            }
            if self.api_key:
                params["api_key"] = self.api_key
            if self.contact:
                params["email"] = self.contact
            response = await self._get(f"{self.base_url}/esearch.fcgi", params)
            payload = response.json()
            result = payload.get("esearchresult", {})
            pmids: list[str] = list(result.get("idlist", []))
            total = int(result.get("count", 0) or 0)
            records = [
                {
                    "locator": f"pmid:{pmid}",
                    "assertions": [
                        identifier_assertion(
                            IdentifierScheme.PMID, pmid, authority="biomedical_index"
                        )
                    ],
                }
                for pmid in pmids
            ]
            # A page envelope, not the source's bytes: `upstream_body` carries
            # those so provenance and replay stay separable.
            body = json.dumps({"records": records, "raw": payload}, sort_keys=True).encode()
            next_start = start + len(pmids)
            exhausted = not pmids or next_start >= total
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=body,
                    upstream_body=response.content,
                ),
                cursor_before=cursor_for(request.compiled_query, position=str(start)),
                cursor_after=cursor_for(
                    request.compiled_query,
                    position=None if exhausted else str(next_start),
                    exhausted=exhausted,
                ),
                declared_total=total,
                record_locators=tuple(record["locator"] for record in records),
            )
            pages += 1
            if exhausted or (request.max_pages is not None and pages >= request.max_pages):
                return
            start = next_start

    async def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]:
        pmids = [i.value for i in identifiers if i.scheme is IdentifierScheme.PMID]
        if not pmids:
            return
        params: dict[str, Any] = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
        }
        if self.api_key:
            params["api_key"] = self.api_key
        response = await self._get(f"{self.base_url}/efetch.fcgi", params)
        compiled = CompiledQuery(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            query_text=f"efetch:{','.join(pmids)}",
            query_fingerprint=compile_query_fingerprint(
                connector_id=self.connector_id,
                connector_version=self.connector_version,
                query=f"efetch:{','.join(pmids)}",
                parameters={},
            ),
        )
        yield RawPage(
            response=make_response(
                method="GET",
                url_redacted=self._redact(str(response.url)),
                status=response.status_code,
                body=response.content,
                headers={"content-type": "application/xml"},
            ),
            cursor_before=cursor_for(compiled, position=None),
            cursor_after=cursor_for(compiled, position=None, exhausted=True),
        )

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        raise LabAssistantError(
            "PubMed provides metadata; full text comes from Europe PMC or a repository"
        ).with_class(ErrorClass.POLICY_BLOCKED)

    async def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]:
        raise LabAssistantError("PubMed has no change feed in this connector").with_class(
            ErrorClass.INVALID_QUERY
        )
        yield  # pragma: no cover


@dataclass
class EuropePmcConnector(_HttpConnector):
    """Europe PMC REST: discovery plus open-access full text."""

    connector_id: str = "europepmc"
    connector_version: str = "1.0.0"
    base_url: str = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            resource_kinds=frozenset({"article", "preprint"}),
            operations=frozenset(
                {
                    ConnectorOperation.DISCOVER,
                    ConnectorOperation.RESOLVE,
                    ConnectorOperation.ACQUIRE,
                }
            ),
            query_schema_id="europepmc-query/1",
            output_schema_id="canonical-record/1",
            pagination_modes=frozenset({"cursorMark"}),
            supports_bulk_snapshot=False,
            supports_incremental_cursor=True,
            content_formats=frozenset({"application/xml", "application/json"}),
            identifier_schemes=frozenset(
                {IdentifierScheme.DOI, IdentifierScheme.PMID, IdentifierScheme.PMCID}
            ),
            terms_url="https://europepmc.org/RestfulWebService",
            rate_policy=RatePolicy(requests_per_second=3.0, burst=3, max_concurrent=2),
            owner="platform",
        )

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery:
        query = str(scope_payload.get("query", "")).strip()
        if not query:
            raise LabAssistantError("Europe PMC scope entry has no query").with_class(
                ErrorClass.INVALID_QUERY
            )
        parameters = {"format": "json", "resultType": "core"}
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
        cursor_mark = request.cursor.position or "*"
        pages = 0
        while True:
            params = {
                **request.compiled_query.parameters,
                "query": request.compiled_query.query_text,
                "pageSize": request.page_size,
                "cursorMark": cursor_mark,
            }
            response = await self._get(f"{self.base_url}/search", params)
            payload = response.json()
            results = payload.get("resultList", {}).get("result", [])
            next_cursor = payload.get("nextCursorMark")
            total = payload.get("hitCount")
            records = [self._record(result) for result in results]
            # A page envelope, not the source's bytes: `upstream_body` carries
            # those so provenance and replay stay separable.
            body = json.dumps({"records": records, "raw": payload}, sort_keys=True).encode()
            exhausted = not results or next_cursor in (None, cursor_mark)
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=body,
                    upstream_body=response.content,
                ),
                cursor_before=cursor_for(request.compiled_query, position=cursor_mark),
                cursor_after=cursor_for(
                    request.compiled_query,
                    position=None if exhausted else next_cursor,
                    exhausted=exhausted,
                ),
                declared_total=int(total) if total is not None else None,
                record_locators=tuple(record["locator"] for record in records),
            )
            pages += 1
            if exhausted or (request.max_pages is not None and pages >= request.max_pages):
                return
            cursor_mark = next_cursor

    @staticmethod
    def _record(result: dict[str, Any]) -> dict[str, Any]:
        assertions: list[dict[str, Any]] = []
        authority = "biomedical_index"
        if result.get("doi"):
            assertions.append(
                identifier_assertion(IdentifierScheme.DOI, result["doi"], authority=authority)
            )
        if result.get("pmid"):
            assertions.append(
                identifier_assertion(IdentifierScheme.PMID, result["pmid"], authority=authority)
            )
        if result.get("pmcid"):
            assertions.append(
                identifier_assertion(IdentifierScheme.PMCID, result["pmcid"], authority=authority)
            )
        if result.get("title"):
            assertions.append(assertion(Predicate.TITLE, result["title"], authority=authority))
        if result.get("abstractText"):
            assertions.append(
                assertion(Predicate.ABSTRACT, result["abstractText"], authority=authority)
            )
        if result.get("journalTitle"):
            assertions.append(
                assertion(Predicate.JOURNAL, result["journalTitle"], authority=authority)
            )
        if (year := _year(result.get("pubYear"))) is not None:
            assertions.append(assertion(Predicate.YEAR, year, authority=authority))
        licence = (result.get("license") or "").strip()
        if licence:
            assertions.append(assertion(Predicate.LICENCE, licence, authority=authority))
        if result.get("isOpenAccess") == "Y" and result.get("pmcid"):
            assertions.append(
                assertion(
                    Predicate.FULL_TEXT_LOCATION,
                    f"pmc:{result['pmcid']}",
                    authority=authority,
                )
            )
        for author in (result.get("authorList") or {}).get("author", [])[:50]:
            assertions.append(
                assertion(
                    Predicate.AUTHOR,
                    {
                        "last_name": author.get("lastName"),
                        "fore_name": author.get("firstName"),
                        "orcid": (author.get("authorId") or {}).get("value")
                        if (author.get("authorId") or {}).get("type") == "ORCID"
                        else None,
                    },
                    authority=authority,
                )
            )
        locator = (
            f"doi:{result['doi']}"
            if result.get("doi")
            else f"pmid:{result.get('pmid') or result.get('id')}"
        )
        return {"locator": locator, "assertions": assertions}

    async def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]:
        for identifier in identifiers:
            query = (
                f"EXT_ID:{identifier.value}"
                if identifier.scheme is IdentifierScheme.PMID
                else (f"DOI:{identifier.value}")
            )
            compiled = await self.compile_query({"query": query})
            async for page in self.discover(
                DiscoveryRequest(
                    compiled_query=compiled, cursor=cursor_for(compiled, position=None)
                )
            ):
                yield page

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        pmcid = next(
            (i.value for i in request.identifiers if i.scheme is IdentifierScheme.PMCID), None
        )
        if pmcid is None:
            raise LabAssistantError(
                "Europe PMC full text needs a PMCID; this work has none"
            ).with_class(ErrorClass.NOT_FOUND)
        response = await self._get(f"{self.base_url}/{pmcid}/fullTextXML", {})
        return RawAcquisition(
            response=make_response(
                method="GET",
                url_redacted=self._redact(str(response.url)),
                status=response.status_code,
                body=b"",
                headers={"content-type": "application/xml"},
            ),
            content=response.content,
            declared_media_type="application/xml",
            licence_expression=None,  # the licence assertion comes from discovery
            access_basis="europepmc_open_access",
            source_url_redacted=self._redact(str(response.url)),
            embedded_identifiers=tuple(request.identifiers),
        )

    async def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]:
        raise LabAssistantError("no change feed configured for Europe PMC").with_class(
            ErrorClass.INVALID_QUERY
        )
        yield  # pragma: no cover


@dataclass
class CrossrefConnector(_HttpConnector):
    """Crossref REST: DOI registration metadata and relations."""

    connector_id: str = "crossref"
    connector_version: str = "1.0.0"
    base_url: str = "https://api.crossref.org"

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            resource_kinds=frozenset(
                {"article", "preprint", "book_chapter", "conference_item", "dataset"}
            ),
            operations=frozenset({ConnectorOperation.DISCOVER, ConnectorOperation.RESOLVE}),
            query_schema_id="crossref-query/1",
            output_schema_id="canonical-record/1",
            pagination_modes=frozenset({"cursor"}),
            supports_bulk_snapshot=True,
            supports_incremental_cursor=True,
            content_formats=frozenset({"application/json"}),
            identifier_schemes=frozenset({IdentifierScheme.DOI}),
            terms_url="https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
            rate_policy=RatePolicy(requests_per_second=5.0, burst=5, max_concurrent=2),
            owner="platform",
        )

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery:
        query = str(scope_payload.get("query", "")).strip()
        if not query:
            raise LabAssistantError("Crossref scope entry has no query").with_class(
                ErrorClass.INVALID_QUERY
            )
        scope = scope_payload.get("scope") or {}
        parameters: dict[str, Any] = {}
        filters = []
        if scope.get("date_from"):
            filters.append(f"from-pub-date:{scope['date_from']}")
        if scope.get("date_to"):
            filters.append(f"until-pub-date:{scope['date_to']}")
        if filters:
            parameters["filter"] = ",".join(filters)
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
        cursor = request.cursor.position or "*"
        pages = 0
        while True:
            params = {
                **request.compiled_query.parameters,
                "query.bibliographic": request.compiled_query.query_text,
                "rows": request.page_size,
                "cursor": cursor,
            }
            if self.contact:
                params["mailto"] = self.contact
            response = await self._get(f"{self.base_url}/works", params)
            payload = response.json()
            message = payload.get("message", {})
            items = message.get("items", [])
            next_cursor = message.get("next-cursor")
            total = message.get("total-results")
            records = [self._record(item) for item in items]
            # A page envelope, not the source's bytes: `upstream_body` carries
            # those so provenance and replay stay separable.
            body = json.dumps({"records": records, "raw": payload}, sort_keys=True).encode()
            exhausted = not items or next_cursor in (None, cursor)
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=body,
                    upstream_body=response.content,
                ),
                cursor_before=cursor_for(request.compiled_query, position=cursor),
                cursor_after=cursor_for(
                    request.compiled_query,
                    position=None if exhausted else next_cursor,
                    exhausted=exhausted,
                ),
                declared_total=int(total) if total is not None else None,
                record_locators=tuple(record["locator"] for record in records),
            )
            pages += 1
            if exhausted or (request.max_pages is not None and pages >= request.max_pages):
                return
            cursor = next_cursor

    @staticmethod
    def _record(item: dict[str, Any]) -> dict[str, Any]:
        authority = "doi_registry"
        assertions: list[dict[str, Any]] = []
        doi = item.get("DOI")
        if doi:
            assertions.append(identifier_assertion(IdentifierScheme.DOI, doi, authority=authority))
        titles = item.get("title") or []
        if titles:
            assertions.append(assertion(Predicate.TITLE, titles[0], authority=authority))
        if item.get("type"):
            assertions.append(assertion(Predicate.RESOURCE_KIND, item["type"], authority=authority))
        containers = item.get("container-title") or []
        if containers:
            assertions.append(assertion(Predicate.JOURNAL, containers[0], authority=authority))
        issued = (item.get("issued") or {}).get("date-parts") or []
        if issued and issued[0] and (year := _year(issued[0][0])) is not None:
            assertions.append(assertion(Predicate.YEAR, year, authority=authority))
        if item.get("publisher"):
            assertions.append(
                assertion(Predicate.PUBLISHER, item["publisher"], authority=authority)
            )
        for licence in item.get("license") or []:
            if licence.get("URL"):
                assertions.append(assertion(Predicate.LICENCE, licence["URL"], authority=authority))
        for relation_kind, entries in (item.get("relation") or {}).items():
            for entry in entries:
                assertions.append(
                    assertion(
                        Predicate.RELATION,
                        {
                            "kind": relation_kind,
                            "id": entry.get("id"),
                            "type": entry.get("id-type"),
                        },
                        authority=authority,
                    )
                )
        for author in (item.get("author") or [])[:50]:
            assertions.append(
                assertion(
                    Predicate.AUTHOR,
                    {
                        "last_name": author.get("family"),
                        "fore_name": author.get("given"),
                        "orcid": author.get("ORCID"),
                    },
                    authority=authority,
                )
            )
        return {
            "locator": f"doi:{doi}" if doi else f"crossref:{item.get('URL')}",
            "assertions": assertions,
        }

    async def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]:
        for identifier in identifiers:
            if identifier.scheme is not IdentifierScheme.DOI:
                continue
            response = await self._get(f"{self.base_url}/works/{identifier.value}", {})
            payload = response.json()
            record = self._record(payload.get("message", {}))
            compiled = CompiledQuery(
                connector_id=self.connector_id,
                connector_version=self.connector_version,
                query_text=f"doi:{identifier.value}",
                query_fingerprint=compile_query_fingerprint(
                    connector_id=self.connector_id,
                    connector_version=self.connector_version,
                    query=f"doi:{identifier.value}",
                    parameters={},
                ),
            )
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=json.dumps({"records": [record], "raw": payload}, sort_keys=True).encode(),
                    upstream_body=response.content,
                ),
                cursor_before=cursor_for(compiled, position=None),
                cursor_after=cursor_for(compiled, position=None, exhausted=True),
                record_locators=(record["locator"],),
            )

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        raise LabAssistantError("Crossref supplies metadata, not content").with_class(
            ErrorClass.POLICY_BLOCKED
        )

    async def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]:
        raise LabAssistantError("no change feed configured for Crossref").with_class(
            ErrorClass.INVALID_QUERY
        )
        yield  # pragma: no cover


@dataclass
class OpenAlexConnector(_HttpConnector):
    """OpenAlex: broad coverage, aggregated fields that need source-aware use."""

    connector_id: str = "openalex"
    connector_version: str = "1.0.0"
    base_url: str = "https://api.openalex.org"

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            resource_kinds=frozenset({"article", "preprint", "dataset", "book_chapter"}),
            operations=frozenset({ConnectorOperation.DISCOVER, ConnectorOperation.RESOLVE}),
            query_schema_id="openalex-search/1",
            output_schema_id="canonical-record/1",
            pagination_modes=frozenset({"cursor"}),
            supports_bulk_snapshot=True,
            supports_incremental_cursor=True,
            content_formats=frozenset({"application/json"}),
            identifier_schemes=frozenset(
                {IdentifierScheme.OPENALEX, IdentifierScheme.DOI, IdentifierScheme.PMID}
            ),
            terms_url="https://docs.openalex.org/",
            rate_policy=RatePolicy(requests_per_second=5.0, burst=5, max_concurrent=2),
            required_secret_names=(
                frozenset({"OPENALEX_API_KEY"}) if self.api_key else frozenset()
            ),
            owner="platform",
        )

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery:
        query = str(scope_payload.get("query", "")).strip()
        if not query:
            raise LabAssistantError("OpenAlex scope entry has no query").with_class(
                ErrorClass.INVALID_QUERY
            )
        scope = scope_payload.get("scope") or {}
        filters = []
        if scope.get("date_from"):
            filters.append(f"from_publication_date:{scope['date_from']}")
        if scope.get("date_to"):
            filters.append(f"to_publication_date:{scope['date_to']}")
        parameters = {"filter": ",".join(filters)} if filters else {}
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
        cursor = request.cursor.position or "*"
        pages = 0
        while True:
            params = {
                **request.compiled_query.parameters,
                "search": request.compiled_query.query_text,
                "per-page": min(request.page_size, 200),
                "cursor": cursor,
            }
            if self.contact:
                params["mailto"] = self.contact
            try:
                response = await self._get(f"{self.base_url}/works", self._keyed(params))
            except LabAssistantError as exc:
                raise self._explain(exc) from exc
            payload = response.json()
            results = payload.get("results", [])
            meta = payload.get("meta", {})
            next_cursor = meta.get("next_cursor")
            records = [self._record(result) for result in results]
            # A page envelope, not the source's bytes: `upstream_body` carries
            # those so provenance and replay stay separable.
            body = json.dumps({"records": records, "raw": payload}, sort_keys=True).encode()
            exhausted = not results or not next_cursor
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=body,
                    upstream_body=response.content,
                ),
                cursor_before=cursor_for(request.compiled_query, position=cursor),
                cursor_after=cursor_for(
                    request.compiled_query,
                    position=None if exhausted else next_cursor,
                    exhausted=exhausted,
                ),
                declared_total=meta.get("count"),
                record_locators=tuple(record["locator"] for record in records),
            )
            pages += 1
            if exhausted or (request.max_pages is not None and pages >= request.max_pages):
                return
            cursor = next_cursor

    def _keyed(self, params: dict[str, Any]) -> dict[str, Any]:
        # OpenAlex reads its key from the query string only; `_redact` strips it
        # from every URL this connector stores.
        return {**params, "api_key": self.api_key} if self.api_key else params

    def _explain(self, error: LabAssistantError) -> LabAssistantError:
        """Say what to configure when OpenAlex refuses a keyless request.

        OpenAlex serves the first page of a search without a key and refuses the
        cursor for the second, so a keyless run stops after one page with a bare
        401 that reads like a revoked credential.
        """
        if self.api_key or error.error_class is not ErrorClass.AUTHENTICATION_REQUIRED:
            return error
        return LabAssistantError(
            "OpenAlex requires an API key to page past the first page of results; "
            "set LAB_OPENALEX_API_KEY (a free key from https://openalex.org/settings/api)",
            context=error.context,
        ).with_class(ErrorClass.AUTHENTICATION_REQUIRED)

    @staticmethod
    def _record(result: dict[str, Any]) -> dict[str, Any]:
        authority = "aggregator"
        assertions: list[dict[str, Any]] = []
        openalex_id = result.get("id")
        if openalex_id:
            assertions.append(
                identifier_assertion(IdentifierScheme.OPENALEX, openalex_id, authority=authority)
            )
        doi = result.get("doi")
        if doi:
            assertions.append(identifier_assertion(IdentifierScheme.DOI, doi, authority=authority))
        if result.get("title"):
            assertions.append(assertion(Predicate.TITLE, result["title"], authority=authority))
        if (year := _year(result.get("publication_year"))) is not None:
            assertions.append(assertion(Predicate.YEAR, year, authority=authority))
        if result.get("type"):
            assertions.append(
                assertion(Predicate.RESOURCE_KIND, result["type"], authority=authority)
            )
        location = result.get("primary_location") or {}
        source = location.get("source") or {}
        if source.get("display_name"):
            assertions.append(
                assertion(Predicate.JOURNAL, source["display_name"], authority=authority)
            )
        if location.get("license"):
            assertions.append(
                assertion(Predicate.LICENCE, location["license"], authority=authority)
            )
        best_oa = result.get("best_oa_location") or {}
        if best_oa.get("pdf_url"):
            assertions.append(
                assertion(Predicate.FULL_TEXT_LOCATION, best_oa["pdf_url"], authority=authority)
            )
        for concept in (result.get("concepts") or [])[:20]:
            if concept.get("display_name"):
                assertions.append(
                    assertion(Predicate.SUBJECT_TERM, concept["display_name"], authority=authority)
                )
        locator = f"doi:{doi}" if doi else f"openalex:{openalex_id}"
        return {"locator": locator, "assertions": assertions}

    async def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]:
        for identifier in identifiers:
            path = (
                f"works/doi:{identifier.value}"
                if identifier.scheme is IdentifierScheme.DOI
                else f"works/{identifier.value}"
            )
            response = await self._get(f"{self.base_url}/{path}", self._keyed({}))
            record = self._record(response.json())
            compiled = CompiledQuery(
                connector_id=self.connector_id,
                connector_version=self.connector_version,
                query_text=path,
                query_fingerprint=compile_query_fingerprint(
                    connector_id=self.connector_id,
                    connector_version=self.connector_version,
                    query=path,
                    parameters={},
                ),
            )
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=json.dumps({"records": [record]}, sort_keys=True).encode(),
                ),
                cursor_before=cursor_for(compiled, position=None),
                cursor_after=cursor_for(compiled, position=None, exhausted=True),
                record_locators=(record["locator"],),
            )

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        raise LabAssistantError("OpenAlex supplies metadata and locations, not content").with_class(
            ErrorClass.POLICY_BLOCKED
        )

    async def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]:
        raise LabAssistantError("no change feed configured for OpenAlex").with_class(
            ErrorClass.INVALID_QUERY
        )
        yield  # pragma: no cover


@dataclass
class DataCiteConnector(_HttpConnector):
    """DataCite: datasets, software, and reports — not literature."""

    connector_id: str = "datacite"
    connector_version: str = "1.0.0"
    base_url: str = "https://api.datacite.org"

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            resource_kinds=frozenset({"dataset", "software", "report", "thesis"}),
            operations=frozenset({ConnectorOperation.DISCOVER, ConnectorOperation.RESOLVE}),
            query_schema_id="datacite-query/1",
            output_schema_id="canonical-record/1",
            pagination_modes=frozenset({"page"}),
            supports_bulk_snapshot=False,
            supports_incremental_cursor=True,
            content_formats=frozenset({"application/json"}),
            identifier_schemes=frozenset({IdentifierScheme.DATACITE_DOI}),
            terms_url="https://support.datacite.org/docs/api",
            rate_policy=RatePolicy(requests_per_second=3.0, burst=3, max_concurrent=2),
            owner="platform",
        )

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery:
        query = str(scope_payload.get("query", "")).strip()
        if not query:
            raise LabAssistantError("DataCite scope entry has no query").with_class(
                ErrorClass.INVALID_QUERY
            )
        return CompiledQuery(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            query_text=query,
            parameters={},
            query_fingerprint=compile_query_fingerprint(
                connector_id=self.connector_id,
                connector_version=self.connector_version,
                query=query,
                parameters={},
            ),
        )

    async def discover(self, request: DiscoveryRequest) -> AsyncIterator[RawPage]:
        page_number = int(request.cursor.position or 1)
        pages = 0
        while True:
            params = {
                "query": request.compiled_query.query_text,
                "page[number]": page_number,
                "page[size]": min(request.page_size, 100),
            }
            response = await self._get(f"{self.base_url}/dois", params)
            payload = response.json()
            data = payload.get("data", [])
            total = (payload.get("meta") or {}).get("total")
            records = [self._record(item) for item in data]
            # A page envelope, not the source's bytes: `upstream_body` carries
            # those so provenance and replay stay separable.
            body = json.dumps({"records": records, "raw": payload}, sort_keys=True).encode()
            exhausted = not data or (total is not None and page_number * len(data) >= total)
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=body,
                    upstream_body=response.content,
                ),
                cursor_before=cursor_for(request.compiled_query, position=str(page_number)),
                cursor_after=cursor_for(
                    request.compiled_query,
                    position=None if exhausted else str(page_number + 1),
                    exhausted=exhausted,
                ),
                declared_total=int(total) if total is not None else None,
                record_locators=tuple(record["locator"] for record in records),
            )
            pages += 1
            if exhausted or (request.max_pages is not None and pages >= request.max_pages):
                return
            page_number += 1

    @staticmethod
    def _record(item: dict[str, Any]) -> dict[str, Any]:
        authority = "doi_registry"
        attributes = item.get("attributes", {})
        assertions: list[dict[str, Any]] = []
        doi = attributes.get("doi") or item.get("id")
        if doi:
            assertions.append(
                identifier_assertion(IdentifierScheme.DATACITE_DOI, doi, authority=authority)
            )
        titles = attributes.get("titles") or []
        if titles:
            assertions.append(
                assertion(Predicate.TITLE, titles[0].get("title"), authority=authority)
            )
        types = attributes.get("types") or {}
        if types.get("resourceTypeGeneral"):
            assertions.append(
                assertion(
                    Predicate.RESOURCE_KIND,
                    types["resourceTypeGeneral"].lower(),
                    authority=authority,
                )
            )
        if (year := _year(attributes.get("publicationYear"))) is not None:
            assertions.append(assertion(Predicate.YEAR, year, authority=authority))
        if attributes.get("publisher"):
            assertions.append(
                assertion(Predicate.PUBLISHER, attributes["publisher"], authority=authority)
            )
        for right in attributes.get("rightsList") or []:
            if right.get("rightsIdentifier") or right.get("rights"):
                assertions.append(
                    assertion(
                        Predicate.LICENCE,
                        right.get("rightsIdentifier") or right.get("rights"),
                        authority=authority,
                    )
                )
        return {"locator": f"doi:{doi}", "assertions": assertions}

    async def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]:
        for identifier in identifiers:
            response = await self._get(f"{self.base_url}/dois/{identifier.value}", {})
            record = self._record(response.json().get("data", {}))
            compiled = await self.compile_query({"query": f"doi:{identifier.value}"})
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=json.dumps({"records": [record]}, sort_keys=True).encode(),
                ),
                cursor_before=cursor_for(compiled, position=None),
                cursor_after=cursor_for(compiled, position=None, exhausted=True),
                record_locators=(record["locator"],),
            )

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        raise LabAssistantError("DataCite supplies metadata, not content").with_class(
            ErrorClass.POLICY_BLOCKED
        )

    async def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]:
        raise LabAssistantError("no change feed configured for DataCite").with_class(
            ErrorClass.INVALID_QUERY
        )
        yield  # pragma: no cover


@dataclass
class UnpaywallResolver(_HttpConnector):
    """Open-access location resolution.

    It resolves *locations*. It is not a work-identity source, and a resolved URL
    is not permission: the rights policy still decides.
    """

    connector_id: str = "unpaywall"
    connector_version: str = "1.0.0"
    base_url: str = "https://api.unpaywall.org/v2"
    contact: str | None = None
    #: Hosts an automated fetch may use. Anything else needs a steward decision.
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            resource_kinds=frozenset({"article"}),
            operations=frozenset({ConnectorOperation.RESOLVE, ConnectorOperation.ACQUIRE}),
            query_schema_id=None,
            output_schema_id="oa-location/1",
            pagination_modes=frozenset(),
            supports_bulk_snapshot=False,
            supports_incremental_cursor=False,
            content_formats=frozenset({"application/pdf", "application/xml"}),
            identifier_schemes=frozenset({IdentifierScheme.DOI}),
            terms_url="https://unpaywall.org/api",
            rate_policy=RatePolicy(requests_per_second=5.0, burst=5, max_concurrent=2),
            required_secret_names=frozenset({"UNPAYWALL_EMAIL"}),
            owner="platform",
        )

    async def compile_query(self, scope_payload: dict[str, Any]) -> CompiledQuery:
        raise LabAssistantError("Unpaywall does not perform discovery").with_class(
            ErrorClass.INVALID_QUERY
        )

    async def discover(self, request: DiscoveryRequest) -> AsyncIterator[RawPage]:
        raise LabAssistantError("Unpaywall does not perform discovery").with_class(
            ErrorClass.INVALID_QUERY
        )
        yield  # pragma: no cover

    async def resolve(self, identifiers: list[Identifier]) -> AsyncIterator[RawPage]:
        if not self.contact:
            raise LabAssistantError(
                "Unpaywall requires a contact address for every request"
            ).with_class(ErrorClass.AUTHENTICATION_REQUIRED)
        for identifier in identifiers:
            if identifier.scheme is not IdentifierScheme.DOI:
                continue
            response = await self._get(
                f"{self.base_url}/{identifier.value}", {"email": self.contact}
            )
            payload = response.json()
            locations = [payload.get("best_oa_location"), *(payload.get("oa_locations") or [])]
            assertions = [
                assertion(
                    Predicate.FULL_TEXT_LOCATION,
                    {
                        "url": location.get("url_for_pdf") or location.get("url"),
                        "host_type": location.get("host_type"),
                        "version": location.get("version"),
                        "licence": location.get("license"),
                    },
                    authority="aggregator",
                )
                for location in locations
                if location
            ]
            record = {"locator": f"doi:{identifier.value}", "assertions": assertions}
            compiled = CompiledQuery(
                connector_id=self.connector_id,
                connector_version=self.connector_version,
                query_text=f"oa:{identifier.value}",
                query_fingerprint=compile_query_fingerprint(
                    connector_id=self.connector_id,
                    connector_version=self.connector_version,
                    query=f"oa:{identifier.value}",
                    parameters={},
                ),
            )
            yield RawPage(
                response=make_response(
                    method="GET",
                    url_redacted=self._redact(str(response.url)),
                    status=response.status_code,
                    body=json.dumps({"records": [record], "raw": payload}, sort_keys=True).encode(),
                    upstream_body=response.content,
                ),
                cursor_before=cursor_for(compiled, position=None),
                cursor_after=cursor_for(compiled, position=None, exhausted=True),
                record_locators=(record["locator"],),
            )

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        """Fetch an open-access PDF from an allowlisted host.

        SSRF controls: the URL must be absolute https, its host must be
        allowlisted, and redirects are not followed to a different host.
        """
        target = request.target_url
        if not target:
            raise LabAssistantError("no open-access location was supplied").with_class(
                ErrorClass.NOT_FOUND
            )
        from urllib.parse import urlparse

        parsed = urlparse(target)
        if parsed.scheme != "https" or not parsed.hostname:
            raise LabAssistantError(f"refusing non-https location {target!r}").with_class(
                ErrorClass.POLICY_BLOCKED
            )
        if self.allowed_hosts and parsed.hostname not in self.allowed_hosts:
            raise LabAssistantError(
                f"host {parsed.hostname} is not on the acquisition allowlist"
            ).with_class(ErrorClass.POLICY_BLOCKED)
        client = self.client or httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        )
        owns = self.client is None
        try:
            response = await client.get(target, headers=self._headers())
        finally:
            if owns:
                await client.aclose()
        if response.status_code in (301, 302, 303, 307, 308):
            raise LabAssistantError(
                "redirect not followed; the redirect target must be revalidated by policy"
            ).with_class(ErrorClass.POLICY_BLOCKED)
        if response.status_code != 200:
            raise map_http_error(
                response.status_code, retry_after=response.headers.get("retry-after")
            )
        if len(response.content) > request.max_bytes:
            raise LabAssistantError(
                f"content of {len(response.content)} bytes exceeds the limit"
            ).with_class(ErrorClass.TOO_LARGE)
        return RawAcquisition(
            response=make_response(
                method="GET",
                url_redacted=self._redact(target),
                status=response.status_code,
                body=b"",
                headers={"content-type": response.headers.get("content-type", "")},
            ),
            content=response.content,
            declared_media_type=response.headers.get("content-type", "").split(";")[0] or None,
            access_basis="unpaywall_open_access_location",
            source_url_redacted=self._redact(target),
            embedded_identifiers=tuple(request.identifiers),
        )

    async def changes(self, cursor: Cursor) -> AsyncIterator[RawPage]:
        raise LabAssistantError("Unpaywall has no change feed here").with_class(
            ErrorClass.INVALID_QUERY
        )
        yield  # pragma: no cover
