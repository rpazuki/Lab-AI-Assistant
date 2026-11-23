"""A scope's screening rules, re-applied; and the review history every step writes.

Runs over memory, SQLite and (with ``LAB_TEST_DATABASE_URL``) Postgres, like the
bulk-review tests whose fixture it shares: the history is paged by a keyset the
SQL adapter and the memory adapter each implement.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from test_bulk_review import Lab, lab  # noqa: F401  (the fixture)

from lab_assistant.application import screening
from lab_assistant.application.reconciliation import IdentityService
from lab_assistant.application.review_history import ReviewHistoryService
from lab_assistant.application.screening import ScreeningService
from lab_assistant.contracts.errors import StateTransitionError, ValidationFailure
from lab_assistant.domain.identity import IdentityDecisionType
from lab_assistant.domain.review import ReviewStepKind, WorkSummary
from lab_assistant.domain.scope import ApprovalState, RelevancePolicy, ScopeVersion
from lab_assistant.domain.screening import score_relevance
from lab_assistant.domain.states import RelevanceState


def score(text: str, *, abstract: bool = True, **rules: Any) -> tuple[RelevanceState, str]:
    options: dict[str, Any] = {
        "concepts": ("yarrowia | y. lipolytica", "lipid"),
        "exclusion_terms": (),
        "include_threshold": 0.75,
        "exclude_threshold": 0.25,
        "allow_included_uncertain": False,
        "has_abstract": abstract,
    }
    options.update(rules)
    return score_relevance(text.casefold(), **options)


class TestTheRules:
    def test_a_concept_is_present_when_any_alternative_is(self) -> None:
        assert score("Lipid flux in Y. lipolytica") == (
            RelevanceState.INCLUDED,
            "concept_score:1.00",
        )

    def test_an_exclusion_term_excludes_whatever_the_score(self) -> None:
        assert score("Yarrowia lipid in a plant host", exclusion_terms=("plant",)) == (
            RelevanceState.EXCLUDED,
            "exclusion_term:plant",
        )

    def test_a_title_that_misses_every_concept_is_no_evidence_without_an_abstract(self) -> None:
        assert score("An editorial", abstract=False)[0] is RelevanceState.UNREVIEWED
        assert score("An editorial", abstract=True)[0] is RelevanceState.EXCLUDED

    def test_between_the_thresholds_is_left_for_review(self) -> None:
        assert score("Yarrowia growth") == (
            RelevanceState.UNREVIEWED,
            "uncertain_concept_score:0.50",
        )
        assert score("Yarrowia growth", allow_included_uncertain=True)[0] is (
            RelevanceState.INCLUDED_UNCERTAIN
        )

    def test_no_concepts_decide_nothing(self) -> None:
        assert score("anything", concepts=())[0] is RelevanceState.UNREVIEWED


class TestTheFingerprint:
    """Purpose and intended uses are part of a version, without moving old checksums."""

    @staticmethod
    def version(**fields: Any) -> ScopeVersion:
        base: dict[str, Any] = {
            "id": UUID(int=1),
            "workspace_id": UUID(int=2),
            "scope_id": UUID(int=3),
            "version": 1,
            "name": "Yarrowia",
            "scientific_objective": "Papers that study Y. lipolytica.",
            "intended_uses": ("literature search",),
        }
        return ScopeVersion(**{**base, **fields})

    def test_a_new_version_changes_with_its_purpose_and_uses(self) -> None:
        checksum = self.version().config_checksum
        assert self.version(scientific_objective="Something else.").config_checksum != checksum
        assert self.version(intended_uses=("answers",)).config_checksum != checksum

    def test_a_new_version_changes_with_its_exclusion_terms(self) -> None:
        checksum = self.version().config_checksum
        assert self.version(exclusion_terms=("plant",)).config_checksum != checksum


class ScopeLab(Lab):
    async def scope(
        self,
        *,
        concepts: tuple[str, ...] = ("yarrowia", "lipid"),
        exclusion_terms: tuple[str, ...] = (),
        approved: bool = True,
        version: int = 1,
        scope_id: UUID | None = None,
    ) -> ScopeVersion:
        context = self.harness.context
        scope = ScopeVersion(
            id=context.new_id(),
            workspace_id=self.ws,
            scope_id=scope_id or context.new_id(),
            version=version,
            name="Yarrowia",
            scientific_objective="Y. lipolytica for bioproduction",
            concepts=concepts,
            exclusion_terms=exclusion_terms,
            relevance_policy=RelevancePolicy(),
            approval_state=ApprovalState.APPROVED if approved else ApprovalState.DRAFT,
            created_at=context.now(),
            frozen_at=context.now() if approved else None,
        )
        async with context.uow_factory() as uow:
            await uow.configs.add_scope_version(scope)
            await uow.commit()
        return scope

    async def screened(self, title: str, *, abstract: str | None = None, **fields: Any) -> UUID:
        """A paper whose summary carries the text the rules read."""
        work_id = await self.paper(title, abstract=abstract is not None, **fields)
        async with self.harness.context.uow_factory() as uow:
            summary = (await uow.works.get_summaries(self.ws, [work_id]))[work_id]
            text = " ".join(part for part in (title, abstract) if part).casefold()
            await uow.works.upsert_summaries(
                [WorkSummary.model_validate({**summary.model_dump(), "relevance_text": text})]
            )
            await uow.commit()
        return work_id

    async def relevance(self, work_id: UUID) -> RelevanceState:
        async with self.harness.context.uow_factory() as uow:
            work = await uow.works.get(self.ws, work_id)
        assert work is not None
        return work.relevance

    async def history(self, **options: Any) -> list[Any]:
        steps, _, _, _ = await ReviewHistoryService(self.harness.context).page(
            self.actor,
            scope_version_id=options.get("scope_version_id"),
            kinds=options.get("kinds"),
            limit=options.get("limit", 50),
            before=options.get("before"),
        )
        return steps


@pytest.fixture
def scoped(lab) -> ScopeLab:  # noqa: F811
    scoped = ScopeLab(lab.harness, lab.actor)
    return scoped


class TestApplyingTheRules:
    async def test_the_preview_changes_nothing_and_says_why(self, scoped: ScopeLab) -> None:
        scope = await scoped.scope(exclusion_terms=("plant",))
        both = await scoped.screened("Yarrowia lipid factories")
        plant = await scoped.screened("Yarrowia lipid in a plant")
        title_only = await scoped.screened("A short note")
        read = await scoped.screened("A short note", abstract="About bacteria only")

        plan = await ScreeningService(scoped.harness.context).plan(scoped.actor, scope.id)
        view = plan.view()
        assert view["changes"] == 3
        assert view["transitions"] == {"unreviewed>included": 1, "unreviewed>excluded": 2}
        reasons = {(r["relevance"], r["reason"]): r["count"] for r in view["reasons"]}
        assert reasons == {
            ("included", "concept_score"): 1,
            ("excluded", "exclusion_term:plant"): 1,
            ("excluded", "concept_score"): 1,
            ("unreviewed", "no_abstract_concept_score"): 1,
        }
        assert view["samples"]["included"][0]["title"] == "Yarrowia lipid factories"
        for work_id in (both, plant, title_only, read):
            assert await scoped.relevance(work_id) is RelevanceState.UNREVIEWED

    async def test_apply_in_chunks_is_one_step_and_names_the_version(
        self, scoped: ScopeLab, monkeypatch
    ) -> None:
        monkeypatch.setattr(screening, "RELEVANCE_CHUNK", 2)
        scope = await scoped.scope()
        papers = [await scoped.screened(f"Yarrowia lipid {n}") for n in range(5)]
        service = ScreeningService(scoped.harness.context)

        first = await service.apply(scoped.actor, scope.id)
        assert (first["applied"], first["remaining"]) == (2, 3)
        step_id = UUID(first["step_id"])
        rounds = 1
        while (result := await service.apply(scoped.actor, scope.id, step_id=step_id))["remaining"]:
            rounds += 1
        assert rounds == 2
        assert [await scoped.relevance(p) for p in papers] == [RelevanceState.INCLUDED] * 5
        assert result["step_id"] == str(step_id)

        steps = await scoped.history(kinds=[ReviewStepKind.SCOPE_RULES_APPLIED.value])
        assert len(steps) == 1
        assert steps[0].counts == {"included": 5}
        assert steps[0].scope_version_id == scope.id
        assert steps[0].rule["concepts"] == ["yarrowia", "lipid"]
        async with scoped.harness.context.uow_factory() as uow:
            decisions = await uow.works.list_decisions(scoped.ws, work_id=papers[0])
        decision = decisions[-1]
        assert decision.decision_type is IdentityDecisionType.SET_RELEVANCE
        assert decision.decided_by_type == "scope_rule"
        assert decision.after["scope_version_id"] == str(scope.id)
        assert decision.reason_code == "concept_score:1.00"

    async def test_a_persons_decision_and_a_merged_record_are_left_alone(
        self, scoped: ScopeLab
    ) -> None:
        scope = await scoped.scope()
        decided = await scoped.screened("Yarrowia lipid, decided by hand")
        await IdentityService(scoped.harness.context).set_relevance(
            scoped.actor,
            work_id=decided,
            relevance=RelevanceState.EXCLUDED,
            reason_code="reviewer_out_of_scope",
        )
        open_one = await scoped.screened("Yarrowia lipid, never looked at")
        plan = await ScreeningService(scoped.harness.context).plan(scoped.actor, scope.id)
        assert plan.decided_by_person == 1
        assert [work.id for work, _, _ in plan.changes] == [open_one]

        await ScreeningService(scoped.harness.context).apply(scoped.actor, scope.id)
        assert await scoped.relevance(decided) is RelevanceState.EXCLUDED

    async def test_a_new_version_redecides_what_an_earlier_rule_decided(
        self, scoped: ScopeLab
    ) -> None:
        service = ScreeningService(scoped.harness.context)
        first = await scoped.scope(concepts=("yarrowia",))
        paper = await scoped.screened("Yarrowia in a plant host")
        await service.apply(scoped.actor, first.id)
        assert await scoped.relevance(paper) is RelevanceState.INCLUDED

        second = await scoped.scope(
            concepts=("yarrowia",),
            exclusion_terms=("plant",),
            version=2,
            scope_id=first.scope_id,
        )
        assert (await service.apply(scoped.actor, second.id))["applied"] == 1
        assert await scoped.relevance(paper) is RelevanceState.EXCLUDED
        # Each version's application is listed under that version.
        assert len(await scoped.history(scope_version_id=first.id)) == 1
        assert len(await scoped.history(scope_version_id=second.id)) == 1

    async def test_only_an_approved_version_with_concepts_decides(self, scoped: ScopeLab) -> None:
        service = ScreeningService(scoped.harness.context)
        draft = await scoped.scope(approved=False)
        with pytest.raises(StateTransitionError):
            await service.plan(scoped.actor, draft.id)
        empty = await scoped.scope(concepts=())
        assert (await service.plan(scoped.actor, empty.id)).view()["no_concepts"] is True
        with pytest.raises(ValidationFailure):
            await service.apply(scoped.actor, empty.id)


class TestHistory:
    async def test_review_decisions_are_steps_under_the_scope_in_force(
        self, scoped: ScopeLab
    ) -> None:
        scope = await scoped.scope()
        left = await scoped.paper("Same title", pmid="1")
        right = await scoped.paper("Same title", pmid="2")
        third = await scoped.paper("Same title", pmid="3")
        pair = await scoped.pair(left, right)
        other = await scoped.pair(left, third, similarity=0.9)
        await scoped.service.merge_pair(scoped.actor, pair)
        await scoped.service.dismiss(scoped.actor, other)

        steps = await scoped.history(scope_version_id=scope.id)
        assert [step.kind for step in steps] == [ReviewStepKind.DISMISSED, ReviewStepKind.MERGED]
        assert set(steps[1].work_ids) == {left, right}
        assert all(step.actor_id == scoped.actor.user_id for step in steps)

    async def test_a_filter_applied_in_chunks_is_one_step(self, scoped: ScopeLab) -> None:
        await scoped.scope()
        for n in range(3):
            await scoped.paper(f"Corn ethanol {n}")
        from lab_assistant.application import bulk_review
        from lab_assistant.domain.review import CandidateFilter

        rule = CandidateFilter(title_words=["ethanol"])
        original = bulk_review.RELEVANCE_CHUNK
        bulk_review.RELEVANCE_CHUNK = 2
        try:
            first = await scoped.service.bulk_relevance(
                scoped.actor, rule, relevance=RelevanceState.EXCLUDED, apply=True
            )
            await scoped.service.bulk_relevance(
                scoped.actor,
                rule,
                relevance=RelevanceState.EXCLUDED,
                apply=True,
                step_id=UUID(first["step_id"]),
            )
        finally:
            bulk_review.RELEVANCE_CHUNK = original
        steps = await scoped.history(kinds=[ReviewStepKind.RELEVANCE_BY_FILTER.value])
        assert len(steps) == 1
        assert steps[0].counts == {"excluded": 3}
        assert steps[0].rule["title_words"] == ["ethanol"]

    async def test_pages_newest_first(self, scoped: ScopeLab) -> None:
        context = scoped.harness.context
        await scoped.scope()
        papers = [await scoped.paper(f"Paper {n}") for n in range(5)]
        for work_id in papers:
            scoped.harness.clock.advance(60)
            await IdentityService(context).set_relevance(
                scoped.actor,
                work_id=work_id,
                relevance=RelevanceState.INCLUDED,
                reason_code="reviewer_in_scope",
            )
        kinds = [ReviewStepKind.RELEVANCE_SET.value]
        first = await scoped.history(kinds=kinds, limit=3)
        rest = await scoped.history(
            kinds=kinds, limit=3, before=(first[-1].started_at, first[-1].id)
        )
        assert [s.work_ids[0] for s in first + rest] == list(reversed(papers))

    async def test_backfill_records_earlier_steps_once(self, scoped: ScopeLab) -> None:
        scope = await scoped.scope()
        left = await scoped.paper("Same", pmid="1")
        right = await scoped.paper("Same", pmid="2")
        await IdentityService(scoped.harness.context).merge(
            scoped.actor,
            keep_work_id=left,
            merge_work_id=right,
            reason_code="reviewer_confirmed_same_work",
        )
        # A database from before history was kept: no steps at all.
        async with scoped.harness.context.uow_factory() as uow:
            for step in await uow.works.list_review_steps(scoped.ws, limit=100):
                await _delete_step(scoped, uow, step.id)
            await uow.commit()
        assert await scoped.history() == []

        history = ReviewHistoryService(scoped.harness.context)
        assert await history.backfill(scoped.ws) == 2
        steps = await scoped.history()
        assert {step.kind for step in steps} == {
            ReviewStepKind.SCOPE_APPROVED,
            ReviewStepKind.MERGED,
        }
        assert all(step.scope_version_id == scope.id for step in steps)
        assert await history.backfill(scoped.ws) == 0


async def _delete_step(scoped: ScopeLab, uow: Any, step_id: UUID) -> None:
    """Remove a step behind the services' back: no service deletes history."""
    db = getattr(uow, "_db", None)
    if db is not None:
        db.review_steps.pop(step_id, None)
        return
    from sqlalchemy import delete

    from lab_assistant.adapters.db import tables as t

    await uow.works._c.execute(delete(t.review_steps).where(t.review_steps.c.id == step_id))
