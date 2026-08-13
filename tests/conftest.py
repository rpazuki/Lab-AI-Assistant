"""Shared fixtures.

Unit and integration tests run entirely offline: in-memory repositories, an
in-memory object store, fixture-keyed fake connectors, a fake model provider, and
a fixed clock. Nothing here reads a shared checkpoint directory, a home directory,
real credentials, or the network — the predecessor had a test that failed because
it read a production PubMed checkpoint from ``./data/checkpoints``.

Tests that need real PostgreSQL are marked ``postgres`` and skip unless
``LAB_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import pytest

from lab_assistant.adapters.connectors.fake import FakeAcquisitionConnector
from lab_assistant.application.accounts import AccountService
from lab_assistant.application.context import ActorContext
from lab_assistant.contracts.policy import (
    Effect,
    GrantSpec,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import (
    Harness,
    build_harness,
    fake_source,
    make_actor,
    record,
    sample_jats,
    source_page,
)

pytest_plugins: tuple[str, ...] = ()

SCOPE_QUERY = "yarrowia lipolytica lipid production"


@pytest.fixture
def jats_bytes() -> bytes:
    return sample_jats()


@pytest.fixture
def fixture_records() -> list[dict[str, Any]]:
    """Two sources describing overlapping works, plus one preprint."""
    return [
        record(
            locator="doi:10.1234/example",
            identifiers=[("doi", "10.1234/example"), ("pmid", "40000001")],
            title="Lipid production in an oleaginous yeast",
            abstract="We report lipid production in two engineered strains of Yarrowia lipolytica.",
            year=2025,
            journal="Journal of Applied Microbiology",
            licence="cc-by-4.0",
            full_text_location="pmc:PMC9000001",
            subject_terms=["Yarrowia lipolytica", "lipid production"],
            resource_kind="journal-article",
        ),
        record(
            locator="doi:10.5555/other",
            identifiers=[("doi", "10.5555/other")],
            title="Microbial community dynamics in a bioreactor",
            abstract="Community composition shifted under nitrogen limitation.",
            year=2024,
            journal="Bioresource Reports",
            licence="cc-by-4.0",
            subject_terms=["microbial community"],
            resource_kind="journal-article",
        ),
    ]


@pytest.fixture
def harness(fixture_records: list[dict[str, Any]], jats_bytes: bytes) -> Harness:
    """A harness with two discovery sources and one acquirable asset."""
    pubmed = fake_source(
        "pubmed-fixture",
        [source_page(query=SCOPE_QUERY, records=fixture_records)],
    )
    europepmc = fake_source(
        "europepmc-fixture",
        [
            source_page(
                query=SCOPE_QUERY,
                records=[
                    record(
                        locator="doi:10.1234/example",
                        identifiers=[("doi", "10.1234/example"), ("pmcid", "PMC9000001")],
                        title="Lipid production in an oleaginous yeast",
                        journal="J Appl Microbiol",
                        licence="cc-by-4.0",
                        full_text_location="pmc:PMC9000001",
                        authority="doi_registry",
                    )
                ],
            )
        ],
    )
    acquirer = FakeAcquisitionConnector(
        assets={
            "doi:10.1234/example": (jats_bytes, "application/xml", "cc-by-4.0"),
            "pmcid:PMC9000001": (jats_bytes, "application/xml", "cc-by-4.0"),
        }
    )
    return build_harness(
        connectors={
            "pubmed-fixture": pubmed,
            "europepmc-fixture": europepmc,
            "fake-acquire": acquirer,
        }
    )


@pytest.fixture
async def provisioned(harness: Harness) -> dict[str, Any]:
    """A workspace with a steward, a researcher, and query grants."""
    accounts: AccountService = harness.accounts
    steward = await accounts.create_user(
        email="steward@lab.invalid", password="correct horse battery"
    )
    researcher = await accounts.create_user(
        email="researcher@lab.invalid", password="correct horse battery"
    )
    workspace, _membership = await accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=steward.id
    )
    steward_actor = make_actor(
        user_id=steward.id, workspace_id=workspace.id, role=Role.ADMIN
    )
    await accounts.add_member(steward_actor, user_id=researcher.id, role=Role.RESEARCHER)

    grants: list[GrantSpec] = []
    for action in (
        PolicyAction.QUERY,
        PolicyAction.VIEW_CONTENT,
        PolicyAction.DISPLAY_EXCERPT,
        PolicyAction.DISPLAY_FULL_CONTENT,
        PolicyAction.EXPORT_CLAIMS,
        PolicyAction.EXPORT_EVIDENCE,
        PolicyAction.EXPORT_METADATA,
    ):
        grants.append(
            await accounts.grant(
                steward_actor,
                subject_type=SubjectType.WORKSPACE,
                subject_id=workspace.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace.id,
                action=action,
                effect=Effect.ALLOW,
                policy_basis="workspace_open_corpus",
            )
        )

    def actor_for(user_id: UUID, role: Role) -> ActorContext:
        return make_actor(
            user_id=user_id,
            workspace_id=workspace.id,
            role=role,
            grants=tuple(grants),
        )

    return {
        "workspace": workspace,
        "steward": steward,
        "researcher": researcher,
        "steward_actor": actor_for(steward.id, Role.ADMIN),
        "researcher_actor": actor_for(researcher.id, Role.RESEARCHER),
        "grants": tuple(grants),
        "actor_for": actor_for,
    }


@pytest.fixture
def postgres_url() -> Iterator[str]:
    url = os.environ.get("LAB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("LAB_TEST_DATABASE_URL is not set; PostgreSQL integration tests skipped")
    yield url


@pytest.fixture
async def unused_async_iterator() -> AsyncIterator[None]:  # pragma: no cover
    """Placeholder that keeps pytest-asyncio's plugin loaded for this package."""
    yield None
