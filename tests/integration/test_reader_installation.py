"""What a reader installation refuses, and what it must still do.

Half of this file is the second half, and it is the half that is easy to skip.
An enforcement pass that denied too much would look correct — every refusal
test green — and leave an installation that cannot answer a question. A reader
exists to serve; the point of the role is that it serves *without* being able
to change the corpus.

So: it writes logins, chat sessions, messages, feedback and audit, and it
searches, and search is itself a POST. "Read-only" is the wrong mental model
and blocking writes would be the wrong implementation (audit F08).
"""

from __future__ import annotations

import pytest

from lab_assistant.application.releases import ReleaseService
from lab_assistant.application.scopes import ScopeService
from lab_assistant.contracts.errors import InstallationDenied
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.installation import InstallationRole
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor, publish_demo_corpus

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)


async def _provision(harness):
    """An admin with every grant, so a refusal can only come from the role."""
    user = await harness.accounts.create_user(
        email="steward@lab.invalid", password="correct horse battery"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
    )
    admin = make_actor(user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN)
    grants = [
        await harness.accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=action,
            effect=Effect.ALLOW,
            policy_basis="reader_role_test",
        )
        for action in GRANTED
    ]
    return make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )


@pytest.fixture
async def reader():
    harness = build_harness(installation_role=InstallationRole.READER)
    actor = await _provision(harness)
    return harness, actor


@pytest.fixture
async def publisher():
    harness = build_harness(installation_role=InstallationRole.PUBLISHER)
    actor = await _provision(harness)
    return harness, actor


class TestAReaderRefusesCorpusChanges:
    """An admin is still an admin. The limit is the machine, not the caller."""

    async def test_it_will_not_publish(self, reader) -> None:
        harness, actor = reader
        with pytest.raises(InstallationDenied) as raised:
            await ReleaseService(harness.context).publish(actor, release_id=actor.workspace_id)
        assert raised.value.context["installation_role"] == "reader"
        assert raised.value.context["required_capability"] == "publish_releases"

    async def test_it_will_not_curate(self, reader) -> None:
        harness, actor = reader
        with pytest.raises(InstallationDenied):
            await ScopeService(harness.context).create_version(
                actor,
                scope_id=None,
                name="anything",
                scientific_objective="a scope a reader must not define",
                connector_plan=[],
            )

    async def test_the_refusal_is_not_a_permissions_problem(self, reader) -> None:
        """`InstallationDenied`, never `PolicyDenied`.

        A client that confused them would offer the user a permissions dialog
        for something no permission can fix — the capability is on another
        installation.
        """
        harness, actor = reader
        with pytest.raises(InstallationDenied) as raised:
            await ReleaseService(harness.context).rollback(actor)
        # 501: not implemented *here*. 403 would invite a retry with more rights.
        assert raised.value.http_status == 501

    async def test_it_claims_no_tasks(self, reader) -> None:
        """The case that looks harmless: a reader restored from a publisher's
        backup inherits a queue full of ready work."""
        harness, _actor = reader
        with pytest.raises(InstallationDenied) as raised:
            await harness.orchestrator.run_once(owner="probe", worker_class="network")
        assert raised.value.context["required_capability"] == "run_workers"

    async def test_a_publisher_does_all_of_these(self, publisher) -> None:
        """The refusals above must come from the role and nothing else."""
        harness, actor = publisher
        # Reaches the real logic rather than the role guard: a missing release
        # is a NotFound, not an InstallationDenied.
        with pytest.raises(Exception) as raised:
            await ReleaseService(harness.context).publish(actor, release_id=actor.workspace_id)
        assert not isinstance(raised.value, InstallationDenied)

        result = await harness.orchestrator.run_once(owner="probe", worker_class="network")
        assert result.idle is True


class TestAReaderStillServes:
    """The half that matters most. A reader that cannot serve is not a reader."""

    async def test_it_answers_a_question_with_a_citation(self, publisher) -> None:
        """Built on a publisher, then served by a reader over the same state —
        which is what an imported corpus will look like."""
        harness, actor = publisher
        await publish_demo_corpus(harness, actor)

        served = build_harness(
            installation_role=InstallationRole.READER,
            uow_factory=harness.context.uow_factory,
            object_store=harness.object_store,
            search_index=harness.search,
        )
        from lab_assistant.application.chat import ChatService

        chat = ChatService(served.context)
        session = await chat.create_session(actor)
        answer = await chat.ask(
            actor, session_id=session.id, question="What lipid yield was reported?"
        )
        assert answer.evidence_count > 0, "a reader must be able to answer with evidence"
        assert answer.unsupported_handles == ()

    async def test_it_records_interactions(self, reader) -> None:
        """Logins, sessions, messages, feedback — all writes, all permitted."""
        from lab_assistant.application.chat import ChatService

        harness, actor = reader
        chat = ChatService(harness.context)
        session = await chat.create_session(actor)
        assert session.id is not None
        renamed = await chat.rename_session(actor, session_id=session.id, title="kept")
        assert renamed.title == "kept"

    async def test_it_needs_no_connectors_scanner_or_extractor(self) -> None:
        """A reader serves a corpus somebody else approved, so it should not
        require source API keys or a ClamAV socket to start."""
        from lab_assistant.config import Settings
        from lab_assistant_api.deps import build_container

        container = build_container(Settings(installation_role="reader", database_url="memory://"))
        context = container.context
        assert context.connectors is None
        assert context.parsers is None
        assert context.extractor is None
        assert context.malware_scanner is None
        # Still able to answer: retrieval and chat use the provider directly.
        assert context.llm_provider is not None
        assert context.embedding_provider is not None

    async def test_a_publisher_builds_all_of_them(self) -> None:
        from lab_assistant.config import Settings
        from lab_assistant_api.deps import build_container

        context = build_container(
            Settings(installation_role="publisher", database_url="memory://")
        ).context
        assert context.connectors is not None
        assert context.parsers is not None
        assert context.extractor is not None


class TestTheWorkerEntryPoint:
    async def test_it_refuses_to_start_on_a_reader(self) -> None:
        """Whether workers run is a deployment decision rather than a setting,
        so the refusal lives where one actually starts."""
        from types import SimpleNamespace

        from lab_assistant.cli.main import _worker
        from lab_assistant.config import Settings

        code = await _worker(
            SimpleNamespace(worker_class="network", once=True, owner=None),
            Settings(installation_role="reader", database_url="memory://"),
        )
        assert code == 2
