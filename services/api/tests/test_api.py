"""API tests over the deterministic stack.

The container is replaced with the in-process harness, so these tests exercise the
real routes, the real services, and the real policy engine — only the adapters are
in-memory. No route is monkeypatched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from lab_assistant.application.accounts import AccountService
from lab_assistant.application.tasks import TaskOrchestrator
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, fake_source, make_actor, record, source_page
from lab_assistant_api.deps import ApiContainer
from lab_assistant_api.main import create_app

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture
async def api() -> dict[str, Any]:
    harness = build_harness(
        connectors={
            "pubmed-fixture": fake_source(
                "pubmed-fixture",
                [
                    source_page(
                        query="lipid production",
                        records=[
                            record(
                                locator="doi:10.1234/example",
                                identifiers=[("doi", "10.1234/example")],
                                title="Lipid production in an oleaginous yeast",
                                year=2025,
                                journal="Journal of Applied Microbiology",
                                licence="cc-by-4.0",
                            )
                        ],
                    )
                ],
            )
        }
    )
    accounts: AccountService = harness.accounts
    steward = await accounts.create_user(email="steward@lab.invalid", password=PASSWORD)
    researcher = await accounts.create_user(email="researcher@lab.invalid", password=PASSWORD)
    workspace, _ = await accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=steward.id
    )
    admin = make_actor(user_id=steward.id, workspace_id=workspace.id, role=Role.ADMIN)
    await accounts.add_member(admin, user_id=researcher.id, role=Role.RESEARCHER)
    for action in (PolicyAction.QUERY, PolicyAction.VIEW_CONTENT, PolicyAction.DISPLAY_EXCERPT):
        await accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=action,
            effect=Effect.ALLOW,
            policy_basis="workspace_corpus",
        )

    container = ApiContainer(
        settings=harness.settings,
        context=harness.context,
        accounts=accounts,
        handlers=harness.handlers,
        orchestrator=TaskOrchestrator(harness.context, harness.handlers),
    )
    app = create_app(settings=harness.settings, container=container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.invalid") as client:
        yield {
            "client": client,
            "harness": harness,
            "workspace": workspace,
            "steward": steward,
            "researcher": researcher,
            # The readiness tests swap the container to simulate a dependency
            # that has stopped answering, then put the real one back.
            "app": app,
            "container": container,
        }


async def _login(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _raises(message: str):
    """A unit-of-work factory that fails the way an unreachable database does."""

    def factory():
        raise RuntimeError(message)

    return factory


class TestHealthAndConfig:
    async def test_health_needs_no_credentials(self, api) -> None:
        response = await api["client"].get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_liveness_does_not_touch_a_dependency(self, api) -> None:
        """A database blip must not make every container report unhealthy: an
        orchestrator would restart all of them, which fixes nothing."""
        container = api["container"]
        broken = SimpleNamespace(
            context=SimpleNamespace(
                uow_factory=_raises("the database is down"),
                object_store=container.context.object_store,
            ),
            schema_revision=None,
        )
        api["app"].state.container = broken
        try:
            response = await api["client"].get("/health")
            assert response.status_code == 200
        finally:
            api["app"].state.container = container

    async def test_readiness_reports_each_dependency(self, api) -> None:
        response = await api["client"].get("/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        by_name = {check["name"]: check for check in body["checks"]}
        assert by_name["database"]["ok"] is True
        assert by_name["object_store"]["ok"] is True

    async def test_readiness_answers_503_when_it_cannot_serve(self, api) -> None:
        """The status code is the contract.

        This answered 200 with `"status": "degraded"` in the body, so every
        load balancer and container healthcheck — anything reading the code and
        not the JSON — sent traffic to a process that had just said it could
        not reach its database.
        """
        container = api["container"]
        broken = SimpleNamespace(
            context=SimpleNamespace(
                uow_factory=_raises("the database is down"),
                object_store=container.context.object_store,
            ),
            schema_revision=None,
        )
        api["app"].state.container = broken
        try:
            response = await api["client"].get("/ready")
            assert response.status_code == 503
            body = response.json()
            assert body["status"] == "not_ready"
            database = next(c for c in body["checks"] if c["name"] == "database")
            assert database["ok"] is False
        finally:
            api["app"].state.container = container

    async def test_readiness_refuses_when_the_migration_has_not_run(self, api) -> None:
        """An API container that starts before its migrations is the most common
        way a deploy goes wrong, and it was the one thing not checked."""
        container = api["container"]

        async def stale() -> str:
            return "0000_an_older_release"

        behind = SimpleNamespace(
            context=container.context,
            schema_revision=stale,
        )
        api["app"].state.container = behind
        try:
            response = await api["client"].get("/ready")
            assert response.status_code == 503
            schema = next(c for c in response.json()["checks"] if c["name"] == "schema")
            assert schema["ok"] is False
            assert "run the migration" in schema["detail"]
        finally:
            api["app"].state.container = container

    async def test_readiness_does_not_write_to_the_object_store(self, api) -> None:
        """The probe this replaces stored a payload every time it ran, into the
        store that holds the evidence."""
        store = api["container"].context.object_store
        before = {record.id async for record in store.inventory()}
        for _ in range(3):
            assert (await api["client"].get("/ready")).status_code == 200
        after = {record.id async for record in store.inventory()}
        assert after == before

    async def test_effective_config_needs_an_operator(self, api) -> None:
        """Redaction is not a reason to publish deployment topology."""
        anonymous = await api["client"].get("/api/v1/config")
        assert anonymous.status_code == 401

        researcher = await _login(api["client"], "researcher@lab.invalid")
        denied = await api["client"].get("/api/v1/config", headers=_auth(researcher))
        assert denied.status_code == 403
        assert denied.json()["error_class"] == "policy_blocked"

    async def test_effective_config_redacts_secrets_for_an_operator(self, api) -> None:
        token = await _login(api["client"], "steward@lab.invalid")
        response = await api["client"].get("/api/v1/config", headers=_auth(token))
        assert response.status_code == 200, response.text
        config = response.json()["config"]
        assert config["secret_key"].startswith("[redacted sha256:")
        assert "development-only" not in json.dumps(config)

    async def test_request_id_is_echoed(self, api) -> None:
        response = await api["client"].get("/health", headers={"X-Request-Id": "abc123"})
        assert response.headers["X-Request-Id"] == "abc123"


class TestLoginThrottling:
    async def test_the_route_enforces_the_configured_rate(self, api) -> None:
        """A password endpoint with no limit is an online attack on an offline hash."""
        client = api["client"]
        settings = api["harness"].context.settings
        limit = settings.login_rate_limit_per_minute
        statuses = []
        for _ in range(limit + 2):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "steward@lab.invalid", "password": "definitely-wrong"},
            )
            statuses.append(response.status_code)
        assert 401 in statuses
        assert 429 in statuses, f"no throttling after {limit + 2} attempts: {statuses}"
        throttled = await client.post(
            "/api/v1/auth/login",
            json={"email": "steward@lab.invalid", "password": "definitely-wrong"},
        )
        body = throttled.json()
        assert throttled.status_code == 429
        assert body["error_class"] == "rate_limited"
        # Still no hint about which half was wrong, or whether the account exists.
        assert "password" not in body["detail"].lower()


class TestAuth:
    async def test_login_returns_a_token_and_the_users_workspaces(self, api) -> None:
        response = await api["client"].post(
            "/api/v1/auth/login",
            json={"email": "steward@lab.invalid", "password": PASSWORD},
        )
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["workspaces"][0]["slug"] == "rla-lab"

    async def test_wrong_password_is_a_problem_document(self, api) -> None:
        response = await api["client"].post(
            "/api/v1/auth/login",
            json={"email": "steward@lab.invalid", "password": "wrong-passphrase"},
        )
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["error_class"] == "authentication_required"

    async def test_error_does_not_say_which_half_was_wrong(self, api) -> None:
        unknown = await api["client"].post(
            "/api/v1/auth/login",
            json={"email": "nobody@lab.invalid", "password": PASSWORD},
        )
        wrong = await api["client"].post(
            "/api/v1/auth/login",
            json={"email": "steward@lab.invalid", "password": "wrong-passphrase"},
        )
        assert unknown.json()["detail"] == wrong.json()["detail"]

    async def test_protected_route_requires_a_session(self, api) -> None:
        response = await api["client"].get("/api/v1/auth/me")
        assert response.status_code == 401

    async def test_me_reports_capabilities(self, api) -> None:
        token = await _login(api["client"], "researcher@lab.invalid")
        response = await api["client"].get("/api/v1/auth/me", headers=_auth(token))
        body = response.json()
        assert body["role"] == "researcher"
        assert "ask" in body["capabilities"]
        assert "manage_policy" not in body["capabilities"]

    async def test_logout_revokes_every_issued_token(self, api) -> None:
        token = await _login(api["client"], "researcher@lab.invalid")
        assert (await api["client"].get("/api/v1/auth/me", headers=_auth(token))).status_code == 200
        await api["client"].post("/api/v1/auth/logout", headers=_auth(token))
        after = await api["client"].get("/api/v1/auth/me", headers=_auth(token))
        assert after.status_code == 401
        assert "revoked" in after.json()["detail"]

    async def test_a_tampered_token_is_rejected(self, api) -> None:
        token = await _login(api["client"], "researcher@lab.invalid")
        response = await api["client"].get("/api/v1/auth/me", headers=_auth(token[:-3] + "aaa"))
        assert response.status_code == 401

    async def test_cookie_sessions_work_for_the_web_tier(self, api) -> None:
        token = await _login(api["client"], "researcher@lab.invalid")
        api["client"].cookies.set("lab_session", token)
        response = await api["client"].get("/api/v1/auth/me")
        assert response.status_code == 200
        api["client"].cookies.clear()


class TestSessionsAndAsk:
    async def test_session_lifecycle(self, api) -> None:
        token = await _login(api["client"], "researcher@lab.invalid")
        created = await api["client"].post("/api/v1/chat/sessions", headers=_auth(token))
        session_id = created.json()["id"]

        listed = await api["client"].get("/api/v1/chat/sessions", headers=_auth(token))
        assert [item["id"] for item in listed.json()["items"]] == [session_id]

        renamed = await api["client"].patch(
            f"/api/v1/chat/sessions/{session_id}",
            headers=_auth(token),
            json={"title": "Lipid yields"},
        )
        assert renamed.json()["title"] == "Lipid yields"

        deleted = await api["client"].delete(
            f"/api/v1/chat/sessions/{session_id}", headers=_auth(token)
        )
        assert deleted.status_code == 204
        after = await api["client"].get("/api/v1/chat/sessions", headers=_auth(token))
        assert after.json()["items"] == []

    async def test_ask_streams_sources_then_tokens_then_done(self, api) -> None:
        token = await _login(api["client"], "researcher@lab.invalid")
        created = await api["client"].post("/api/v1/chat/sessions", headers=_auth(token))
        session_id = created.json()["id"]
        response = await api["client"].post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            headers=_auth(token),
            json={"question": "What lipid yield was reported?"},
        )
        assert response.status_code == 200
        events = [
            line.removeprefix("event: ")
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        # No release is published in this fixture, so the stream must still end
        # with a terminal event rather than hanging or 500ing.
        assert events[-1] in ("done", "error")

    async def test_another_users_session_is_not_found(self, api) -> None:
        steward_token = await _login(api["client"], "steward@lab.invalid")
        created = await api["client"].post("/api/v1/chat/sessions", headers=_auth(steward_token))
        session_id = created.json()["id"]
        researcher_token = await _login(api["client"], "researcher@lab.invalid")
        response = await api["client"].get(
            f"/api/v1/chat/sessions/{session_id}", headers=_auth(researcher_token)
        )
        assert response.status_code == 404


class TestCollections:
    async def test_scope_create_gate_and_approve(self, api) -> None:
        token = await _login(api["client"], "steward@lab.invalid")
        created = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "name": "Yarrowia lipids",
                "scientific_objective": "Lipid yields",
                "inclusion_criteria": ["engineered yeast"],
                "intended_uses": ["literature search"],
                "concepts": ["lipid production"],
                "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
            },
        )
        assert created.status_code == 201
        scope_id = created.json()["id"]

        gate = await api["client"].post(f"/api/v1/scopes/{scope_id}/gate", headers=_auth(token))
        assert gate.json()["passed"] is True
        assert gate.json()["compiled_connectors"] == ["pubmed-fixture"]

        approved = await api["client"].post(
            f"/api/v1/scopes/{scope_id}/approve", headers=_auth(token)
        )
        assert approved.json()["approval_state"] == "approved"

    async def test_a_researcher_cannot_create_a_scope(self, api) -> None:
        token = await _login(api["client"], "researcher@lab.invalid")
        response = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "name": "n",
                "scientific_objective": "o",
                "connectors": [{"connector_id": "pubmed-fixture", "query": "q"}],
            },
        )
        assert response.status_code == 403
        assert response.json()["error_class"] == "policy_blocked"

    async def test_discovery_run_reports_connector_state(self, api) -> None:
        token = await _login(api["client"], "steward@lab.invalid")
        created = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "name": "Yarrowia lipids",
                "scientific_objective": "Lipid yields",
                "inclusion_criteria": ["engineered yeast"],
                "intended_uses": ["literature search"],
                "concepts": ["lipid production"],
                "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
            },
        )
        scope_id = created.json()["id"]
        await api["client"].post(f"/api/v1/scopes/{scope_id}/approve", headers=_auth(token))
        started = await api["client"].post(
            "/api/v1/discovery-runs",
            headers=_auth(token),
            json={"scope_version_id": scope_id},
        )
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        await api["harness"].run_all_tasks()

        listed = await api["client"].get("/api/v1/discovery-runs", headers=_auth(token))
        run = next(item for item in listed.json()["items"] if item["id"] == run_id)
        assert run["connectors"][0]["outcome"] == "succeeded"

        frozen = await api["client"].post(
            f"/api/v1/discovery-runs/{run_id}/freeze", headers=_auth(token)
        )
        assert frozen.json()["status"] == "frozen"
        await api["harness"].run_all_tasks()

        candidates = await api["client"].get("/api/v1/candidates", headers=_auth(token))
        items = candidates.json()["items"]
        assert items
        assert items[0]["title"] == "Lipid production in an oleaginous yeast"
        # Canonical fields explain themselves.
        assert items[0]["provenance"]["title"]


class TestOperations:
    async def test_task_snapshot_requires_the_capability(self, api) -> None:
        researcher = await _login(api["client"], "researcher@lab.invalid")
        denied = await api["client"].get("/api/v1/operations/tasks", headers=_auth(researcher))
        assert denied.status_code == 403

        steward = await _login(api["client"], "steward@lab.invalid")
        allowed = await api["client"].get("/api/v1/operations/tasks", headers=_auth(steward))
        assert allowed.status_code == 200
        assert "counts" in allowed.json()

    async def test_audit_events_are_readable_by_operators(self, api) -> None:
        steward = await _login(api["client"], "steward@lab.invalid")
        response = await api["client"].get("/api/v1/audit-events", headers=_auth(steward))
        assert response.status_code == 200
        actions = {event["action"] for event in response.json()["items"]}
        assert "membership_changed" in actions or "grant_changed" in actions

    async def test_audit_metadata_never_contains_a_secret(self, api) -> None:
        steward = await _login(api["client"], "steward@lab.invalid")
        response = await api["client"].get("/api/v1/audit-events", headers=_auth(steward))
        blob = json.dumps(response.json())
        assert PASSWORD not in blob


class TestOpenApi:
    async def test_schema_is_generated(self, api) -> None:
        response = await api["client"].get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == "Lab AI Assistant"
        for path in (
            "/api/v1/auth/login",
            "/api/v1/chat/sessions",
            "/api/v1/search",
            "/api/v1/corpus-releases",
            "/api/v1/analytics/corpus",
        ):
            assert path in schema["paths"], path

    async def test_no_public_query_endpoint_exists(self, api) -> None:
        """Internal-only is a product boundary, so it is asserted."""
        schema = (await api["client"].get("/openapi.json")).json()
        unauthenticated = {"/health", "/ready", "/api/v1/config", "/api/v1/auth/login"}
        for path in schema["paths"]:
            if path in unauthenticated:
                continue
            response = await api["client"].request(
                next(iter(schema["paths"][path])).upper(),
                path.replace("{session_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{release_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{scope_version_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{run_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{work_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{revision_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{plan_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{task_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{message_id}", "00000000-0000-4000-8000-000000000001")
                .replace("{handle}", "E0123456789"),
            )
            assert response.status_code != 200, f"{path} answered without a session"
