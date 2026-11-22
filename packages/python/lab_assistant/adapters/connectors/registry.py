"""Which connectors a deployment actually has.

The composition root asks for a registry; this module decides what goes in it.
Keeping the decision here rather than in the API container means the CLI, the
workers, and the API all get the same set, and a test can assert the production
set without starting FastAPI.

Two rules:

* **A deployment gets the live connectors.** An empty registry means every real
  discovery run fails with ``UnknownConnector`` at the first scope entry — a
  failure that looks like a configuration mistake and is actually a missing wire.
* **Fakes are never registered here.** Fixture-keyed fakes belong to tests, the
  demo, and explicit local fixtures, and they are handed in through
  ``extra_connectors`` so nothing silently substitutes a fake for a source.
"""

from __future__ import annotations

from lab_assistant.adapters.connectors.base import ConnectorRegistryImpl
from lab_assistant.adapters.connectors.scholarly import (
    CrossrefConnector,
    DataCiteConnector,
    EuropePmcConnector,
    OpenAlexConnector,
    PubMedConnector,
    UnpaywallResolver,
)
from lab_assistant.config import Settings
from lab_assistant.contracts.connector import SourceConnector

__all__ = ["LIVE_CONNECTOR_IDS", "live_connector_registry", "registry_summary"]

#: Every connector a deployment can reach. An id absent from a built registry is
#: absent for a stated reason (see ``registry_summary``), never by accident.
LIVE_CONNECTOR_IDS: tuple[str, ...] = (
    "pubmed",
    "europepmc",
    "crossref",
    "openalex",
    "datacite",
    "unpaywall",
)


def live_connector_registry(
    settings: Settings,
    *,
    extra_connectors: dict[str, SourceConnector] | None = None,
) -> ConnectorRegistryImpl:
    """Build the registry for this deployment.

    ``extra_connectors`` is how a demo or a fixture-backed integration test adds
    deterministic connectors. It is applied last, so a local fixture can shadow a
    live id deliberately — but only because a caller asked for it.
    """
    identity = settings.connector_request_identity
    contact = settings.connector_contact

    connectors: dict[str, SourceConnector] = {
        "pubmed": PubMedConnector(
            request_identity=identity,
            contact=contact,
            # The key raises PubMed's rate ceiling. Its presence changes declared
            # capabilities, which is why it is passed rather than read globally.
            api_key=settings.ncbi_api_key,
        ),
        "europepmc": EuropePmcConnector(request_identity=identity, contact=contact),
        "crossref": CrossrefConnector(request_identity=identity, contact=contact),
        "openalex": OpenAlexConnector(
            request_identity=identity,
            contact=contact,
            # Required to page past the first page of results.
            api_key=settings.openalex_api_key,
        ),
        "datacite": DataCiteConnector(request_identity=identity, contact=contact),
    }

    # Unpaywall's terms require a contact address on every request. Registering it
    # without one would produce 4xx responses that read like an outage, so it is
    # absent with a reason instead.
    if contact:
        connectors["unpaywall"] = UnpaywallResolver(request_identity=identity, contact=contact)

    if extra_connectors:
        connectors.update(extra_connectors)
    return ConnectorRegistryImpl(connectors=connectors)


def registry_summary(registry: ConnectorRegistryImpl) -> list[dict[str, object]]:
    """What is registered, for `lab config connectors` and the support bundle.

    Ids, versions, operations, and whether a credential is configured — never the
    credential itself. An operator diagnosing a failed discovery run needs to know
    that `pubmed` is registered and keyless, not what the key is.
    """
    summary: list[dict[str, object]] = []
    for connector_id in registry.list_ids():
        capabilities = registry.capabilities(connector_id)
        summary.append(
            {
                "connector_id": capabilities.connector_id,
                "connector_version": capabilities.connector_version,
                "operations": sorted(operation.value for operation in capabilities.operations),
                "identifier_schemes": sorted(
                    scheme.value for scheme in capabilities.identifier_schemes
                ),
                "requests_per_second": capabilities.rate_policy.requests_per_second,
                "max_concurrent": capabilities.rate_policy.max_concurrent,
                "required_secret_names": sorted(capabilities.required_secret_names),
                "credential_configured": bool(getattr(registry.get(connector_id), "api_key", None)),
                "data_classification": capabilities.data_classification,
                "owner": capabilities.owner,
            }
        )
    registered = set(registry.list_ids())
    missing = [
        connector_id for connector_id in LIVE_CONNECTOR_IDS if connector_id not in registered
    ]
    if missing:
        summary.append({"absent": missing, "reason": "no LAB_CONNECTOR_CONTACT configured"})
    return summary
