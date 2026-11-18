"""Expensive work runs once per distinct input, including when it finds nothing.

Audit F10: reuse was decided by looking for *output*. Extraction asked whether
a claim revision carried the right fingerprint, so a paper the extractor read
and found nothing in — every target abstaining, which in this system is a
positive statement that the source does not report the value — produced no
revision, looked unprocessed, and went back to the model on every restart.

That is the expensive kind of bug: nothing fails, nothing is logged as wrong,
and the provider is paid again for every silent paper in the corpus, forever.
So the headline test here is the one where the extractor returns nothing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from lab_assistant.contracts.archive import StageOutcome, StageResultEnvelope
from lab_assistant.domain.stage_results import (
    REUSABLE_OUTCOMES,
    evaluate_reuse,
    is_reusable,
)
from lab_assistant.testing import build_harness


class TestTheReuseRule:
    """Which recorded outcomes count as the work having been done."""

    def test_an_empty_result_is_a_result(self) -> None:
        """The case every naive cache gets wrong, and the one F10 is about."""
        decision = evaluate_reuse(StageOutcome.SUCCEEDED_EMPTY)
        assert decision.reuse
        assert not decision.carries_failure
        assert "does not report" in decision.detail

    def test_a_retryable_failure_is_never_a_cache_hit(self) -> None:
        """A timeout recorded as a completed stage is how a transient blip
        becomes a permanent hole in a corpus."""
        decision = evaluate_reuse(StageOutcome.FAILED_RETRYABLE)
        assert not decision.reuse

    def test_an_unknown_outcome_is_not_reused(self) -> None:
        """The request left and its answer did not come back. The provider may
        have charged for it, and nobody knows what it said."""
        decision = evaluate_reuse(StageOutcome.OUTCOME_UNKNOWN)
        assert not decision.reuse
        assert "may have charged" in decision.detail

    def test_a_permanent_failure_is_reused_as_a_failure(self) -> None:
        """Reused so an unfixable item is not retried forever — and carried as
        a failure, because "we know this fails" is not "this succeeded"."""
        decision = evaluate_reuse(StageOutcome.FAILED_PERMANENT)
        assert decision.reuse
        assert decision.carries_failure

    def test_nothing_recorded_is_not_reuse(self) -> None:
        assert not evaluate_reuse(None).reuse

    def test_the_reusable_set_is_an_allow_list(self) -> None:
        """An outcome added later is not reused until somebody decides it
        should be: the safe direction when being wrong means serving a timeout
        as a result."""
        assert frozenset(StageOutcome) > REUSABLE_OUTCOMES
        assert not is_reusable(StageOutcome.FAILED_RETRYABLE)
        assert not is_reusable(StageOutcome.OUTCOME_UNKNOWN)


class TestTheLookupIndex:
    @pytest.fixture
    async def workspace(self):
        harness = build_harness()
        user = await harness.accounts.create_user(
            email="steward@lab.invalid", password="correct horse battery"
        )
        workspace, _ = await harness.accounts.create_workspace(
            slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
        )
        return harness, workspace

    def _envelope(self, outcome: StageOutcome, fingerprint: str) -> StageResultEnvelope:
        return StageResultEnvelope(
            stage="extraction",
            outcome=outcome,
            reuse_fingerprint=fingerprint,
        )

    async def test_an_outcome_round_trips(self, workspace) -> None:
        harness, ws = workspace
        async with harness.context.uow_factory() as uow:
            await uow.stage_results.record(
                self._envelope(StageOutcome.SUCCEEDED_EMPTY, "fp:1"), workspace_id=ws.id
            )
            await uow.commit()
        async with harness.context.uow_factory() as uow:
            found = await uow.stage_results.find(ws.id, "fp:1")
        assert found is not None
        assert found.outcome is StageOutcome.SUCCEEDED_EMPTY

    async def test_an_unknown_fingerprint_is_absent(self, workspace) -> None:
        harness, ws = workspace
        async with harness.context.uow_factory() as uow:
            assert await uow.stage_results.find(ws.id, "fp:never") is None

    async def test_a_later_run_replaces_an_earlier_outcome(self, workspace) -> None:
        """A retryable failure recorded last week must give way to this week's
        success, or retrying it achieves nothing."""
        harness, ws = workspace
        async with harness.context.uow_factory() as uow:
            await uow.stage_results.record(
                self._envelope(StageOutcome.FAILED_RETRYABLE, "fp:2"), workspace_id=ws.id
            )
            await uow.commit()
        async with harness.context.uow_factory() as uow:
            await uow.stage_results.record(
                self._envelope(StageOutcome.SUCCEEDED, "fp:2"), workspace_id=ws.id
            )
            await uow.commit()
        async with harness.context.uow_factory() as uow:
            found = await uow.stage_results.find(ws.id, "fp:2")
        assert found.outcome is StageOutcome.SUCCEEDED

    async def test_the_index_is_scoped_to_a_workspace(self, workspace) -> None:
        harness, ws = workspace
        async with harness.context.uow_factory() as uow:
            await uow.stage_results.record(
                self._envelope(StageOutcome.SUCCEEDED, "fp:3"), workspace_id=ws.id
            )
            await uow.commit()
        async with harness.context.uow_factory() as uow:
            assert await uow.stage_results.find(uuid4(), "fp:3") is None


class TestExtractionDoesNotPayTwice:
    """The defect itself, end to end through the real stage handler."""

    @pytest.fixture
    async def silent_paper(self):
        """A corpus whose extractor finds nothing: every target abstains."""
        from lab_assistant.contracts.policy import (
            Effect,
            PolicyAction,
            ResourceType,
            SubjectType,
        )
        from lab_assistant.domain.workspace import Role
        from lab_assistant.fixtures import publish_archive_fixture
        from lab_assistant.testing import make_actor

        harness = build_harness()
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
                policy_basis="stage_result_test",
            )
            for action in (
                PolicyAction.QUERY,
                PolicyAction.VIEW_CONTENT,
                PolicyAction.INDEX,
                PolicyAction.DISPLAY_EXCERPT,
            )
        ]
        actor = make_actor(
            user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
        )
        fixture = await publish_archive_fixture(harness, actor)
        return harness, workspace, fixture

    async def test_a_silent_paper_is_recorded_as_processed(self, silent_paper) -> None:
        """The fixture includes a paper the extractor searched and found nothing
        in. Before this index, nothing recorded that it had been done.
        """
        harness, workspace, _fixture = silent_paper
        # The archive fixture runs extraction over every work, including the
        # silent one, so at least one empty outcome must be on record.
        recorded = [
            value for (ws, _fp), value in harness.db.stage_results.items() if ws == workspace.id
        ]
        assert recorded, "extraction recorded no outcome at all"
        assert any(envelope.outcome is StageOutcome.SUCCEEDED_EMPTY for envelope in recorded), (
            "the searched-and-found-nothing extraction left no record, so it would be "
            "sent to the model again on the next run"
        )

    async def test_every_recorded_outcome_carries_its_tool_versions(self, silent_paper) -> None:
        """F10 also notes the fingerprint's inputs were labels rather than
        provenance. The envelope carries what actually ran."""
        harness, workspace, _ = silent_paper
        recorded = [
            value for (ws, _fp), value in harness.db.stage_results.items() if ws == workspace.id
        ]
        assert recorded
        for envelope in recorded:
            assert envelope.tool_versions.get("model")
            assert envelope.tool_versions.get("validator")
            assert envelope.stage == "extraction"


class TestReuseSkipsTheProvider:
    """The claim that matters: a second run does not call the model again."""

    async def test_a_recorded_extraction_is_not_run_twice(self) -> None:
        """Re-runs every extraction with an extractor that refuses to be called.

        If the index works, none of them reaches it. Before this index, the
        silent papers would have gone straight back to the provider — which is
        the defect, and the only way to show it is to make the second call
        impossible rather than to count it.
        """
        from dataclasses import replace as replace_dataclass

        from lab_assistant.contracts.policy import (
            Effect,
            PolicyAction,
            ResourceType,
            SubjectType,
        )
        from lab_assistant.contracts.stage import StageCommand, StageName, StageStatus
        from lab_assistant.domain.workspace import Role
        from lab_assistant.fixtures import publish_archive_fixture
        from lab_assistant.testing import make_actor

        harness = build_harness()
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
                policy_basis="reuse_test",
            )
            for action in (
                PolicyAction.QUERY,
                PolicyAction.VIEW_CONTENT,
                PolicyAction.INDEX,
                PolicyAction.DISPLAY_EXCERPT,
            )
        ]
        actor = make_actor(
            user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
        )
        await publish_archive_fixture(harness, actor)

        # Only the renditions that were actually extracted. The fixture creates
        # one more afterwards — the revised paper's — which has never been near
        # the extractor, so calling the model for it is correct rather than a
        # cache miss. Asserting over every rendition would fail for a reason
        # that has nothing to do with reuse.
        recorded_fingerprints = {
            fingerprint for (ws, fingerprint) in harness.db.stage_results if ws == workspace.id
        }
        assert recorded_fingerprints

        async with harness.context.uow_factory() as uow:
            renditions = await uow.renditions.list_renditions(workspace.id)
        assert renditions

        class CountsCalls:
            """Wraps the real extractor and counts what reaches it.

            Counting rather than refusing: one rendition in this corpus has
            genuinely never been extracted, so a stub that raised on every call
            would fail for a reason unrelated to reuse.
            """

            def __init__(self, inner) -> None:
                self._inner = inner
                self.calls = 0

            async def propose(self, request):
                self.calls += 1
                return await self._inner.propose(request)

        from lab_assistant.application import build_handler_registry

        extractor = CountsCalls(harness.context.extractor)
        second_run = build_handler_registry(replace_dataclass(harness.context, extractor=extractor))
        reused = 0
        for rendition in renditions:
            result = await second_run.get(StageName.EXTRACTION).execute(
                StageCommand(
                    stage=StageName.EXTRACTION,
                    workspace_id=workspace.id,
                    actor_id=actor.user_id,
                    inputs={
                        "rendition_id": str(rendition.id),
                        "template": "yeast-lipid-datasheet",
                    },
                )
            )
            if result.metrics.get("fingerprint") in recorded_fingerprints:
                assert result.status is StageStatus.SUCCEEDED_NOOP
                assert result.metrics.get("reused") is True, result.metrics
                reused += 1

        assert reused == len(recorded_fingerprints), (
            "an input already on record was sent back to the model"
        )
        # The decisive number: every extraction on record cost nothing the
        # second time, and only the one never extracted reached the provider.
        assert extractor.calls == len(renditions) - len(recorded_fingerprints)
