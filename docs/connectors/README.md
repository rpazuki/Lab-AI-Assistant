Status: active
Owner: platform + connector owners
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/contracts/connector.py`

# Connector SDK

A connector is a **versioned adapter with declared capabilities**. An operation it does
not support is an absent capability, discovered at registration — not a runtime
surprise on a Friday.

## The contract

```python
class SourceConnector(Protocol):
    connector_id: str
    connector_version: str

    @property
    def capabilities(self) -> ConnectorCapabilities: ...
    async def compile_query(self, scope_payload: dict) -> CompiledQuery: ...
    async def discover(self, request: DiscoveryRequest) -> AsyncIterator[RawPage]: ...
    async def resolve(self, identifiers: Sequence[Identifier]) -> RawResponse: ...
    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition: ...
```

`discover` yields pages of **raw** payloads with a cursor. It does not normalise, dedupe,
decide relevance, or write to the database. A connector that interprets its own output
makes a parsing bug unfixable without re-querying the source.

## Declared capabilities

| Field | Why it must be declared |
|---|---|
| `resource_kinds` | article, review, preprint, dataset, thesis |
| `operations` | discover, resolve, acquire, delta |
| `query_schema_id`, `output_schema_id` | versioned shapes, so a change is visible |
| `pagination_modes` | `retstart`, `cursor`, `page`, `token` |
| `supports_bulk_snapshot`, `supports_incremental_cursor` | whether a delta run is possible at all |
| `content_formats` | media types it can return |
| `identifier_schemes` | which identifiers it can assert |
| `terms_url` | the source's own terms — linked, never paraphrased |
| `rate_policy` | requests per second, burst, max concurrent |
| `data_classification` | `public_metadata`, `licensed_content`, `internal` |
| `required_secret_names` | names only; never values |
| `owner` | who is called when it breaks |

## Rate policy lives in versioned config, and is enforced

`RatePolicy` values come from connector policy records, and the connector's own page
links the source's current official guidance. Hard-coding "3 requests per second" into
prose guarantees the prose is wrong within a year and the code is wrong forever.

The policy is **enforced per request** by a stored token bucket, not by the connector
itself: discovery acquires a slot before each page, the row is locked while it is
spent, and an upstream `Retry-After` holds every worker off rather than only the one
that saw it (ADR-0018). A declared limit nobody acquires against is documentation.

## What a deployment actually has

`live_connector_registry(settings)` builds the registry for the API, the workers, and
the CLI from one place, so all three agree. Fixture-keyed fakes are never registered
there — they are passed in explicitly by tests and the demo, so nothing silently
substitutes a fake for a source.

```bash
lab config connectors
```

Prints each registered connector with its version, operations, identifier schemes,
rate policy, and **whether** a credential is configured — never the credential. That
output is safe to paste into a support bundle, and it answers the first question after
an `UnknownConnector` or an `authentication_required` outcome.

A connector can be absent for a stated reason: Unpaywall's terms require a contact
address on every request, so without `LAB_CONNECTOR_CONTACT` it is not registered and
the summary says so. Registering it anyway would produce 4xx responses that read like
an outage.

## Error mapping

`map_http_error` turns transport reality into the `ErrorClass` taxonomy: 429 with
`Retry-After` → `rate_limited` with the delay preserved; 401/403 →
`authentication_required`; 404 → `not_found`; 5xx → `source_unavailable`; a timeout →
`timeout`. A connector never raises a bare `httpx` exception into the application layer.

## Testing a connector

Two fakes, both **fixture-keyed and raising on a miss**:

```python
FakeConnector(pages={("query", None): [...]})  # a request with no fixture raises FixtureMiss
```

A permissive fake that returns an empty page on an unexpected request makes every test
pass and proves nothing. That lesson is inherited from the predecessor and is enforced
by the adapter contract test, which every connector must pass: capability honesty,
pagination termination, error mapping, and idempotent re-discovery.

Live-source tests exist as a separate, opt-in nightly job. The default suite is
offline.

## Adding one

1. implement the protocol under `adapters/connectors/`;
2. declare capabilities honestly — including what it cannot do;
3. add fixtures and pass the adapter contract test;
4. add a row to the [source matrix](source-matrix.md) with the owner;
5. record the rate and terms in connector policy, linking the source's own page.

See also: [source matrix](source-matrix.md) ·
[source observations](../data-contracts/source-observations.md)
