"""The operator surface the runbooks tell someone to type, tested.

Every command here is named by a runbook in ``docs/operations/runbooks/``. They
exist as commands rather than as psql recipes because a hand-written ``UPDATE``
leaves an unexplained state and an audit chain that still verifies while being
incomplete — so the commands themselves need coverage, not only the services
underneath them.

``lab demo`` is covered here too: the README offers it as proof the stack works,
and a demo that quietly stops proving anything is a broken promise nobody notices.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lab_assistant.application.releases import ReleaseService
from lab_assistant.cli.main import (
    _demo,
    _release_references,
    _task,
    _verify_audit,
    _verify_restore,
)
from lab_assistant.contracts.errors import (
    NotFound,
    StateTransitionError,
    ValidationFailure,
)
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.states import ReleaseState, TaskState
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor, publish_demo_corpus


@pytest.fixture
async def deployment() -> dict[str, Any]:
    """A workspace with one published release, as an operator would find it."""
    harness = build_harness()
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    grants = [
        await harness.accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=action,
            effect=Effect.ALLOW,
            policy_basis="workspace_corpus",
        )
        for action in (
            PolicyAction.QUERY,
            PolicyAction.VIEW_CONTENT,
            PolicyAction.INDEX,
            PolicyAction.DISPLAY_EXCERPT,
        )
    ]
    actor = make_actor(
        user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    corpus = await publish_demo_corpus(harness, actor)
    async with harness.context.uow_factory() as uow:
        renditions = await uow.renditions.list_renditions(workspace.id)
    return {
        "harness": harness,
        "actor": actor,
        "workspace": workspace,
        "rendition": renditions[0],
        "release_id": corpus.release_id,
        "served": corpus.served_release_id,
        "work_ids": corpus.work_ids,
        # What the CLI helpers take: the composition root, not the whole API.
        "container": SimpleNamespace(
            context=harness.context, orchestrator=harness.orchestrator, shutdown=[]
        ),
    }


class TestWithdraw:
    async def test_refuses_while_an_alias_still_serves_the_release(self, deployment) -> None:
        service = ReleaseService(deployment["harness"].context)
        with pytest.raises(StateTransitionError) as raised:
            await service.withdraw(deployment["actor"], release_id=deployment["release_id"])
        # The operator is told what to do first, not merely that it failed.
        assert "roll the alias back" in str(raised.value)
        assert raised.value.context["aliases"] == ["current"]

    async def test_withdrawn_is_terminal_and_audited(self, deployment) -> None:
        harness, actor = deployment["harness"], deployment["actor"]
        service = ReleaseService(harness.context)
        # Publishing a successor supersedes the first release and moves the alias.
        second = await publish_demo_corpus(
            harness,
            actor,
            works=(
                (
                    "Titre of an engineered strain on glucose",
                    "Strain YL-2 reached a lipid titre of 12 g/L on glucose.",
                ),
            ),
            semantic_version="1.1.0",
        )
        assert second.release_id != deployment["release_id"]

        withdrawn = await service.withdraw(
            actor, release_id=deployment["release_id"], reason="bad_claim_shipped"
        )
        assert withdrawn.status is ReleaseState.WITHDRAWN

        # Terminal: publication refuses rather than transitioning, and the release
        # stays withdrawn either way, so nobody republishes it without having read
        # the incident write-up.
        with pytest.raises((StateTransitionError, ValidationFailure)):
            await service.publish(actor, release_id=deployment["release_id"])
        async with harness.context.uow_factory() as uow:
            still = await uow.releases.get(actor.workspace_id, deployment["release_id"])
        assert still is not None
        assert still.status is ReleaseState.WITHDRAWN

        actions = [event.action.value for event in harness.db.audit_events]
        assert actions.count("release_withdrawn") == 1

    async def test_a_missing_release_is_not_found(self, deployment) -> None:
        service = ReleaseService(deployment["harness"].context)
        with pytest.raises(NotFound):
            await service.withdraw(
                deployment["actor"], release_id=deployment["workspace"].id  # any absent id
            )


class TestReleaseReferences:
    async def test_a_served_digest_is_not_safe_to_delete(self, deployment, capsys) -> None:
        code = await _release_references(
            deployment["container"],
            deployment["workspace"].id,
            digest=deployment["rendition"].sha256,
        )
        payload = capsys.readouterr().out
        # Non-zero so a deletion script can gate on it.
        assert code == 1
        assert '"safe_to_delete": false' in payload
        assert str(deployment["release_id"]) in payload
        assert '"served_as": "current"' in payload

    async def test_an_unreferenced_digest_is_safe(self, deployment, capsys) -> None:
        code = await _release_references(
            deployment["container"], deployment["workspace"].id, digest="0" * 64
        )
        assert code == 0
        assert '"safe_to_delete": true' in capsys.readouterr().out


class TestVerifyRestore:
    async def test_a_coherent_deployment_verifies(self, deployment, capsys) -> None:
        code = await _verify_restore(deployment["container"], deployment["workspace"].id)
        captured = capsys.readouterr()
        assert code == 0
        assert '"audit_chain": "ok"' in captured.out
        assert '"problems": []' in captured.out
        assert '"objects_checked": 1' in captured.out
        assert "restore verified" in captured.err

    async def test_a_manifest_digest_that_does_not_recompute_fails(
        self, deployment, capsys
    ) -> None:
        harness = deployment["harness"]
        async with harness.context.uow_factory() as uow:
            release = await uow.releases.get(
                deployment["workspace"].id, deployment["release_id"]
            )
            assert release is not None
            release.manifest_sha256 = "f" * 64
            await uow.releases.update(release)
            await uow.commit()

        code = await _verify_restore(deployment["container"], deployment["workspace"].id)
        captured = capsys.readouterr()
        assert code == 1
        assert "the release records ffffffffffff" in captured.out
        # The operator is told not to serve it, rather than left to infer that.
        assert "do not serve this restore" in captured.err

    async def test_a_broken_audit_chain_is_reported(self, deployment, capsys) -> None:
        harness = deployment["harness"]
        events = harness.db.audit_events
        assert events, "the fixture should have produced audit events"
        # Tamper the way a partial restore or a hand-edit would.
        events[-1] = events[-1].model_copy(update={"integrity_hash": "0" * 64})

        ok, problems = await _verify_audit(deployment["container"], deployment["workspace"].id)
        assert not ok
        assert problems

        code = await _verify_restore(deployment["container"], deployment["workspace"].id)
        assert code == 1
        assert '"audit_chain": "failed"' in capsys.readouterr().out


class TestTaskCommands:
    async def test_inspect_reports_the_dimensions_a_runbook_asks_for(
        self, deployment, capsys
    ) -> None:
        harness, actor = deployment["harness"], deployment["actor"]
        task = await harness.orchestrator.submit(
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            stage=StageName.INDEXING,
            inputs={"release_id": str(deployment["release_id"])},
        )
        code = await _task(
            _args(workspace="rla-lab", task_command="inspect", task=str(task.id)),
            harness.context.settings,
            container=deployment["container"],
        )
        payload = capsys.readouterr().out
        assert code == 0
        for key in ("state", "attempt", "lease_owner", "not_before", "input_fingerprint"):
            assert f'"{key}"' in payload
        # Waiting for a human is not a failure, and the command says so explicitly.
        assert '"counts_as_failure": false' in payload

    async def test_cancel_refuses_a_task_that_is_already_terminal(
        self, deployment, capsys
    ) -> None:
        harness, actor = deployment["harness"], deployment["actor"]
        task = await harness.orchestrator.submit(
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            stage=StageName.EXTRACTION,
            inputs={"work_id": str(actor.user_id)},
        )
        first = await _task(
            _args(
                workspace="rla-lab",
                task_command="cancel",
                task=str(task.id),
                reason="poisoned_input",
            ),
            harness.context.settings,
            container=deployment["container"],
        )
        assert first == 0
        async with harness.context.uow_factory() as uow:
            stored = await uow.tasks.get(actor.workspace_id, task.id)
        assert stored is not None
        assert stored.state is TaskState.CANCELLED
        assert stored.error_detail == "poisoned_input"

        second = await _task(
            _args(workspace="rla-lab", task_command="cancel", task=str(task.id)),
            harness.context.settings,
            container=deployment["container"],
        )
        assert second == 1
        assert "already cancelled" in capsys.readouterr().err

    async def test_a_missing_task_is_an_error_not_a_silent_success(
        self, deployment, capsys
    ) -> None:
        code = await _task(
            _args(
                workspace="rla-lab",
                task_command="inspect",
                task=str(deployment["workspace"].id),
            ),
            deployment["harness"].context.settings,
            container=deployment["container"],
        )
        assert code == 1
        assert "not found" in capsys.readouterr().err

    async def test_queue_reports_depth_without_a_worker_heartbeat(
        self, deployment, capsys
    ) -> None:
        harness, actor = deployment["harness"], deployment["actor"]
        await harness.orchestrator.submit(
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            stage=StageName.INDEXING,
            inputs={"release_id": str(deployment["release_id"])},
        )
        code = await _task(
            _args(workspace="rla-lab", task_command="queue"),
            harness.context.settings,
            container=deployment["container"],
        )
        payload = capsys.readouterr().out
        assert code == 0
        assert '"counts"' in payload
        assert '"oldest_pending_by_stage"' in payload
        assert '"awaiting_user"' in payload

    async def test_an_unknown_workspace_is_an_error_not_an_empty_queue(
        self, deployment, capsys
    ) -> None:
        code = await _task(
            _args(workspace="not-a-workspace", task_command="queue"),
            deployment["harness"].context.settings,
            container=deployment["container"],
        )
        assert code == 1
        assert "not found" in capsys.readouterr().err


class TestDemo:
    async def test_the_demo_answers_with_a_citation_that_resolves(self, capsys) -> None:
        code = await _demo(_args(question="What lipid yield was reported?"))
        captured = capsys.readouterr()
        assert code == 0, captured.err
        assert '"citations_resolved": 1' in captured.out
        assert '"unsupported_handles": []' in captured.out
        # The evidence itself is printed, not only a handle: a citation nobody can
        # read is the failure this system exists to prevent.
        assert "0.21 g/g" in captured.out


def _args(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(**values)
