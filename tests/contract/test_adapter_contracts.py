"""Adapter contracts.

Fixture-keyed fakes, object-store immutability, provider sampling gating,
structured-output truncation, connector capability manifests, and export package
validation. These are the promises the rest of the suite leans on.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from lab_assistant.adapters.connectors.base import ConnectorRegistryImpl
from lab_assistant.adapters.connectors.fake import (
    FakeAcquisitionConnector,
    FixtureMiss,
    load_fixture_pages,
)
from lab_assistant.adapters.connectors.scholarly import (
    CrossrefConnector,
    DataCiteConnector,
    EuropePmcConnector,
    OpenAlexConnector,
    PubMedConnector,
    UnpaywallResolver,
    map_http_error,
)
from lab_assistant.adapters.models.anthropic_provider import AnthropicProvider, sampling_params
from lab_assistant.adapters.models.fake import FakeLLMProvider, HashingEmbeddingProvider
from lab_assistant.adapters.object_store.filesystem import (
    FilesystemObjectStore,
    InMemoryObjectStore,
)
from lab_assistant.adapters.security import StubScanner
from lab_assistant.application.registries import UnknownConnector
from lab_assistant.contracts.connector import (
    AcquisitionRequest,
    ConnectorOperation,
    DiscoveryRequest,
    Identifier,
    IdentifierScheme,
)
from lab_assistant.contracts.errors import ErrorClass
from lab_assistant.contracts.models import GenerationRequest, StructuredRequest
from lab_assistant.testing import fake_source, record, source_page

WORKSPACE = uuid4()


class TestFakesRaiseOnAMiss:
    """The single most important test-infrastructure property.

    A fake that answers an unmatched request with a default makes an end-to-end
    suite pass while proving nothing. Both fakes raise instead.
    """

    async def test_unmatched_query_raises(self) -> None:
        connector = fake_source(
            "src", [source_page(query="known", records=[record(locator="a", identifiers=[])])]
        )
        compiled = await connector.compile_query({"query": "unknown"})
        with pytest.raises(FixtureMiss, match="no fixture page"):
            async for _page in connector.discover(
                DiscoveryRequest(compiled_query=compiled, cursor=_cursor(compiled))
            ):
                pass

    async def test_unmatched_cursor_raises(self) -> None:
        connector = fake_source(
            "src",
            [
                source_page(
                    query="known",
                    records=[record(locator="a", identifiers=[])],
                    cursor_out="2",
                )
            ],
        )
        compiled = await connector.compile_query({"query": "known"})
        with pytest.raises(FixtureMiss, match="cursor"):
            async for _page in connector.discover(
                DiscoveryRequest(compiled_query=compiled, cursor=_cursor(compiled))
            ):
                pass

    async def test_unmatched_identifier_raises_rather_than_returning_some_pdf(self) -> None:
        connector = FakeAcquisitionConnector(
            assets={"doi:10.1/known": (b"%PDF-1.7", "application/pdf", "cc-by-4.0")}
        )
        with pytest.raises(FixtureMiss, match="no fixture asset"):
            await connector.acquire(
                AcquisitionRequest(
                    identifiers=(Identifier(scheme=IdentifierScheme.DOI, value="10.1/other"),),
                    route="open_repository",
                )
            )

    async def test_the_miss_message_lists_what_was_recorded(self) -> None:
        connector = fake_source("src", [source_page(query="known", records=[])])
        compiled = await connector.compile_query({"query": "typo"})
        with pytest.raises(FixtureMiss) as caught:
            async for _page in connector.discover(
                DiscoveryRequest(compiled_query=compiled, cursor=_cursor(compiled))
            ):
                pass
        assert "known" in str(caught.value)

    async def test_fake_model_provider_raises_on_an_unknown_schema(self) -> None:
        provider = FakeLLMProvider(structured_fixtures={"known": {"ok": True}})
        with pytest.raises(Exception, match="no structured fixture"):
            await provider.structured(
                StructuredRequest(
                    system_prompt="s",
                    user_prompt="u",
                    json_schema={"type": "object"},
                    schema_name="unknown",
                )
            )


def _cursor(compiled):
    from lab_assistant.contracts.connector import Cursor

    return Cursor(
        connector_id=compiled.connector_id, query_fingerprint=compiled.query_fingerprint
    )


class TestConnectorRegistry:
    def test_unknown_connector_raises_and_names_what_exists(self) -> None:
        registry = ConnectorRegistryImpl(
            connectors={"pubmed": PubMedConnector(request_identity="t")}
        )
        with pytest.raises(UnknownConnector, match="pubmed"):
            registry.get("europepmc")

    def test_operations_filter_by_declared_capability(self) -> None:
        registry = ConnectorRegistryImpl(
            connectors={
                "pubmed": PubMedConnector(request_identity="t"),
                "europepmc": EuropePmcConnector(request_identity="t"),
            }
        )
        assert registry.list_ids(operation=ConnectorOperation.ACQUIRE) == ("europepmc",)
        assert registry.list_ids(operation=ConnectorOperation.DISCOVER) == (
            "europepmc",
            "pubmed",
        )


class TestConnectorCapabilityManifests:
    CONNECTORS = (
        PubMedConnector(request_identity="t"),
        EuropePmcConnector(request_identity="t"),
        CrossrefConnector(request_identity="t"),
        OpenAlexConnector(request_identity="t"),
        DataCiteConnector(request_identity="t"),
        UnpaywallResolver(request_identity="t", contact="ops@lab.invalid"),
    )

    @pytest.mark.parametrize("connector", CONNECTORS, ids=lambda c: c.connector_id)
    def test_manifest_matches_the_implementation(self, connector) -> None:
        capabilities = connector.capabilities
        assert capabilities.connector_id == connector.connector_id
        assert capabilities.terms_url.startswith("https://")
        assert capabilities.output_schema_id
        assert capabilities.rate_policy.requests_per_second > 0
        # Declared identifier schemes must be real schemes.
        for scheme in capabilities.identifier_schemes:
            assert isinstance(scheme, IdentifierScheme)

    @pytest.mark.parametrize("connector", CONNECTORS, ids=lambda c: c.connector_id)
    async def test_unsupported_operations_fail_loudly(self, connector) -> None:
        capabilities = connector.capabilities
        if ConnectorOperation.ACQUIRE not in capabilities.operations:
            with pytest.raises(Exception):  # noqa: B017 - any refusal is acceptable
                await connector.acquire(
                    AcquisitionRequest(identifiers=(), route="open_repository")
                )

    def test_no_connector_declares_a_secret_value(self) -> None:
        for connector in self.CONNECTORS:
            for name in connector.capabilities.required_secret_names:
                assert name.isupper(), f"{name} looks like a value, not an env var name"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, ErrorClass.RATE_LIMITED),
            (403, ErrorClass.AUTHENTICATION_REQUIRED),
            (404, ErrorClass.NOT_FOUND),
            (400, ErrorClass.INVALID_QUERY),
            (503, ErrorClass.SOURCE_UNAVAILABLE),
        ],
    )
    def test_http_status_maps_onto_the_taxonomy(self, status: int, expected: ErrorClass) -> None:
        assert map_http_error(status).error_class is expected

    def test_retry_after_is_carried_into_the_error_context(self) -> None:
        error = map_http_error(429, retry_after="30")
        assert error.context["retry_after_seconds"] == 30


class TestObjectStoreImmutability:
    async def test_same_bytes_register_once(self) -> None:
        store = InMemoryObjectStore()
        first = await store.put(b"payload", workspace_id=WORKSPACE, object_class="asset")
        second = await store.put(b"payload", workspace_id=WORKSPACE, object_class="asset")
        assert first.id == second.id
        assert first.sha256 == second.sha256

    async def test_key_encodes_only_the_digest(self) -> None:
        store = InMemoryObjectStore()
        stored = await store.put(b"payload", workspace_id=WORKSPACE, object_class="asset")
        assert stored.key.startswith("sha256/")
        assert stored.sha256 in stored.key
        # No DOI, filename, or run directory in the key.
        assert "10." not in stored.key

    async def test_corruption_is_detected(self) -> None:
        store = InMemoryObjectStore()
        stored = await store.put(b"payload", workspace_id=WORKSPACE, object_class="asset")
        assert await store.verify(stored.id)
        store.corrupt_for_test(stored.id, b"tampered")
        assert not await store.verify(stored.id)

    async def test_workspaces_do_not_share_object_ids(self) -> None:
        store = InMemoryObjectStore()
        left = await store.put(b"payload", workspace_id=uuid4(), object_class="asset")
        right = await store.put(b"payload", workspace_id=uuid4(), object_class="asset")
        assert left.id != right.id
        assert left.sha256 == right.sha256

    async def test_tombstoned_object_is_unreadable_but_recorded(self) -> None:
        store = InMemoryObjectStore()
        stored = await store.put(b"payload", workspace_id=WORKSPACE, object_class="asset")
        await store.tombstone(stored.id, reason="rights_revocation")
        with pytest.raises(FileNotFoundError):
            await store.get(stored.id)
        head = await store.head(stored.id)
        assert head is not None and head.tombstoned

    async def test_filesystem_store_survives_a_restart(self, tmp_path) -> None:
        store = FilesystemObjectStore(tmp_path)
        stored = await store.put(b"payload", workspace_id=WORKSPACE, object_class="rendition")
        reopened = FilesystemObjectStore(tmp_path)
        assert await reopened.get(stored.id) == b"payload"
        assert await reopened.verify(stored.id)

    async def test_signed_url_is_scoped_and_never_a_filesystem_path(self, tmp_path) -> None:
        store = FilesystemObjectStore(tmp_path)
        stored = await store.put(b"payload", workspace_id=WORKSPACE, object_class="asset")
        url = await store.signed_url(stored.id, expires_seconds=60)
        assert str(tmp_path) not in url
        assert str(stored.id) in url


class TestProviderContract:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("claude-sonnet-4-6", {"temperature": 0.1}),
            ("claude-opus-5", {}),
            ("claude-sonnet-5", {}),
            ("claude-fable-5", {}),
        ],
    )
    def test_sampling_parameters_are_model_gated(self, model: str, expected: dict) -> None:
        """Newer models reject temperature with a 400 rather than ignoring it."""
        assert sampling_params(model, temperature=0.1) == expected

    def test_no_temperature_means_no_parameter(self) -> None:
        assert sampling_params("claude-sonnet-4-6", temperature=None) == {}

    def test_declared_properties_follow_the_model(self) -> None:
        opus = AnthropicProvider(api_key="x", model="claude-opus-5")
        assert opus.properties.accepted_sampling_params == frozenset()
        assert AnthropicProvider(
            api_key="x", model="claude-sonnet-4-6"
        ).properties.accepted_sampling_params == frozenset({"temperature"})

    async def test_truncated_structured_output_is_not_parsed_data(self) -> None:
        provider = FakeLLMProvider(
            structured_fixtures={"claims": {"proposals": [{"target_id": "a"}]}},
            truncate_after_chars=5,
        )
        result = await provider.structured(
            StructuredRequest(
                system_prompt="s",
                user_prompt="u",
                json_schema={"type": "object"},
                schema_name="claims",
            )
        )
        assert result.truncated
        assert result.data is None

    async def test_refusal_is_a_first_class_outcome(self) -> None:
        provider = FakeLLMProvider(refuse=True, structured_fixtures={"claims": {}})
        result = await provider.structured(
            StructuredRequest(
                system_prompt="s",
                user_prompt="u",
                json_schema={"type": "object"},
                schema_name="claims",
            )
        )
        assert result.refused and result.data is None

    async def test_generator_only_cites_supplied_handles(self) -> None:
        from lab_assistant.contracts.models import EvidenceBlock

        provider = FakeLLMProvider()
        request = GenerationRequest(
            system_prompt="s",
            prompt_bundle_id="b",
            prompt_bundle_version="1",
            question="q",
            evidence=(EvidenceBlock(handle="E0123456789", text="Yield was 0.42 g/g."),),
        )
        result = await provider.generate(request)
        assert result.cited_handles == ("E0123456789",)

    async def test_invented_handle_is_reproducible_for_tests(self) -> None:
        from lab_assistant.contracts.models import EvidenceBlock

        provider = FakeLLMProvider(invent_handle="Edeadbeef01")
        result = await provider.generate(
            GenerationRequest(
                system_prompt="s",
                prompt_bundle_id="b",
                prompt_bundle_version="1",
                question="q",
                evidence=(EvidenceBlock(handle="E0123456789", text="Yield was 0.42 g/g."),),
            )
        )
        assert "Edeadbeef01" in result.cited_handles

    async def test_streaming_and_generation_agree(self) -> None:
        from lab_assistant.contracts.models import EvidenceBlock

        provider = FakeLLMProvider()
        request = GenerationRequest(
            system_prompt="s",
            prompt_bundle_id="b",
            prompt_bundle_version="1",
            question="q",
            evidence=(EvidenceBlock(handle="E0123456789", text="Yield was 0.42 g/g."),),
        )
        streamed = "".join([token async for token in provider.stream(request)])
        assert streamed == (await provider.generate(request)).text

    async def test_embedder_is_deterministic_and_normalised(self) -> None:
        embedder = HashingEmbeddingProvider(dimensions=32)
        first = await embedder.embed_query("lipid yield")
        second = await embedder.embed_query("lipid yield")
        assert list(first) == list(second)
        assert len(first) == 32
        assert abs(sum(value * value for value in first) - 1.0) < 1e-9


class TestScannerContract:
    async def test_unavailable_scanner_never_reports_clean(self) -> None:
        result = await StubScanner(available=False).scan(b"payload")
        assert not result.clean
        assert result.detail == "scanner unavailable"

    async def test_known_marker_is_detected(self) -> None:
        result = await StubScanner().scan(b"prefix <script>evil suffix")
        assert not result.clean
        assert result.signature_version


class TestFixtureLoading:
    def test_pages_load_from_json(self, tmp_path) -> None:
        path = tmp_path / "pages.json"
        path.write_text(
            '{"pages": [{"query": "q", "cursor_in": null, "cursor_out": null, '
            '"body": {"records": []}, "declared_total": 0}]}',
            encoding="utf-8",
        )
        pages = load_fixture_pages(path)
        assert len(pages) == 1
        assert pages[0].exhausted
