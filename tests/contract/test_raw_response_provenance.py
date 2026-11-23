"""Source bytes and replay envelopes are distinguishable.

``RawResponse`` claimed to be "exactly what the source returned", but several
connectors build a page body from more than one upstream call (PubMed's esearch then
efetch) and stored that envelope in the same field. The claim was therefore true
sometimes, which is worse than not making it: an auditor checking provenance cannot
tell which rows are source bytes.

Now the record says which it is, and the source's own bytes are stored as their own
object when they differ.
"""

from __future__ import annotations

from typing import Any

import pytest

from lab_assistant.adapters.connectors.base import make_response
from lab_assistant.contracts.connector import BodyKind
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import make_actor

UPSTREAM = b'{"esearchresult": {"idlist": ["40000001"], "count": "1"}}'
ENVELOPE = b'{"records": [{"locator": "pmid:40000001"}], "raw": {"count": "1"}}'


class TestRawResponseContract:
    def test_a_plain_response_is_upstream_bytes(self) -> None:
        response = make_response(
            method="GET", url_redacted="https://example.invalid/x", status=200, body=UPSTREAM
        )
        assert response.body_kind is BodyKind.UPSTREAM_BYTES
        assert response.upstream_body is None
        assert response.source_bytes == UPSTREAM

    def test_an_envelope_declares_itself_and_keeps_the_source_bytes(self) -> None:
        response = make_response(
            method="GET",
            url_redacted="https://example.invalid/x",
            status=200,
            body=ENVELOPE,
            upstream_body=UPSTREAM,
        )
        assert response.body_kind is BodyKind.REPLAY_ENVELOPE
        assert response.body == ENVELOPE
        assert response.source_bytes == UPSTREAM
        assert response.source_bytes != response.body


class TestDiscoveryStoresBoth:
    @pytest.fixture
    async def discovered(self, harness, provisioned) -> dict[str, Any]:
        from lab_assistant.application.discovery import DiscoveryService
        from lab_assistant.application.scopes import ScopeService
        from lab_assistant.domain.scope import ConnectorPlanEntry

        actor = provisioned["steward_actor"]
        scopes = ScopeService(harness.context)
        scope = await scopes.create_version(
            actor,
            scope_id=None,
            name="Yarrowia lipid production",
            scientific_objective="Lipid yields in engineered oleaginous yeast.",
            connector_plan=[
                ConnectorPlanEntry(
                    connector_id="pubmed-fixture",
                    connector_version="1.0.0",
                    query="yarrowia lipolytica lipid production",
                )
            ],
            intended_uses=("internal literature search",),
            concepts=("lipid production",),
        )
        approved = await scopes.approve(actor, scope_version_id=scope.id)
        run, tasks = await DiscoveryService(harness.context).start_run(
            actor, scope_version_id=approved.id
        )
        await harness.orchestrator.submit_requests(
            tasks,
            workspace_id=provisioned["workspace"].id,
            actor_id=actor.user_id,
            run_id=run.id,
        )
        await harness.run_all_tasks()
        async with harness.context.uow_factory() as uow:
            requests = await uow.observations.list_requests(provisioned["workspace"].id, run.id)
        return {"harness": harness, "requests": requests, "workspace": provisioned["workspace"]}

    async def test_a_fixture_page_is_recorded_as_upstream_bytes(self, discovered) -> None:
        """A fixture body *is* the source response, so the replay boundary holds."""
        assert discovered["requests"]
        request = discovered["requests"][0]
        assert request.response_body_kind == BodyKind.UPSTREAM_BYTES.value
        assert request.upstream_body_object_id is None
        assert request.upstream_sha256 is None
        # The stored object is readable and is what the digest says it is.
        payload = await discovered["harness"].context.object_store.get(
            request.response_body_object_id
        )
        from lab_assistant.contracts.canonical import sha256_bytes

        assert sha256_bytes(payload) == request.response_sha256

    async def test_an_envelope_is_stored_beside_its_source_bytes(self, harness) -> None:
        """The path a live connector takes: two objects, two digests, one page."""
        from lab_assistant.application.discovery import DiscoveryHandler
        from lab_assistant.contracts.canonical import sha256_bytes
        from lab_assistant.contracts.connector import Cursor, RawPage
        from lab_assistant.contracts.stage import StageCommand, StageName
        from lab_assistant.domain.observation import DiscoveryRun, DiscoveryRunStatus
        from lab_assistant.domain.scope import ScopeVersion

        owner = await harness.accounts.create_user(
            email="steward2@lab.invalid", password="a-long-passphrase"
        )
        workspace, _ = await harness.accounts.create_workspace(
            slug="envelopes", display_name="Envelopes", owner_user_id=owner.id
        )
        actor = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
        context = harness.context

        scope = ScopeVersion(
            id=context.new_id(),
            workspace_id=workspace.id,
            scope_id=context.new_id(),
            version=1,
            name="envelope scope",
            scientific_objective="objective",
            created_by=actor.user_id,
            created_at=context.now(),
        )
        run = DiscoveryRun(
            id=context.new_id(),
            workspace_id=workspace.id,
            scope_version_id=scope.id,
            status=DiscoveryRunStatus.RUNNING,
            run_fingerprint="envelope-run",
            requested_by=actor.user_id,
            started_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.configs.add_scope_version(scope)
            await uow.observations.add_run(run)
            await uow.commit()

        from lab_assistant.adapters.connectors.base import compile_query_fingerprint
        from lab_assistant.contracts.connector import CompiledQuery

        compiled = CompiledQuery(
            connector_id="pubmed",
            connector_version="1.0.0",
            query_text="term",
            query_fingerprint=compile_query_fingerprint(
                connector_id="pubmed",
                connector_version="1.0.0",
                query="term",
                parameters={},
            ),
        )
        page = RawPage(
            response=make_response(
                method="GET",
                url_redacted="https://eutils.ncbi.nlm.nih.gov/esearch.fcgi?db=pubmed",
                status=200,
                body=ENVELOPE,
                upstream_body=UPSTREAM,
            ),
            cursor_before=Cursor(
                connector_id="pubmed", query_fingerprint=compiled.query_fingerprint, position=None
            ),
            cursor_after=Cursor(
                connector_id="pubmed",
                query_fingerprint=compiled.query_fingerprint,
                position=None,
                exhausted=True,
            ),
        )
        handler = DiscoveryHandler(context)
        await handler._store_page(
            command=StageCommand(
                stage=StageName.DISCOVERY,
                workspace_id=workspace.id,
                actor_id=actor.user_id,
                inputs={},
            ),
            run=run,
            scope=scope,
            connector_id="pubmed",
            connector_version="1.0.0",
            compiled=compiled,
            page=page,
        )

        async with context.uow_factory() as uow:
            requests = await uow.observations.list_requests(workspace.id, run.id)
        assert len(requests) == 1
        stored = requests[0]
        assert stored.response_body_kind == BodyKind.REPLAY_ENVELOPE.value
        assert stored.response_sha256 == sha256_bytes(ENVELOPE)
        assert stored.upstream_sha256 == sha256_bytes(UPSTREAM)
        assert stored.upstream_body_object_id is not None
        # Both objects are retrievable: replay uses one, provenance the other.
        assert await context.object_store.get(stored.response_body_object_id) == ENVELOPE
        assert await context.object_store.get(stored.upstream_body_object_id) == UPSTREAM
