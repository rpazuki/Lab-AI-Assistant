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
                installation=container.context.installation,
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
                installation=container.context.installation,
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
        # The web app shows a mirror's own pages only on a mirror.
        assert body["installation_role"] == "publisher"

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


class TestMembers:
    async def test_an_admin_lists_members_and_a_researcher_cannot(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        listed = await client.get("/api/v1/members", headers=_auth(admin))
        assert listed.status_code == 200
        rows = {row["email"]: row for row in listed.json()["items"]}
        assert rows["steward@lab.invalid"]["role"] == "admin"
        assert rows["researcher@lab.invalid"]["status"] == "active"
        assert "hashed_password" not in rows["researcher@lab.invalid"]

        researcher = await _login(client, "researcher@lab.invalid")
        refused = await client.get("/api/v1/members", headers=_auth(researcher))
        assert refused.status_code == 403
        assert (
            await client.get("/api/v1/invitations", headers=_auth(researcher))
        ).status_code == 403

    async def test_an_invitation_is_previewed_accepted_once_and_signs_in(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        created = await client.post(
            "/api/v1/invitations",
            json={"email": "New.Person@Lab.invalid", "role": "reviewer"},
            headers=_auth(admin),
        )
        assert created.status_code == 201, created.text
        token = created.json()["token"]

        preview = await client.post("/api/v1/invitations/preview", json={"token": token})
        assert preview.status_code == 200
        assert preview.json()["email"] == "new.person@lab.invalid"
        assert preview.json()["workspace"]["name"] == "RLA Lab"
        assert preview.json()["has_account"] is False

        accepted = await client.post(
            "/api/v1/invitations/accept",
            json={"token": token, "password": PASSWORD, "full_name": "New Person"},
        )
        assert accepted.status_code == 200, accepted.text
        me = await client.get("/api/v1/auth/me", headers=_auth(accepted.json()["access_token"]))
        assert me.json()["role"] == "reviewer"
        listed = (await client.get("/api/v1/members", headers=_auth(admin))).json()["items"]
        joined = next(row for row in listed if row["email"] == "new.person@lab.invalid")
        assert joined["last_login_at"] is not None

        again = await client.post(
            "/api/v1/invitations/accept", json={"token": token, "password": PASSWORD}
        )
        assert again.status_code == 403
        states = [
            row["state"]
            for row in (await client.get("/api/v1/invitations", headers=_auth(admin))).json()[
                "items"
            ]
        ]
        assert states == ["accepted"]

    async def test_an_accepted_or_open_invitation_stays_and_a_revoked_one_goes(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        created = (
            await client.post(
                "/api/v1/invitations", json={"email": "gone@lab.invalid"}, headers=_auth(admin)
            )
        ).json()
        invitation_id = created["id"]
        refused = await client.delete(f"/api/v1/invitations/{invitation_id}", headers=_auth(admin))
        assert refused.status_code == 409
        assert "revoke it first" in refused.text
        await client.post(f"/api/v1/invitations/{invitation_id}/revoke", headers=_auth(admin))
        deleted = await client.delete(f"/api/v1/invitations/{invitation_id}", headers=_auth(admin))
        assert deleted.status_code == 204
        listed = (await client.get("/api/v1/invitations", headers=_auth(admin))).json()["items"]
        assert all(row["id"] != invitation_id for row in listed)

    async def test_inviting_again_replaces_the_earlier_link(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        first = (
            await client.post(
                "/api/v1/invitations", json={"email": "late@lab.invalid"}, headers=_auth(admin)
            )
        ).json()
        await client.post(
            "/api/v1/invitations", json={"email": "late@lab.invalid"}, headers=_auth(admin)
        )
        stale = await client.post("/api/v1/invitations/preview", json={"token": first["token"]})
        assert stale.status_code == 403
        states = sorted(
            row["state"]
            for row in (await client.get("/api/v1/invitations", headers=_auth(admin))).json()[
                "items"
            ]
        )
        assert states == ["open", "revoked"]

    async def test_an_open_invitation_can_be_revoked(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        created = (
            await client.post(
                "/api/v1/invitations", json={"email": "gone@lab.invalid"}, headers=_auth(admin)
            )
        ).json()
        revoked = await client.post(
            f"/api/v1/invitations/{created['id']}/revoke", headers=_auth(admin)
        )
        assert revoked.status_code == 200
        twice = await client.post(
            f"/api/v1/invitations/{created['id']}/revoke", headers=_auth(admin)
        )
        assert twice.status_code == 409
        preview = await client.post("/api/v1/invitations/preview", json={"token": created["token"]})
        assert preview.status_code == 403

    async def test_an_active_member_is_not_invited_twice(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        response = await client.post(
            "/api/v1/invitations", json={"email": "researcher@lab.invalid"}, headers=_auth(admin)
        )
        assert response.status_code == 422

    async def test_a_service_account_role_is_not_offered_to_a_person(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        response = await client.post(
            "/api/v1/invitations",
            json={"email": "bot@lab.invalid", "role": "service_account"},
            headers=_auth(admin),
        )
        assert response.status_code == 422

    async def test_an_existing_account_accepts_with_its_own_password(self, api) -> None:
        client = api["client"]
        await api["harness"].accounts.create_user(email="elsewhere@lab.invalid", password=PASSWORD)
        admin = await _login(client, "steward@lab.invalid")
        token = (
            await client.post(
                "/api/v1/invitations", json={"email": "elsewhere@lab.invalid"}, headers=_auth(admin)
            )
        ).json()["token"]
        preview = await client.post("/api/v1/invitations/preview", json={"token": token})
        assert preview.json()["has_account"] is True

        wrong = await client.post(
            "/api/v1/invitations/accept",
            json={"token": token, "password": "not-the-password-at-all"},
        )
        assert wrong.status_code == 401
        right = await client.post(
            "/api/v1/invitations/accept", json={"token": token, "password": PASSWORD}
        )
        assert right.status_code == 200

    async def test_a_role_change_takes_effect_on_the_next_request(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        researcher = await _login(client, "researcher@lab.invalid")
        changed = await client.patch(
            f"/api/v1/members/{api['researcher'].id}",
            json={"role": "reviewer"},
            headers=_auth(admin),
        )
        assert changed.status_code == 200
        assert "review_claims" in changed.json()["capabilities"]
        me = await client.get("/api/v1/auth/me", headers=_auth(researcher))
        assert me.json()["role"] == "reviewer"

    async def test_suspending_ends_sessions_and_refuses_sign_in(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        researcher = await _login(client, "researcher@lab.invalid")
        member = f"/api/v1/members/{api['researcher'].id}"

        suspended = await client.patch(member, json={"status": "suspended"}, headers=_auth(admin))
        assert suspended.status_code == 200
        assert suspended.json()["capabilities"] == []
        assert (await client.get("/api/v1/auth/me", headers=_auth(researcher))).status_code == 401

        login = await client.post(
            "/api/v1/auth/login", json={"email": "researcher@lab.invalid", "password": PASSWORD}
        )
        assert login.status_code == 403
        assert login.json()["context"]["reason"] == "membership_suspended"

        await client.patch(member, json={"status": "active"}, headers=_auth(admin))
        assert await _login(client, "researcher@lab.invalid")

    async def test_an_admin_cannot_change_their_own_membership(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        response = await client.patch(
            f"/api/v1/members/{api['steward'].id}",
            json={"role": "researcher"},
            headers=_auth(admin),
        )
        assert response.status_code == 403

    async def test_signing_a_member_out_everywhere(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        researcher = await _login(client, "researcher@lab.invalid")
        response = await client.post(
            f"/api/v1/members/{api['researcher'].id}/revoke-sessions", headers=_auth(admin)
        )
        assert response.status_code == 200
        assert (await client.get("/api/v1/auth/me", headers=_auth(researcher))).status_code == 401
        assert await _login(client, "researcher@lab.invalid")


async def _seed_answer(api, *, tokens: int, deleted: bool = False) -> str:
    """Record one question and answer for the researcher, as if asked earlier."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from lab_assistant.domain.chat import ChatMessage, ChatSession, MessageRole

    harness, workspace = api["harness"], api["workspace"]
    now = datetime.now(tz=UTC)
    session = ChatSession(
        id=uuid4(),
        workspace_id=workspace.id,
        user_id=api["researcher"].id,
        title="Lipid yields",
        created_at=now,
        updated_at=now,
        deleted_at=now if deleted else None,
    )
    async with harness.context.uow_factory() as uow:
        await uow.chat.add_session(session)
        for role, content, used in (
            (MessageRole.USER, "What lipid yield was reported?", 0),
            (MessageRole.ASSISTANT, "About 40% of dry cell weight.", tokens),
        ):
            await uow.chat.add_message(
                ChatMessage(
                    id=uuid4(),
                    workspace_id=workspace.id,
                    session_id=session.id,
                    role=role,
                    content=content,
                    input_tokens=used // 2,
                    output_tokens=used - used // 2,
                    latency_ms=900 if role is MessageRole.ASSISTANT else 0,
                    created_at=now,
                )
            )
        await uow.commit()
    return str(session.id)


class TestUsageAndAllowance:
    async def test_a_member_sees_their_own_allowance(self, api) -> None:
        await _seed_answer(api, tokens=1200)
        token = await _login(api["client"], "researcher@lab.invalid")
        quota = (await api["client"].get("/api/v1/chat/quota", headers=_auth(token))).json()
        assert quota == {
            "token_limit": 1_000_000,
            "used_tokens": 1200,
            "remaining_tokens": 998_800,
            "limit_reached": False,
            "custom_limit": False,
        }

    async def test_deleting_a_conversation_does_not_refund_it(self, api) -> None:
        await _seed_answer(api, tokens=500, deleted=True)
        token = await _login(api["client"], "researcher@lab.invalid")
        quota = (await api["client"].get("/api/v1/chat/quota", headers=_auth(token))).json()
        assert quota["used_tokens"] == 500

    async def test_asking_is_refused_once_the_allowance_is_used(self, api) -> None:
        client = api["client"]
        await _seed_answer(api, tokens=1000)
        admin = await _login(client, "steward@lab.invalid")
        limited = await client.patch(
            f"/api/v1/members/{api['researcher'].id}",
            json={"token_limit": 1000},
            headers=_auth(admin),
        )
        assert limited.json()["limit_reached"] is True
        assert limited.json()["custom_token_limit"] is True

        token = await _login(client, "researcher@lab.invalid")
        session_id = (await client.post("/api/v1/chat/sessions", headers=_auth(token))).json()["id"]
        refused = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            headers=_auth(token),
            json={"question": "Anything new?"},
        )
        assert refused.status_code == 429
        assert refused.json()["error_class"] == "budget_exceeded"
        assert refused.json()["context"]["token_limit"] == 1000

        # Null returns the member to the default, which is far above 1000.
        reset = await client.patch(
            f"/api/v1/members/{api['researcher'].id}",
            json={"token_limit": None},
            headers=_auth(admin),
        )
        assert reset.json()["token_limit"] == 1_000_000
        assert reset.json()["custom_token_limit"] is False

    async def test_an_admin_sets_their_own_limit_but_not_their_role(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        own = f"/api/v1/members/{api['steward'].id}"
        assert (
            await client.patch(own, json={"token_limit": 5}, headers=_auth(admin))
        ).status_code == 200
        assert (
            await client.patch(own, json={"role": "reviewer"}, headers=_auth(admin))
        ).status_code == 403

    async def test_members_list_carries_usage(self, api) -> None:
        await _seed_answer(api, tokens=300)
        admin = await _login(api["client"], "steward@lab.invalid")
        body = (await api["client"].get("/api/v1/members", headers=_auth(admin))).json()
        researcher = next(row for row in body["items"] if row["email"] == "researcher@lab.invalid")
        assert researcher["usage"]["questions"] == 1
        assert researcher["usage"]["answers"] == 1
        assert researcher["usage"]["total_tokens"] == 300
        assert researcher["usage"]["avg_latency_ms"] == 900
        assert body["token_limit_default"] == 1_000_000
        assert body["chat_admin_access"] is True


class TestChatOversight:
    async def test_an_admin_reads_a_members_chat_and_it_is_audited(self, api) -> None:
        client = api["client"]
        session_id = await _seed_answer(api, tokens=100)
        admin = await _login(client, "steward@lab.invalid")
        member = f"/api/v1/members/{api['researcher'].id}"

        listed = await client.get(f"{member}/sessions", headers=_auth(admin))
        assert [item["id"] for item in listed.json()["items"]] == [session_id]
        read = await client.get(f"{member}/sessions/{session_id}", headers=_auth(admin))
        assert [m["role"] for m in read.json()["messages"]] == ["user", "assistant"]

        events = (await client.get("/api/v1/audit-events", headers=_auth(admin))).json()["items"]
        reads = [e for e in events if e["action"] == "member_chat_read"]
        assert {e["reason_code"] for e in reads} == {"admin_oversight_list", "admin_oversight_read"}

    async def test_members_are_told_admins_can_read(self, api) -> None:
        token = await _login(api["client"], "researcher@lab.invalid")
        me = (await api["client"].get("/api/v1/auth/me", headers=_auth(token))).json()
        assert me["chat_visible_to_admins"] is True

    async def test_a_researcher_cannot_read_another_members_chat(self, api) -> None:
        session_id = await _seed_answer(api, tokens=100)
        token = await _login(api["client"], "researcher@lab.invalid")
        response = await api["client"].get(
            f"/api/v1/members/{api['researcher'].id}/sessions/{session_id}", headers=_auth(token)
        )
        assert response.status_code == 403

    async def test_oversight_can_be_switched_off(self, api) -> None:
        api["harness"].settings.chat_admin_access = False
        try:
            admin = await _login(api["client"], "steward@lab.invalid")
            response = await api["client"].get(
                f"/api/v1/members/{api['researcher'].id}/sessions", headers=_auth(admin)
            )
            assert response.status_code == 403
        finally:
            api["harness"].settings.chat_admin_access = True


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

    async def test_a_draft_failing_its_gate_is_fixed_by_its_next_version(self, api) -> None:
        """The edit loop the web form relies on: list a draft, resend it fixed."""
        token = await _login(api["client"], "steward@lab.invalid")
        created = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "name": "Yarrowia",
                "scientific_objective": "Yarrowia literature",
                "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
            },
        )
        draft_id = created.json()["id"]
        gate = await api["client"].post(f"/api/v1/scopes/{draft_id}/gate", headers=_auth(token))
        assert gate.json()["passed"] is False

        listed = await api["client"].get("/api/v1/scopes", headers=_auth(token))
        draft = next(item for item in listed.json()["items"] if item["id"] == draft_id)
        assert draft["scientific_objective"] == "Yarrowia literature"
        assert "inclusion_criteria" not in draft
        assert draft["connector_plan"] == [
            {"connector_id": "pubmed-fixture", "query": "lipid production", "required": True}
        ]

        fixed = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "scope_id": draft["scope_id"],
                "name": draft["name"],
                "scientific_objective": draft["scientific_objective"],
                "intended_uses": ["literature search"],
                "connectors": draft["connector_plan"],
            },
        )
        assert fixed.json()["version"] == 2
        regate = await api["client"].post(
            f"/api/v1/scopes/{fixed.json()['id']}/gate", headers=_auth(token)
        )
        assert regate.json()["passed"] is True

    async def test_screening_rules_round_trip_and_an_approved_version_applies_them(
        self, api
    ) -> None:
        """Concepts, exclusion terms and thresholds are saved, listed, and applied."""
        token = await _login(api["client"], "steward@lab.invalid")
        body = {
            "name": "Yarrowia",
            "scientific_objective": "Yarrowia literature",
            "intended_uses": ["literature search"],
            "concepts": ["yarrowia | y. lipolytica", " "],
            "exclusion_terms": ["plant"],
            "include_threshold": 0.5,
            "exclude_threshold": 0.0,
            "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
        }
        bad = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={**body, "include_threshold": 0.2, "exclude_threshold": 0.3},
        )
        assert bad.status_code == 422
        created = await api["client"].post("/api/v1/scopes", headers=_auth(token), json=body)
        scope_id = created.json()["id"]
        listed = await api["client"].get("/api/v1/scopes", headers=_auth(token))
        scope = next(item for item in listed.json()["items"] if item["id"] == scope_id)
        assert scope["concepts"] == ["yarrowia | y. lipolytica"]
        assert scope["exclusion_terms"] == ["plant"]
        assert scope["relevance_policy"] == {"include_threshold": 0.5, "exclude_threshold": 0.0}
        assert scope["frozen_at"] is None

        draft_preview = await api["client"].post(
            f"/api/v1/scopes/{scope_id}/rules/preview", headers=_auth(token)
        )
        assert draft_preview.status_code == 409
        await api["client"].post(f"/api/v1/scopes/{scope_id}/approve", headers=_auth(token))
        preview = await api["client"].post(
            f"/api/v1/scopes/{scope_id}/rules/preview", headers=_auth(token)
        )
        assert preview.status_code == 200
        assert preview.json()["scope"]["exclusion_terms"] == ["plant"]
        applied = await api["client"].post(
            f"/api/v1/scopes/{scope_id}/rules/apply", headers=_auth(token), json={}
        )
        assert applied.json()["remaining"] == 0

        history = await api["client"].get(
            "/api/v1/review/history",
            headers=_auth(token),
            params={"scope_version_id": scope_id},
        )
        kinds = [item["kind"] for item in history.json()["items"]]
        assert kinds == ["scope_rules_applied", "scope_approved"]
        assert history.json()["items"][1]["actor"]
        assert history.json()["next_cursor"] is None

    async def test_a_scope_with_only_a_purpose_passes_its_gate_and_is_deleted_whole(
        self, api
    ) -> None:
        """Purpose replaces the criteria lists; deleting needs the name typed exactly.

        Deleting is queued: the press confirms the name and returns a task, and the
        scope is still there until a worker has run it.
        """
        harness = api["harness"]
        token = await _login(api["client"], "steward@lab.invalid")
        body = {
            "name": "Yarrowia",
            "scientific_objective": "Papers that study Y. lipolytica itself.",
            "intended_uses": ["literature search"],
            "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
        }
        v1 = (await api["client"].post("/api/v1/scopes", headers=_auth(token), json=body)).json()
        gate = await api["client"].post(f"/api/v1/scopes/{v1['id']}/gate", headers=_auth(token))
        assert gate.json()["passed"] is True
        await api["client"].post(f"/api/v1/scopes/{v1['id']}/approve", headers=_auth(token))

        preview = await api["client"].get(
            f"/api/v1/scopes/{v1['id']}/deletion", headers=_auth(token)
        )
        assert preview.json()["deletable"] is True
        assert preview.json()["counts"]["versions"] == 1
        wrong = await api["client"].post(
            f"/api/v1/scopes/{v1['id']}/delete-scope", headers=_auth(token), json={"name": "Y"}
        )
        assert wrong.status_code == 422
        queued = await api["client"].post(
            f"/api/v1/scopes/{v1['id']}/delete-scope",
            headers=_auth(token),
            json={"name": "Yarrowia"},
        )
        task_id = queued.json()["task_id"]
        still = await api["client"].get("/api/v1/scopes", headers=_auth(token))
        assert v1["id"] in {item["id"] for item in still.json()["items"]}

        await harness.run_all_tasks()

        status = await api["client"].get(f"/api/v1/scope-deletions/{task_id}", headers=_auth(token))
        assert status.json()["settled"] is True, status.json()["error"]
        assert status.json()["counts"]["versions"] == 1
        listed = await api["client"].get("/api/v1/scopes", headers=_auth(token))
        assert v1["id"] not in {item["id"] for item in listed.json()["items"]}

    async def test_scope_access_is_written_by_an_admin_and_refused_to_a_researcher(
        self, api
    ) -> None:
        """Handing out access is administrative.

        ``steward@lab.invalid`` owns this workspace and so holds ``admin``; the
        steward-cannot-write case is in ``tests/integration/test_scope_access.py``,
        where an actor can be given any role.
        """
        token = await _login(api["client"], "steward@lab.invalid")
        created = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "name": "Yarrowia",
                "scientific_objective": "Yarrowia literature",
                "intended_uses": ["literature search"],
                "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
            },
        )
        scope_id = created.json()["id"]

        empty = await api["client"].get(f"/api/v1/scopes/{scope_id}/access", headers=_auth(token))
        assert empty.status_code == 200, empty.text
        assert empty.json()["rules"] == []
        # Admins reach everything without a rule, so none is offered for them.
        assert "admin" not in [choice["name"] for choice in empty.json()["roles"]]

        added = await api["client"].post(
            f"/api/v1/scopes/{scope_id}/access",
            headers=_auth(token),
            json={"role": "researcher"},
        )
        assert added.status_code == 201, added.text

        listed = await api["client"].get(f"/api/v1/scopes/{scope_id}/access", headers=_auth(token))
        assert [rule["subject_role"] for rule in listed.json()["rules"]] == ["researcher"]

        gone = await api["client"].delete(
            f"/api/v1/scopes/{scope_id}/access/{added.json()['id']}", headers=_auth(token)
        )
        assert gone.status_code == 204
        after = await api["client"].get(f"/api/v1/scopes/{scope_id}/access", headers=_auth(token))
        assert after.json()["rules"] == []

        researcher = _auth(await _login(api["client"], "researcher@lab.invalid"))
        refused = await api["client"].post(
            f"/api/v1/scopes/{scope_id}/access", headers=researcher, json={"role": "reviewer"}
        )
        assert refused.status_code == 403, refused.text
        # Reading the rules is curating, which a researcher does not do either.
        assert (
            await api["client"].get(f"/api/v1/scopes/{scope_id}/access", headers=researcher)
        ).status_code == 403

    async def test_a_role_is_defined_by_an_admin_and_a_scope_opened_to_it(self, api) -> None:
        """The whole path, as the pages walk it.

        An admin names an audience the built-in roles do not — the people who may
        reach patented material — then opens one scope to that name. A researcher
        is refused both halves: defining a role decides what rules can say, and
        reading them is curating.
        """
        token = await _login(api["client"], "steward@lab.invalid")
        created = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "name": "Yarrowia",
                "scientific_objective": "Yarrowia literature",
                "intended_uses": ["literature search"],
                "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
            },
        )
        scope_id = created.json()["id"]

        defined = await api["client"].post(
            "/api/v1/roles",
            headers=_auth(token),
            json={
                "name": "Patent holders",
                "description": "Named on the filings",
                "base_role": "reviewer",
            },
        )
        assert defined.status_code == 201, defined.text
        assert defined.json()["name"] == "patent_holders"

        listed = await api["client"].get("/api/v1/roles", headers=_auth(token))
        by_name = {role["name"]: role for role in listed.json()["items"]}
        assert by_name["patent_holders"]["custom"] is True
        assert by_name["patent_holders"]["members"] == 0
        assert by_name["researcher"]["custom"] is False

        # The access panel offers it beside the built-in ones.
        offered = await api["client"].get(f"/api/v1/scopes/{scope_id}/access", headers=_auth(token))
        assert {
            "name": "patent_holders",
            "label": "Patent holders",
            "custom": True,
        } in offered.json()["roles"]

        opened = await api["client"].post(
            f"/api/v1/scopes/{scope_id}/access",
            headers=_auth(token),
            json={"role": "patent_holders"},
        )
        assert opened.status_code == 201, opened.text

        researcher = _auth(await _login(api["client"], "researcher@lab.invalid"))
        assert (await api["client"].get("/api/v1/roles", headers=researcher)).status_code == 403
        refused = await api["client"].post(
            "/api/v1/roles", headers=researcher, json={"name": "their own"}
        )
        assert refused.status_code == 403, refused.text

        # Removing it takes the rule that named it, and nothing else.
        removed = await api["client"].delete(
            f"/api/v1/roles/{defined.json()['id']}", headers=_auth(token)
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["rules_removed"] == 1
        after = await api["client"].get(f"/api/v1/scopes/{scope_id}/access", headers=_auth(token))
        assert after.json()["rules"] == []

    async def test_a_member_given_a_defined_role_is_shown_holding_it(self, api) -> None:
        """One name in the dropdown, and the bundle it is based on beside it."""
        token = _auth(await _login(api["client"], "steward@lab.invalid"))
        await api["client"].post(
            "/api/v1/roles",
            headers=token,
            json={"name": "patent holders", "base_role": "reviewer"},
        )
        members = await api["client"].get("/api/v1/members", headers=token)
        researcher = next(
            item for item in members.json()["items"] if item["email"] == "researcher@lab.invalid"
        )

        moved = await api["client"].patch(
            f"/api/v1/members/{researcher['user_id']}",
            headers=token,
            json={"role": "patent_holders"},
        )

        assert moved.status_code == 200, moved.text
        assert moved.json()["role"] == "patent_holders"
        assert moved.json()["base_role"] == "reviewer"
        assert "review_claims" in moved.json()["capabilities"]

        counted = await api["client"].get("/api/v1/roles", headers=token)
        held = {role["name"]: role["members"] for role in counted.json()["items"]}
        assert held["patent_holders"] == 1
        # And the role cannot be removed while they are in it.
        listed = next(role for role in counted.json()["items"] if role["name"] == "patent_holders")
        refused = await api["client"].delete(f"/api/v1/roles/{listed['id']}", headers=token)
        assert refused.status_code == 422, refused.text

    async def test_a_draft_is_corrected_in_place_keeping_its_version(self, api) -> None:
        token = await _login(api["client"], "steward@lab.invalid")
        created = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "name": "Yarrowia",
                "scientific_objective": "Yarrowia literature",
                "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
            },
        )
        draft_id = created.json()["id"]
        corrected = await api["client"].put(
            f"/api/v1/scopes/{draft_id}",
            headers=_auth(token),
            json={
                "name": "Yarrowia lipolytica",
                "scientific_objective": "Yarrowia literature",
                "intended_uses": ["literature search"],
                "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
            },
        )
        assert corrected.status_code == 200
        assert corrected.json()["id"] == draft_id
        assert corrected.json()["version"] == 1

        listed = await api["client"].get("/api/v1/scopes", headers=_auth(token))
        versions = [item for item in listed.json()["items"] if item["id"] == draft_id]
        assert len(versions) == 1
        assert versions[0]["name"] == "Yarrowia lipolytica"
        assert versions[0]["intended_uses"] == ["literature search"]
        gate = await api["client"].post(f"/api/v1/scopes/{draft_id}/gate", headers=_auth(token))
        assert gate.json()["passed"] is True

    async def test_a_draft_can_be_deleted_and_the_deletion_is_audited(self, api) -> None:
        token = await _login(api["client"], "steward@lab.invalid")
        created = await api["client"].post(
            "/api/v1/scopes",
            headers=_auth(token),
            json={
                "name": "Mistake",
                "scientific_objective": "o",
                "connectors": [{"connector_id": "pubmed-fixture", "query": "q"}],
            },
        )
        draft_id = created.json()["id"]
        deleted = await api["client"].delete(f"/api/v1/scopes/{draft_id}", headers=_auth(token))
        assert deleted.status_code == 204

        listed = await api["client"].get("/api/v1/scopes", headers=_auth(token))
        assert all(item["id"] != draft_id for item in listed.json()["items"])
        audit = await api["client"].get("/api/v1/audit-events", headers=_auth(token))
        assert any(
            event["action"] == "scope_draft_deleted" and event["subject_id"] == draft_id
            for event in audit.json()["items"]
        )

    async def test_an_approved_scope_can_be_neither_corrected_nor_deleted(self, api) -> None:
        token = await _login(api["client"], "steward@lab.invalid")
        body = {
            "name": "Yarrowia lipids",
            "scientific_objective": "Lipid yields",
            "intended_uses": ["literature search"],
            "connectors": [{"connector_id": "pubmed-fixture", "query": "lipid production"}],
        }
        created = await api["client"].post("/api/v1/scopes", headers=_auth(token), json=body)
        scope_id = created.json()["id"]
        await api["client"].post(f"/api/v1/scopes/{scope_id}/approve", headers=_auth(token))

        edited = await api["client"].put(
            f"/api/v1/scopes/{scope_id}", headers=_auth(token), json={**body, "name": "x"}
        )
        assert edited.status_code == 409
        deleted = await api["client"].delete(f"/api/v1/scopes/{scope_id}", headers=_auth(token))
        assert deleted.status_code == 409

    async def test_a_researcher_cannot_edit_or_delete_a_draft(self, api) -> None:
        steward = await _login(api["client"], "steward@lab.invalid")
        body = {
            "name": "n",
            "scientific_objective": "o",
            "connectors": [{"connector_id": "pubmed-fixture", "query": "q"}],
        }
        created = await api["client"].post("/api/v1/scopes", headers=_auth(steward), json=body)
        draft_id = created.json()["id"]
        researcher = await _login(api["client"], "researcher@lab.invalid")
        edited = await api["client"].put(
            f"/api/v1/scopes/{draft_id}", headers=_auth(researcher), json=body
        )
        assert edited.status_code == 403
        deleted = await api["client"].delete(
            f"/api/v1/scopes/{draft_id}", headers=_auth(researcher)
        )
        assert deleted.status_code == 403

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


class TestMergedCandidates:
    async def test_a_merged_paper_keeps_its_title_in_the_candidate_list(self, api) -> None:
        """Its records move to the paper it joins; the list must still name it."""
        from lab_assistant.application.reconciliation import refresh_summaries
        from lab_assistant.domain.identity import Work, WorkKind
        from lab_assistant.domain.observation import (
            Predicate,
            SourceAssertion,
            SourceObservation,
            SourceRequest,
        )

        context, ws = api["harness"].context, api["workspace"].id
        titles = ["Lipid factories in Yarrowia", "Lipid factories in Yarrowia lipolytica"]
        ids = []
        async with context.uow_factory() as uow:
            for title in titles:
                work = Work(
                    id=context.new_id(),
                    workspace_id=ws,
                    work_kind=WorkKind.ARTICLE,
                    created_at=context.now(),
                )
                await uow.works.add(work)
                request_id, observation_id = context.new_id(), context.new_id()
                await uow.observations.add_request(
                    SourceRequest(
                        id=request_id,
                        workspace_id=ws,
                        discovery_run_id=context.new_id(),
                        connector_id="fixture",
                        connector_version="1.0.0",
                        request_fingerprint=str(request_id),
                        method="GET",
                        canonical_url_redacted="https://example.invalid/q",
                        requested_at=context.now(),
                    )
                )
                await uow.observations.add_observation(
                    SourceObservation(
                        id=observation_id,
                        workspace_id=ws,
                        source_request_id=request_id,
                        source_record_locator=str(work.id),
                        parser_id="p",
                        parser_version="1",
                        schema_version="1",
                        observed_at=context.now(),
                    )
                )
                await uow.observations.add_assertions(
                    [
                        SourceAssertion(
                            id=context.new_id(),
                            workspace_id=ws,
                            source_observation_id=observation_id,
                            subject_hint=str(work.id),
                            predicate=Predicate.TITLE,
                            value_json=title,
                            work_id=work.id,
                        )
                    ]
                )
                ids.append(work.id)
            await refresh_summaries(uow, ws, ids, now=context.now())
            await uow.commit()

        token = await _login(api["client"], "steward@lab.invalid")
        merged = await api["client"].post(
            "/api/v1/decisions/merge",
            headers=_auth(token),
            json={
                "keep_work_id": str(ids[0]),
                "merge_work_id": str(ids[1]),
                "reason_code": "same_paper",
            },
        )
        assert merged.status_code == 200, merged.text
        listed = await api["client"].get(
            "/api/v1/candidates", headers=_auth(token), params={"identity_status": "merged"}
        )
        [row] = [i for i in listed.json()["items"] if i["work_id"] == str(ids[1])]
        assert row["title"] == titles[1]
        assert row["merged_into_work_id"] == str(ids[0])


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

    async def test_queues_and_the_audit_log_are_paged(self, api) -> None:
        steward = await _login(api["client"], "steward@lab.invalid")
        client = api["client"]
        for queue in ("waiting", "awaiting_user", "dead_letter"):
            page = await client.get(
                "/api/v1/operations/queue", params={"queue": queue}, headers=_auth(steward)
            )
            assert page.status_code == 200
            assert set(page.json()) == {"items", "total", "next_cursor"}
        bad = await client.get(
            "/api/v1/operations/queue", params={"queue": "everything"}, headers=_auth(steward)
        )
        assert bad.status_code == 422

        everything = (await client.get("/api/v1/audit-events", headers=_auth(steward))).json()
        assert everything["total"] >= 2
        assert sum(everything["action_counts"].values()) == everything["total"]
        seen, after = [], None
        while True:
            params = {"limit": 1, **({"after": after} if after else {})}
            page = (
                await client.get("/api/v1/audit-events", params=params, headers=_auth(steward))
            ).json()
            seen.extend(event["id"] for event in page["items"])
            after = page["next_cursor"]
            if after is None:
                break
        assert len(seen) == len(set(seen)) == everything["total"]
        stamps = [e["occurred_at"] for e in everything["items"]]
        assert stamps == sorted(stamps, reverse=True)

        action = everything["items"][0]["action"]
        narrowed = (
            await client.get(
                "/api/v1/audit-events", params={"action": action}, headers=_auth(steward)
            )
        ).json()
        assert narrowed["total"] == everything["action_counts"][action]
        assert {e["action"] for e in narrowed["items"]} == {action}
        bad = await client.get(
            "/api/v1/audit-events", params={"after": "not-a-cursor"}, headers=_auth(steward)
        )
        assert bad.status_code == 422

    async def test_every_long_list_answers_one_page_with_a_total(self, api) -> None:
        steward = await _login(api["client"], "steward@lab.invalid")
        client = api["client"]
        for path in (
            "/api/v1/chat/sessions",
            "/api/v1/discovery-runs",
            "/api/v1/corpus-releases",
            "/api/v1/extraction/runs",
            "/api/v1/claims",
            "/api/v1/evaluation/runs",
            "/api/v1/evaluation/reviews/mine",
        ):
            page = await client.get(path, params={"limit": 1}, headers=_auth(steward))
            assert page.status_code == 200, path
            body = page.json()
            assert {"items", "total", "next_cursor"} <= set(body), path
            assert len(body["items"]) <= 1, path
            if body["total"] > 1:
                assert body["next_cursor"] is not None, path
        bad = await client.get(
            "/api/v1/discovery-runs", params={"after": "nonsense"}, headers=_auth(steward)
        )
        assert bad.status_code == 422

    async def test_collection_progress_is_for_stewards(self, api) -> None:
        researcher = await _login(api["client"], "researcher@lab.invalid")
        denied = await api["client"].get("/api/v1/collection-progress", headers=_auth(researcher))
        assert denied.status_code == 403
        steward = await _login(api["client"], "steward@lab.invalid")
        body = (
            await api["client"].get("/api/v1/collection-progress", headers=_auth(steward))
        ).json()
        assert [row["stage"] for row in body["funnel"]] == [
            "found",
            "included",
            "planned",
            "fetched",
            "parsed",
            "extracted",
            "served",
        ]
        assert {"discovery", "screening", "acquisition", "parsing", "extraction", "daily"} <= set(
            body
        )

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


_EVAL_BENCHMARK = {
    "benchmark_id": "api-retrieval",
    "version": "1.0.0",
    "kind": "retrieval",
    "description": "Round trip through the API.",
    "origin": "written for this test",
    "licence": "internal",
    "permitted_use": "testing",
    "cases": [{"case_id": "c1", "question": "lipid yield", "relevant": ["doi:10.1/absent"]}],
    "thresholds": {"recall@5": 0.5},
}


class TestEvaluation:
    """Register a benchmark, queue a run, let the worker finish it, read the result."""

    async def test_register_queue_and_read_a_run(self, api) -> None:
        from lab_assistant.application.context import resolve_actor
        from lab_assistant.testing import publish_demo_corpus

        harness, client = api["harness"], api["client"]
        steward = await resolve_actor(
            harness.context, user_id=api["steward"].id, workspace_id=api["workspace"].id
        )
        corpus = await publish_demo_corpus(harness, steward)
        headers = _auth(await _login(client, "steward@lab.invalid"))

        created = await client.post(
            "/api/v1/evaluation/benchmarks", json=_EVAL_BENCHMARK, headers=headers
        )
        assert created.status_code == 201, created.text
        assert created.json()["key"] == "api-retrieval@1.0.0"
        again = await client.post(
            "/api/v1/evaluation/benchmarks", json=_EVAL_BENCHMARK, headers=headers
        )
        assert (again.status_code, again.json()["created"]) == (200, False)

        queued = await client.post(
            "/api/v1/evaluation/runs",
            json={
                "benchmark_id": "api-retrieval",
                "version": "1.0.0",
                "release_id": str(corpus.release_id),
            },
            headers=headers,
        )
        assert queued.status_code == 202, queued.text
        assert queued.json()["status"] == "queued"

        await harness.run_all_tasks()
        run = await client.get(f"/api/v1/evaluation/runs/{queued.json()['id']}", headers=headers)
        body = run.json()
        assert body["status"] == "succeeded", body["error_detail"]
        # The gold DOI is not in the corpus: a recall failure, reported by case.
        assert body["passed"] is False
        assert body["per_case"][0]["unresolved_relevant"] == ["doi:10.1/absent"]

        listed = await client.get("/api/v1/evaluation/runs", headers=headers)
        assert [item["id"] for item in listed.json()["items"]] == [body["id"]]

    async def test_a_researcher_cannot_see_evaluation(self, api) -> None:
        headers = _auth(await _login(api["client"], "researcher@lab.invalid"))
        response = await api["client"].get("/api/v1/evaluation/benchmarks", headers=headers)
        assert response.status_code == 403

    async def test_an_invalid_file_says_what_is_wrong(self, api) -> None:
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        response = await api["client"].post(
            "/api/v1/evaluation/benchmarks", json={"kind": "retrieval"}, headers=headers
        )
        assert response.status_code == 422
        assert "not valid" in response.json()["detail"]


class TestExportDownload:
    async def _published(self, api):
        from lab_assistant.application.context import resolve_actor
        from lab_assistant.testing import publish_demo_corpus

        steward = await resolve_actor(
            api["harness"].context, user_id=api["steward"].id, workspace_id=api["workspace"].id
        )
        workspace = api["workspace"]
        for action in (PolicyAction.EXPORT_CLAIMS, PolicyAction.EXPORT_METADATA):
            await api["harness"].accounts.grant(
                steward,
                subject_type=SubjectType.WORKSPACE,
                subject_id=workspace.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace.id,
                action=action,
                effect=Effect.ALLOW,
                policy_basis="workspace_export",
            )
        return await publish_demo_corpus(api["harness"], steward)

    async def test_the_whole_package_downloads_as_a_zip(self, api) -> None:
        import io
        import zipfile

        corpus = await self._published(api)
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        built = await api["client"].post(f"/api/v1/exports/{corpus.release_id}", headers=headers)
        response = await api["client"].get(
            f"/api/v1/exports/{corpus.release_id}/download", headers=headers
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
        assert sorted(names) == built.json()["files"]

    async def test_one_file_downloads_with_its_type(self, api) -> None:
        corpus = await self._published(api)
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        files = (
            await api["client"].post(f"/api/v1/exports/{corpus.release_id}", headers=headers)
        ).json()["files"]
        # The CSV is always there; the XLSX beside it needs openpyxl (`parsers`).
        assert "data/wide-form.csv" in files
        response = await api["client"].get(
            f"/api/v1/exports/{corpus.release_id}/download",
            params={"file": "data/wide-form.csv"},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers["content-disposition"].endswith('-wide-form.csv"')

    async def test_an_unknown_file_is_not_found(self, api) -> None:
        corpus = await self._published(api)
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        response = await api["client"].get(
            f"/api/v1/exports/{corpus.release_id}/download",
            params={"file": "../../etc/passwd"},
            headers=headers,
        )
        assert response.status_code == 404

    async def test_a_researcher_cannot_download(self, api) -> None:
        corpus = await self._published(api)
        headers = _auth(await _login(api["client"], "researcher@lab.invalid"))
        response = await api["client"].get(
            f"/api/v1/exports/{corpus.release_id}/download", headers=headers
        )
        assert response.status_code == 403


class TestBulkImport:
    async def test_a_dry_run_reports_each_file(self, api) -> None:
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        response = await api["client"].post(
            "/api/v1/bulk-imports",
            headers=headers,
            data={"dry_run": "true"},
            files=[
                ("files", ("10.1234_nothing.waiting.pdf", b"%PDF-1.7", "application/pdf")),
                ("files", ("notes.pdf", b"%PDF-1.7", "application/pdf")),
            ],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is True
        assert body["counts"] == {"unmatched": 2}
        assert [item["identifiers"] for item in body["items"]] == [
            ["doi:10.1234/nothing.waiting"],
            [],
        ]

    async def test_importing_without_an_attestation_is_refused(self, api) -> None:
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        response = await api["client"].post(
            "/api/v1/bulk-imports",
            headers=headers,
            files=[("files", ("a.pdf", b"%PDF-1.7", "application/pdf"))],
        )
        assert response.status_code == 422


class TestEvaluationWorkbench:
    async def test_a_required_benchmark_blocks_publishing_until_overridden(self, api) -> None:
        from lab_assistant.application.context import resolve_actor
        from lab_assistant.domain.states import ReleaseState
        from lab_assistant.testing import publish_demo_corpus

        harness, client = api["harness"], api["client"]
        steward = await resolve_actor(
            harness.context, user_id=api["steward"].id, workspace_id=api["workspace"].id
        )
        corpus = await publish_demo_corpus(harness, steward)
        async with harness.context.uow_factory() as uow:
            release = await uow.releases.get(steward.workspace_id, corpus.release_id)
            release.status = ReleaseState.APPROVED
            await uow.releases.update(release)
            await uow.commit()
        headers = _auth(await _login(client, "steward@lab.invalid"))
        await client.post("/api/v1/evaluation/benchmarks", json=_EVAL_BENCHMARK, headers=headers)

        required = await client.put(
            "/api/v1/evaluation/release-gates",
            json={"benchmark_id": "api-retrieval", "version": "1.0.0"},
            headers=headers,
        )
        assert [gate["key"] for gate in required.json()["items"]] == ["api-retrieval@1.0.0"]
        status = await client.get(
            "/api/v1/evaluation/release-gates",
            params={"release_id": str(corpus.release_id)},
            headers=headers,
        )
        assert status.json()["status"][0]["passed"] is False

        refused = await client.post(
            f"/api/v1/corpus-releases/{corpus.release_id}/publish", headers=headers
        )
        assert refused.status_code == 422
        assert refused.json()["context"]["blockers"] == [
            "evaluation:api-retrieval@1.0.0 has not passed"
        ]
        published = await client.post(
            f"/api/v1/corpus-releases/{corpus.release_id}/publish",
            json={"override_reason": "gold set is being rewritten"},
            headers=headers,
        )
        assert published.status_code == 200, published.text

    async def test_reviewers_and_my_reviews_are_listed(self, api) -> None:
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        reviewers = await api["client"].get("/api/v1/evaluation/reviewers", headers=headers)
        # The researcher holds no reviewer capability; the admin does.
        assert [item["email"] for item in reviewers.json()["items"]] == ["steward@lab.invalid"]
        mine = await api["client"].get("/api/v1/evaluation/reviews/mine", headers=headers)
        assert mine.json()["items"] == []
        assert mine.json()["scores"][0] == "correctness"

    async def test_comparing_unknown_runs_is_not_found(self, api) -> None:
        from uuid import uuid4

        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        response = await api["client"].get(
            "/api/v1/evaluation/compare",
            params={"base": str(uuid4()), "candidate": str(uuid4())},
            headers=headers,
        )
        assert response.status_code == 404


class TestDatasheets:
    async def test_a_yaml_template_registers_approves_and_lists(self, api) -> None:
        from pathlib import Path

        example = Path(__file__).resolve().parents[3] / "configs/examples/templates"
        text = (example / "yeast-lipid-datasheet.yaml").read_text()
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        created = await api["client"].post(
            "/api/v1/extraction/templates", json={"yaml": text}, headers=headers
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "draft" and created.json()["created"] is True
        approved = await api["client"].post(
            "/api/v1/extraction/templates/yeast-lipid-datasheet/1.0.0/approve", headers=headers
        )
        assert approved.json()["active"] is True
        listed = await api["client"].get("/api/v1/extraction/templates", headers=headers)
        assert [item["key"] for item in listed.json()["items"]] == ["yeast-lipid-datasheet@1.0.0"]

    async def test_broken_yaml_says_so(self, api) -> None:
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        response = await api["client"].post(
            "/api/v1/extraction/templates", json={"yaml": "targets: [unclosed"}, headers=headers
        )
        assert response.status_code == 422
        assert "YAML" in response.json()["detail"]

    async def test_nothing_runs_without_an_approved_template(self, api) -> None:
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        response = await api["client"].post("/api/v1/extraction/runs", json={}, headers=headers)
        assert response.status_code == 422
        assert "no extraction template is approved" in response.json()["detail"]

    async def test_the_working_sheet_downloads_as_csv(self, api) -> None:
        from pathlib import Path

        example = Path(__file__).resolve().parents[3] / "configs/examples/templates"
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        await api["client"].post(
            "/api/v1/extraction/templates",
            json={"yaml": (example / "yeast-lipid-datasheet.yaml").read_text()},
            headers=headers,
        )
        await api["client"].post(
            "/api/v1/extraction/templates/yeast-lipid-datasheet/1.0.0/approve", headers=headers
        )
        rows = await api["client"].get("/api/v1/extraction/rows", headers=headers)
        assert rows.json()["claims"] == 0
        assert rows.json()["wide"]["columns"][:3] == ["work_id", "experiment_id", "revision_ids"]
        csv = await api["client"].get(
            "/api/v1/extraction/rows.csv", params={"shape": "long"}, headers=headers
        )
        assert csv.headers["content-type"].startswith("text/csv")
        assert csv.text.startswith("work_id,experiment_id,claim_id")

    async def test_a_researcher_sees_no_templates(self, api) -> None:
        headers = _auth(await _login(api["client"], "researcher@lab.invalid"))
        response = await api["client"].get("/api/v1/extraction/templates", headers=headers)
        assert response.status_code == 403


class TestScopeLookup:
    async def test_queries_are_written_per_source(self, api) -> None:
        headers = _auth(await _login(api["client"], "steward@lab.invalid"))
        response = await api["client"].post(
            "/api/v1/lookup/queries",
            json={"organism_terms": ["Yarrowia lipolytica"], "product_terms": ["oleic acid"]},
            headers=headers,
        )
        assert response.status_code == 200
        assert set(response.json()["queries"]) == {
            "pubmed",
            "europepmc",
            "openalex",
            "datacite",
            "crossref",
        }

    async def test_a_researcher_cannot_build_scope_queries(self, api) -> None:
        headers = _auth(await _login(api["client"], "researcher@lab.invalid"))
        response = await api["client"].post(
            "/api/v1/lookup/queries", json={"organism_terms": ["x"]}, headers=headers
        )
        assert response.status_code == 403


class TestCollectionsArePaged:
    """A large run's candidates, plans and duplicate pairs arrive a page at a time."""

    async def _seed(self, api) -> None:
        import itertools

        from lab_assistant.domain.assets import AcquisitionPlan
        from lab_assistant.domain.identity import (
            DUPLICATE_PROPOSAL_REASON,
            IdentityDecision,
            IdentityDecisionType,
            Work,
            WorkKind,
        )
        from lab_assistant.domain.states import IdentityState
        from lab_assistant.domain.states import RelevanceState as Relevance

        context, ws = api["harness"].context, api["workspace"].id
        async with context.uow_factory() as uow:
            works = []
            for index in range(7):
                work = Work(
                    id=context.new_id(),
                    workspace_id=ws,
                    work_kind=WorkKind.ARTICLE,
                    relevance=Relevance.INCLUDED if index < 4 else Relevance.INCLUDED_UNCERTAIN,
                    identity_status=(
                        IdentityState.CONFLICT if index < 2 else IdentityState.RESOLVED
                    ),
                    created_at=context.now(),
                )
                await uow.works.add(work)
                works.append(work)
                await uow.assets.add_plan(
                    AcquisitionPlan(
                        id=context.new_id(),
                        workspace_id=ws,
                        work_id=work.id,
                        status="assisted" if index < 3 else "planned",
                    )
                )
            for left, right in itertools.pairwise(works):
                await uow.works.add_decision(
                    IdentityDecision(
                        id=context.new_id(),
                        workspace_id=ws,
                        decision_type=IdentityDecisionType.REOPEN,
                        subject_work_ids=(left.id, right.id),
                        after={"proposal": "duplicate_review", "similarity": 0.9},
                        reason_code=DUPLICATE_PROPOSAL_REASON,
                        decided_by_type="automatic_rule",
                        decided_at=context.now(),
                    )
                )
            await uow.commit()
        # Proposals stored before the review queue existed reach it by backfill.
        from lab_assistant.application.bulk_review import BulkReviewService

        await BulkReviewService(context).backfill(ws)

    @staticmethod
    async def _walk(client, token, path: str, key: str) -> tuple[list, list[int]]:
        seen, totals, cursor = [], [], None
        for _ in range(20):
            params = {"limit": 3, **({"after": cursor} if cursor else {})}
            body = (await client.get(path, headers=_auth(token), params=params)).json()
            seen.extend(body[key])
            totals.append(body.get("total", body.get("proposal_total")))
            cursor = body["next_cursor"]
            if cursor is None:
                return seen, totals
        raise AssertionError("paging never ended")

    async def test_candidates_page_with_a_total_and_filters(self, api) -> None:
        await self._seed(api)
        token = await _login(api["client"], "steward@lab.invalid")
        seen, totals = await self._walk(api["client"], token, "/api/v1/candidates", "items")
        ids = [item["work_id"] for item in seen]
        assert len(ids) == 7 and len(set(ids)) == 7
        assert set(totals) == {7}

        filtered = await api["client"].get(
            "/api/v1/candidates",
            headers=_auth(token),
            params={"relevance": "included", "identity_status": "conflict"},
        )
        body = filtered.json()
        assert body["total"] == 2 and len(body["items"]) == 2
        assert {item["identity_status"] for item in body["items"]} == {"conflict"}
        assert body["next_cursor"] is None

        refused = await api["client"].get(
            "/api/v1/candidates", headers=_auth(token), params={"relevance": "maybe"}
        )
        assert refused.status_code == 422

    async def test_plans_and_duplicate_pairs_page_too(self, api) -> None:
        await self._seed(api)
        token = await _login(api["client"], "steward@lab.invalid")
        plans, totals = await self._walk(api["client"], token, "/api/v1/acquisition-plans", "items")
        assert len({plan["plan_id"] for plan in plans}) == 7 and set(totals) == {7}
        counts = (
            await api["client"].get("/api/v1/acquisition-plans", headers=_auth(token))
        ).json()["status_counts"]
        assert counts == {"assisted": 3, "planned": 4}
        assisted = (
            await api["client"].get(
                "/api/v1/acquisition-plans", headers=_auth(token), params={"status": "assisted"}
            )
        ).json()
        assert assisted["total"] == 3 and len(assisted["items"]) == 3

        pairs, totals = await self._walk(
            api["client"], token, "/api/v1/candidates/conflicts", "duplicate_proposals"
        )
        assert len(pairs) == 6 and set(totals) == {6}
        first = (
            await api["client"].get("/api/v1/candidates/conflicts", headers=_auth(token))
        ).json()
        assert first["conflict_total"] == 2


class TestAcquisitionQueueDownload:
    """The queue's search as one CSV, or as a zip of CSVs of at most 100 rows."""

    async def _seed(self, api, count: int) -> None:
        from lab_assistant.domain.assets import AcquisitionPlan
        from lab_assistant.domain.identity import Work, WorkKind
        from lab_assistant.domain.states import RelevanceState as Relevance

        context, ws = api["harness"].context, api["workspace"].id
        async with context.uow_factory() as uow:
            for index in range(count):
                work = Work(
                    id=context.new_id(),
                    workspace_id=ws,
                    work_kind=WorkKind.ARTICLE,
                    relevance=Relevance.INCLUDED,
                    created_at=context.now(),
                )
                await uow.works.add(work)
                await uow.assets.add_plan(
                    AcquisitionPlan(
                        id=context.new_id(),
                        workspace_id=ws,
                        work_id=work.id,
                        status="assisted" if index % 3 else "fetched",
                        created_at=context.now(),
                    )
                )
            await uow.commit()

    @staticmethod
    def _rows(data: bytes) -> list[dict[str, str]]:
        import csv
        import io

        return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))

    async def test_one_csv_of_every_plan_the_search_finds(self, api) -> None:
        await self._seed(api, 7)
        client = api["client"]
        token = await _login(client, "steward@lab.invalid")
        whole = await client.get("/api/v1/acquisition-plans/export.csv", headers=_auth(token))
        assert whole.status_code == 200
        assert whole.headers["content-type"].startswith("text/csv")
        assert whole.headers["content-disposition"].startswith(
            'attachment; filename="acquisition-queue-'
        )
        rows = self._rows(whole.content)
        assert len(rows) == 7
        assert {"title", "doi", "url", "status", "last_outcome", "plan_id"} <= set(rows[0])

        assisted = await client.get(
            "/api/v1/acquisition-plans/export.csv",
            headers=_auth(token),
            params={"status": "assisted", "outcome": "none"},
        )
        assert {row["status"] for row in self._rows(assisted.content)} == {"assisted"}
        assert len(self._rows(assisted.content)) == 4
        assert 'assisted.csv"' in assisted.headers["content-disposition"]

        # Nothing but a status's own characters reaches the header.
        odd = await client.get(
            "/api/v1/acquisition-plans/export.csv",
            headers=_auth(token),
            params={"status": 'x"; y'},
        )
        assert odd.status_code == 200 and self._rows(odd.content) == []
        assert odd.headers["content-disposition"].endswith('-xy.csv"')

        assert (
            await client.get("/api/v1/acquisition-plans/export.pdf", headers=_auth(token))
        ).status_code == 422
        assert (await client.get("/api/v1/acquisition-plans/export.csv")).status_code == 401

    async def test_a_zip_of_csvs_of_at_most_a_hundred_rows(self, api) -> None:
        import io
        import zipfile

        await self._seed(api, 205)
        client = api["client"]
        token = await _login(client, "steward@lab.invalid")
        response = await client.get("/api/v1/acquisition-plans/export.zip", headers=_auth(token))
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = archive.namelist()
        assert len(names) == 3 and names == sorted(names)
        assert names[0].startswith("acquisition-queue-") and names[0].endswith("-001.csv")
        sizes = [len(self._rows(archive.read(name))) for name in names]
        assert sizes == [100, 100, 5]
        every = [row["plan_id"] for name in names for row in self._rows(archive.read(name))]
        assert len(set(every)) == 205


class TestTemporaryPasswords:
    """An invitation by temporary password: sent by hand, used once, then replaced."""

    async def _invite(self, client, admin: str, email: str, **extra) -> dict[str, Any]:
        created = await client.post(
            "/api/v1/invitations",
            json={"email": email, "role": "reviewer", "temporary_password": True, **extra},
            headers=_auth(admin),
        )
        assert created.status_code == 201, created.text
        return created.json()

    async def test_signing_in_with_it_asks_for_a_password_of_ones_own(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        created = await self._invite(client, admin, "new.person@lab.invalid")
        temporary = created["token"]
        assert created["kind"] == "temporary_password"
        # Four groups of four, from letters and digits that cannot be confused.
        assert len(temporary) == 19 and temporary.count("-") == 3
        assert not set(temporary) & set("01ilo")

        # Typed in capitals and without the dashes, it still works.
        typed = temporary.replace("-", "").upper()
        challenged = await client.post(
            "/api/v1/auth/login", json={"email": "New.Person@lab.invalid", "password": typed}
        )
        assert challenged.status_code == 401
        assert challenged.json()["context"]["reason"] == "password_change_required"
        assert "access_token" not in challenged.json()

        same = await client.post(
            "/api/v1/auth/first-sign-in",
            json={
                "email": "new.person@lab.invalid",
                "temporary_password": typed,
                "password": temporary,
            },
        )
        assert same.status_code == 422
        chosen = await client.post(
            "/api/v1/auth/first-sign-in",
            json={
                "email": "new.person@lab.invalid",
                "temporary_password": temporary,
                "password": PASSWORD,
                "full_name": "New Person",
            },
        )
        assert chosen.status_code == 200, chosen.text
        me = await client.get("/api/v1/auth/me", headers=_auth(chosen.json()["access_token"]))
        assert (me.json()["role"], me.json()["full_name"]) == ("reviewer", "New Person")

        # Used once: the temporary password is now just a wrong password.
        again = await client.post(
            "/api/v1/auth/login",
            json={"email": "new.person@lab.invalid", "password": temporary},
        )
        assert again.status_code == 401
        assert "reason" not in again.json().get("context", {})
        assert await _login(client, "new.person@lab.invalid")
        listed = (await client.get("/api/v1/invitations", headers=_auth(admin))).json()["items"]
        assert [(row["kind"], row["state"]) for row in listed] == [
            ("temporary_password", "accepted")
        ]

    async def test_it_works_only_with_its_own_address(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        temporary = (await self._invite(client, admin, "new.person@lab.invalid"))["token"]
        other = await client.post(
            "/api/v1/auth/login", json={"email": "someone@lab.invalid", "password": temporary}
        )
        assert other.status_code == 401
        assert "reason" not in other.json().get("context", {})
        refused = await client.post(
            "/api/v1/auth/first-sign-in",
            json={
                "email": "someone@lab.invalid",
                "temporary_password": temporary,
                "password": PASSWORD,
            },
        )
        assert refused.status_code == 403

    async def test_an_expired_one_says_so_and_a_new_one_replaces_it(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        first = await self._invite(client, admin, "late@lab.invalid", ttl_hours=48)
        api["harness"].clock.advance(49 * 3600)

        expired = await client.post(
            "/api/v1/auth/login", json={"email": "late@lab.invalid", "password": first["token"]}
        )
        assert expired.status_code == 401
        assert expired.json()["context"]["reason"] == "temporary_password_expired"

        admin = await _login(client, "steward@lab.invalid")
        renewed = await client.post(
            f"/api/v1/invitations/{first['id']}/regenerate", headers=_auth(admin)
        )
        assert renewed.status_code == 201, renewed.text
        second = renewed.json()
        assert second["token"] != first["token"]
        assert (second["role"], second["kind"]) == ("reviewer", "temporary_password")
        # As long as the first was given: two days.
        assert second["expires_at"] > first["expires_at"]

        challenged = await client.post(
            "/api/v1/auth/login", json={"email": "late@lab.invalid", "password": second["token"]}
        )
        assert challenged.json()["context"]["reason"] == "password_change_required"

        # Renewing again withdraws the second, which then says it was replaced.
        await client.post(f"/api/v1/invitations/{second['id']}/regenerate", headers=_auth(admin))
        replaced = await client.post(
            "/api/v1/auth/login", json={"email": "late@lab.invalid", "password": second["token"]}
        )
        assert replaced.json()["context"]["reason"] == "temporary_password_revoked"

    async def test_an_address_with_an_account_is_sent_a_link_instead(self, api) -> None:
        client = api["client"]
        await api["harness"].accounts.create_user(email="elsewhere@lab.invalid", password=PASSWORD)
        admin = await _login(client, "steward@lab.invalid")
        refused = await client.post(
            "/api/v1/invitations",
            json={"email": "elsewhere@lab.invalid", "temporary_password": True},
            headers=_auth(admin),
        )
        assert refused.status_code == 422
        assert "invitation link" in refused.json()["detail"]

    async def test_a_link_has_no_password_to_renew(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        link = (
            await client.post(
                "/api/v1/invitations", json={"email": "link@lab.invalid"}, headers=_auth(admin)
            )
        ).json()
        assert link["kind"] == "link"
        refused = await client.post(
            f"/api/v1/invitations/{link['id']}/regenerate", headers=_auth(admin)
        )
        assert refused.status_code == 409


class TestInvitationEmail:
    """The workspace's wording of the email an invitation is sent in."""

    async def test_it_starts_as_the_products_wording_and_is_saved_per_kind(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        first = (await client.get("/api/v1/invitation-email", headers=_auth(admin))).json()
        assert first["templates"] == {"link": None, "temporary_password": None}
        assert first["set_at"] is None

        wording = {"subject": "Join {workspace}", "body": "Hello,\r\n\r\nOpen {link}\r\n"}
        saved = await client.put(
            "/api/v1/invitation-email", json={"link": wording}, headers=_auth(admin)
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["templates"] == {
            "link": {"subject": "Join {workspace}", "body": "Hello,\n\nOpen {link}"},
            "temporary_password": None,
        }
        assert saved.json()["set_by"] == str(api["steward"].id)

        # Saving null restores the product's wording for that kind.
        restored = await client.put("/api/v1/invitation-email", json={}, headers=_auth(admin))
        assert restored.json()["templates"]["link"] is None

    async def test_an_email_without_what_it_sends_is_refused(self, api) -> None:
        client = api["client"]
        admin = await _login(client, "steward@lab.invalid")
        refused = await client.put(
            "/api/v1/invitation-email",
            json={"temporary_password": {"subject": "Welcome", "body": "Sign in at {link}"}},
            headers=_auth(admin),
        )
        assert refused.status_code == 422
        assert "{password}" in refused.json()["detail"]
        assert (await client.get("/api/v1/invitation-email", headers=_auth(admin))).json()[
            "templates"
        ]["temporary_password"] is None

    async def test_only_those_who_manage_members_see_or_change_it(self, api) -> None:
        client = api["client"]
        researcher = await _login(client, "researcher@lab.invalid")
        assert (
            await client.get("/api/v1/invitation-email", headers=_auth(researcher))
        ).status_code == 403
        assert (
            await client.put("/api/v1/invitation-email", json={}, headers=_auth(researcher))
        ).status_code == 403
