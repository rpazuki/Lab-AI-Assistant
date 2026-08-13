"""What a deployment actually gets when it starts.

The audit found three wires missing in the composition root: no connectors, no
malware scanner, and the S3 setting selecting a local directory anyway. Each was
invisible from inside the test suite, because the tests build their own container.
These assertions are about the *production* one, and they run offline: constructing
an adapter is not the same as calling out to it.
"""

from __future__ import annotations

import pytest

from lab_assistant.adapters.connectors.registry import (
    LIVE_CONNECTOR_IDS,
    live_connector_registry,
    registry_summary,
)
from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
from lab_assistant.adapters.object_store.s3 import S3ObjectStore
from lab_assistant.adapters.security import ClamAvScanner
from lab_assistant.config import LabEnv, Settings
from lab_assistant.contracts.connector import ConnectorOperation
from lab_assistant.contracts.errors import LabAssistantError
from lab_assistant_api.deps import build_container


def _settings(**overrides) -> Settings:
    base = {
        "database_url": "memory://",
        "object_store_root": "/tmp/lab-assistant-container-test",
    }
    return Settings(**(base | overrides))


class TestConnectorWiring:
    def test_the_production_container_has_the_live_connectors(self) -> None:
        container = build_container(_settings())
        registered = container.context.connectors.list_ids()
        # An empty registry is what the audit found: every real scope entry would
        # have raised UnknownConnector at the first discovery run.
        for connector_id in ("pubmed", "europepmc", "crossref", "openalex", "datacite"):
            assert connector_id in registered

    def test_unpaywall_is_absent_without_a_contact_and_present_with_one(self) -> None:
        without = live_connector_registry(_settings())
        assert "unpaywall" not in without.list_ids()
        with_contact = live_connector_registry(_settings(connector_contact="ops@example.ac.uk"))
        assert "unpaywall" in with_contact.list_ids()
        # Absent for a stated reason, not silently: the summary says why.
        reasons = [row for row in registry_summary(without) if "absent" in row]
        assert reasons and "unpaywall" in reasons[0]["absent"]

    def test_no_fake_connector_reaches_a_deployment(self) -> None:
        registered = live_connector_registry(_settings()).list_ids()
        assert not [name for name in registered if "fake" in name or "fixture" in name]
        assert set(registered) <= set(LIVE_CONNECTOR_IDS)

    def test_a_discovery_scope_reaches_a_real_connector_without_any_network_call(self) -> None:
        """Registry lookup and capability declaration only — nothing is fetched."""
        registry = live_connector_registry(_settings())
        connector = registry.get("pubmed")
        assert connector.capabilities.supports(ConnectorOperation.DISCOVER)
        with pytest.raises(Exception):  # noqa: B017 - UnknownConnector, by any path
            registry.get("not-a-connector")

    def test_the_summary_is_safe_for_a_support_bundle(self) -> None:
        settings = _settings(ncbi_api_key="secret-key-value", connector_contact="ops@example.ac.uk")
        summary = registry_summary(live_connector_registry(settings))
        rendered = repr(summary)
        assert "secret-key-value" not in rendered
        pubmed = next(row for row in summary if row.get("connector_id") == "pubmed")
        # The operator learns a credential *is* configured, which is the question
        # after an authentication_required outcome.
        assert pubmed["credential_configured"] is True
        assert pubmed["requests_per_second"] > 0


class TestObjectStoreWiring:
    def test_filesystem_is_selected_by_the_setting(self) -> None:
        container = build_container(_settings(object_store_kind="filesystem"))
        assert isinstance(container.context.object_store, FilesystemObjectStore)

    def test_s3_is_selected_and_constructed(self) -> None:
        container = build_container(
            _settings(
                object_store_kind="s3",
                object_store_bucket="lab-objects",
                object_store_endpoint="https://s3.example.ac.uk",
                object_store_region="eu-west-2",
                object_store_access_key="AKIAEXAMPLE",
                object_store_secret_key="secret",
            )
        )
        assert isinstance(container.context.object_store, S3ObjectStore)

    def test_s3_without_configuration_refuses_at_startup(self) -> None:
        """Better a refused boot than a failed acquisition six hours later."""
        with pytest.raises(LabAssistantError) as raised:
            build_container(_settings(object_store_kind="s3"))
        assert "LAB_OBJECT_STORE_BUCKET" in str(raised.value.context["missing"])

    def test_an_unknown_kind_is_an_error_not_a_silent_default(self) -> None:
        with pytest.raises(ValueError, match="unknown LAB_OBJECT_STORE_KIND"):
            build_container(_settings(object_store_kind="gluster"))

    def test_production_still_refuses_a_local_directory(self) -> None:
        blockers = _settings(
            env=LabEnv.SERVER,
            secret_key="a" * 64,
            app_public_url="https://lab.example.ac.uk",
            cors_origins=("https://lab.example.ac.uk",),
            llm_provider="anthropic",
            object_store_kind="filesystem",
        ).production_blockers()
        assert any("object store is a local directory" in blocker for blocker in blockers)


class TestScannerWiring:
    def test_a_scanner_is_wired_when_scanning_is_required(self) -> None:
        container = build_container(_settings(malware_scan_required_for_uploads=True))
        assert isinstance(container.context.malware_scanner, ClamAvScanner)

    def test_no_scanner_when_every_scan_gate_is_staged_off(self) -> None:
        container = build_container(
            _settings(
                malware_scan_required_for_uploads=False,
                malware_scan_required_for_open_access=False,
            )
        )
        assert container.context.malware_scanner is None
