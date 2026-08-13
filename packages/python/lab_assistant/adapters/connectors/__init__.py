"""Source connectors."""

from lab_assistant.adapters.connectors.base import ConnectorRegistryImpl
from lab_assistant.adapters.connectors.fake import (
    FakeAcquisitionConnector,
    FakeSourceConnector,
    FixtureMiss,
    FixturePage,
    load_fixture_pages,
)
from lab_assistant.adapters.connectors.scholarly import (
    CrossrefConnector,
    DataCiteConnector,
    EuropePmcConnector,
    OpenAlexConnector,
    PubMedConnector,
    UnpaywallResolver,
)

__all__ = [
    "ConnectorRegistryImpl",
    "CrossrefConnector",
    "DataCiteConnector",
    "EuropePmcConnector",
    "FakeAcquisitionConnector",
    "FakeSourceConnector",
    "FixtureMiss",
    "FixturePage",
    "OpenAlexConnector",
    "PubMedConnector",
    "UnpaywallResolver",
    "load_fixture_pages",
]
