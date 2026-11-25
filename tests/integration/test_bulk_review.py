"""Bulk review: merge by rule, include and exclude by filter, and the duplicate queue.

Every test runs over the in-memory adapter and over SQLite, because each filter
is written twice (``CandidateFilter.matches`` and the SQL adapter's query) and
the two must agree; and over SQL the one-active-work-per-identifier index is real,
so a merge that left an identifier behind would fail here.
"""

from __future__ import annotations

import itertools
import os
from typing import Any
from uuid import UUID

import pytest

from lab_assistant.adapters.db.engine import create_schema, make_engine
from lab_assistant.adapters.db.repositories import sql_uow_factory
from lab_assistant.application import bulk_review
from lab_assistant.application.bulk_review import BulkReviewService
from lab_assistant.config import LabEnv, Settings
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import StateTransitionError
from lab_assistant.domain.identity import Work, WorkIdentifier, WorkKind
from lab_assistant.domain.review import (
    CandidateFilter,
    DuplicateCandidate,
    DuplicateStatus,
    MergeRule,
    WorkSummary,
)
from lab_assistant.domain.states import IdentityState, RelevanceState
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor, reset_database

BACKENDS = ["memory", "sqlite"] + (["postgres"] if os.environ.get("LAB_TEST_DATABASE_URL") else [])


@pytest.fixture(params=BACKENDS)
async def lab(request, tmp_path) -> Any:
    engine = None
    if request.param == "memory":
        harness = build_harness()
    else:
        settings = Settings(
            env=LabEnv.LOCAL,
            database_url=(
                os.environ["LAB_TEST_DATABASE_URL"]
                if request.param == "postgres"
                else "sqlite+aiosqlite:///:memory:"
            ),
            object_store_root=tmp_path / "objects",
        )
        engine = make_engine(settings)
        await create_schema(engine, dimensions=(64,))
        await reset_database(engine)
        harness = build_harness(uow_factory=sql_uow_factory(engine))
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    actor = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    try:
        yield Lab(harness, actor)
    finally:
        if engine is not None:
            await engine.dispose()


class Lab:
    """A workspace, and a way to add papers to it as reconciliation would leave them."""

    def __init__(self, harness: Any, actor: Any) -> None:
        self.harness = harness
        self.actor = actor
        self.ws = actor.workspace_id
        self.service = BulkReviewService(harness.context)
        self._n = 0

    async def paper(
        self,
        title: str,
        *,
        year: int | None = 2024,
        kind: WorkKind = WorkKind.ARTICLE,
        doi: str | None = None,
        pmid: str | None = None,
        author: str | None = None,
        subjects: tuple[str, ...] = (),
        sources: tuple[str, ...] = ("pubmed",),
        abstract: bool = False,
        journal: str | None = "Metabolic Engineering",
        relevance: RelevanceState = RelevanceState.UNREVIEWED,
        language: str | None = None,
        text: str | None = None,
    ) -> UUID:
        context = self.harness.context
        self._n += 1
        async with context.uow_factory() as uow:
            work = Work(
                id=context.new_id(),
                workspace_id=self.ws,
                work_kind=kind,
                identity_status=IdentityState.RESOLVED,
                relevance=relevance,
                created_at=context.now(),
            )
            await uow.works.add(work)
            for scheme, value in ((IdentifierScheme.DOI, doi), (IdentifierScheme.PMID, pmid)):
                if value:
                    await uow.works.add_identifier(
                        WorkIdentifier(
                            id=context.new_id(),
                            workspace_id=self.ws,
                            work_id=work.id,
                            scheme=scheme,
                            value_normalized=value,
                            value_display=value,
                            confidence=1.0,
                        )
                    )
            terms = "".join(f"{t.casefold()}\n" for t in subjects)
            await uow.works.upsert_summaries(
                [
                    WorkSummary(
                        work_id=work.id,
                        workspace_id=self.ws,
                        title=title,
                        title_search=title.casefold(),
                        year=year,
                        journal=journal,
                        resource_kind=kind.value,
                        first_author=author,
                        has_abstract=abstract,
                        subject_terms=f"\n{terms}" if terms else "",
                        sources=f" {' '.join(sorted(sources))} ",
                        source_count=len(sources),
                        language=language,
                        text_search=(text or title).casefold(),
                    )
                ]
            )
            await uow.commit()
        return work.id

    async def pair(
        self,
        left: UUID,
        right: UUID,
        *,
        similarity: float = 1.0,
        year_gap: int | None = 0,
        authors: bool | None = None,
    ) -> UUID:
        context = self.harness.context
        row = DuplicateCandidate(
            id=context.new_id(),
            workspace_id=self.ws,
            left_work_id=left,
            right_work_id=right,
            similarity=similarity,
            year_gap=year_gap,
            first_author_match=authors,
            created_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.works.add_duplicate_candidates([row])
            await uow.commit()
        return row.id

    async def work(self, work_id: UUID) -> Work:
        async with self.harness.context.uow_factory() as uow:
            work = await uow.works.get(self.ws, work_id)
        assert work is not None
        return work

    async def open_pairs(self) -> list[DuplicateCandidate]:
        rows, _, _ = await self.service.duplicate_queue(
            self.actor, min_similarity=None, limit=500, after=None
        )
        return rows

    async def titles(self, rule: CandidateFilter) -> list[str]:
        works, total = await self.service.candidates(self.actor, rule, limit=500, after_id=None)
        async with self.harness.context.uow_factory() as uow:
            summaries = await uow.works.get_summaries(self.ws, [w.id for w in works])
        assert total == len(works)
        return sorted(summaries[w.id].title or "" for w in works)


class TestFilters:
    async def test_each_condition_narrows_the_same_way_on_every_backend(self, lab) -> None:
        await lab.paper("Lipid production in Yarrowia lipolytica", subjects=("Yeasts",))
        await lab.paper("Lipids of oleaginous yeast", year=2015, abstract=True)
        await lab.paper("A dataset of yeast lipids", kind=WorkKind.DATASET, sources=("datacite",))
        await lab.paper("Corn ethanol plant economics", journal="Bioresource Technology")

        assert await lab.titles(CandidateFilter(title_words=["lipid"])) == [
            "A dataset of yeast lipids",
            "Lipid production in Yarrowia lipolytica",
            "Lipids of oleaginous yeast",
        ]
        assert await lab.titles(CandidateFilter(title_words=["yeast", "lipid"])) == [
            "A dataset of yeast lipids",
            "Lipids of oleaginous yeast",
        ]
        assert await lab.titles(CandidateFilter(exclude_words=["lipid"])) == [
            "Corn ethanol plant economics"
        ]
        assert await lab.titles(CandidateFilter(subject_terms=["yeast"])) == [
            "Lipid production in Yarrowia lipolytica"
        ]
        assert await lab.titles(CandidateFilter(year_to=2020)) == ["Lipids of oleaginous yeast"]
        assert await lab.titles(CandidateFilter(resource_kinds=["dataset"])) == [
            "A dataset of yeast lipids"
        ]
        assert await lab.titles(CandidateFilter(source="datacite")) == ["A dataset of yeast lipids"]
        assert await lab.titles(CandidateFilter(journal="bioresource")) == [
            "Corn ethanol plant economics"
        ]
        assert await lab.titles(CandidateFilter(has_abstract=True)) == [
            "Lipids of oleaginous yeast"
        ]
        # A % or _ in the search is a character, not a wildcard.
        assert await lab.titles(CandidateFilter(journal="%")) == []

    async def test_by_language_and_by_words_in_the_abstract(self, lab) -> None:
        await lab.paper("Lipids of oleaginous yeast", language="en")
        await lab.paper("Producción de lípidos", language="es")
        await lab.paper("Yarrowia CRISPR", text="yarrowia crispr editing of a bioreactor strain")
        assert await lab.titles(CandidateFilter(languages=["es"])) == ["Producción de lípidos"]
        # Three-letter codes and names mean the same language.
        assert await lab.titles(CandidateFilter(languages=["spa"])) == ["Producción de lípidos"]
        # Not English, the unknown one included or not.
        assert sorted(await lab.titles(CandidateFilter(exclude_languages=["en"]))) == [
            "Producción de lípidos",
            "Yarrowia CRISPR",
        ]
        assert await lab.titles(CandidateFilter(exclude_languages=["en", "unknown"])) == [
            "Producción de lípidos"
        ]
        assert await lab.titles(CandidateFilter(languages=["unknown"])) == ["Yarrowia CRISPR"]
        # A word only the abstract has is found when the abstract is searched.
        assert await lab.titles(CandidateFilter(title_words=["bioreactor"])) == []
        assert await lab.titles(
            CandidateFilter(title_words=["bioreactor"], search_abstract=True)
        ) == ["Yarrowia CRISPR"]
        assert "Yarrowia CRISPR" not in await lab.titles(
            CandidateFilter(exclude_words=["bioreactor"], search_abstract=True)
        )
        facets = await lab.service.facets(lab.actor)
        assert facets["languages"] == {"en": 1, "es": 1, "unknown": 1}

    async def test_facets_count_kinds_and_sources(self, lab) -> None:
        await lab.paper("One", sources=("pubmed", "crossref"))
        await lab.paper("Two", kind=WorkKind.DATASET, sources=("datacite",), year=2001)
        facets = await lab.service.facets(lab.actor)
        assert facets["kinds"] == {"article": 1, "dataset": 1}
        assert facets["sources"] == {"pubmed": 1, "crossref": 1, "datacite": 1}
        assert (facets["year_min"], facets["year_max"]) == (2001, 2024)
        assert facets["summarised"] == facets["works"] == 2


class TestBulkRelevance:
    async def test_preview_changes_nothing_and_apply_counts_down(self, lab, monkeypatch) -> None:
        monkeypatch.setattr(bulk_review, "RELEVANCE_CHUNK", 2)
        ids = [await lab.paper(f"Yeast paper {n}") for n in range(5)]
        decided = await lab.paper("Yeast paper kept", relevance=RelevanceState.INCLUDED)
        rule = CandidateFilter(title_words=["yeast"])

        preview = await lab.service.bulk_relevance(
            lab.actor, rule, relevance=RelevanceState.EXCLUDED
        )
        assert preview["matched"] == 5 and preview["applied"] == 0
        assert len(preview["sample"]) == 5
        assert (await lab.work(ids[0])).relevance is RelevanceState.UNREVIEWED

        remaining, rounds = preview["matched"], 0
        while remaining:
            step = await lab.service.bulk_relevance(
                lab.actor, rule, relevance=RelevanceState.EXCLUDED, apply=True
            )
            remaining, rounds = step["remaining"], rounds + 1
            assert rounds < 10
        assert rounds == 3
        for work_id in ids:
            assert (await lab.work(work_id)).relevance is RelevanceState.EXCLUDED
        # A decision someone already made is not undone by a broad rule.
        assert (await lab.work(decided)).relevance is RelevanceState.INCLUDED

    async def test_overriding_decisions_still_finishes(self, lab, monkeypatch) -> None:
        monkeypatch.setattr(bulk_review, "RELEVANCE_CHUNK", 2)
        for n in range(3):
            await lab.paper(f"Paper {n}", relevance=RelevanceState.INCLUDED)
        rule = CandidateFilter()
        first = await lab.service.bulk_relevance(
            lab.actor, rule, relevance=RelevanceState.EXCLUDED, only_unreviewed=False, apply=True
        )
        assert (first["applied"], first["remaining"]) == (2, 1)
        second = await lab.service.bulk_relevance(
            lab.actor, rule, relevance=RelevanceState.EXCLUDED, only_unreviewed=False, apply=True
        )
        assert (second["applied"], second["remaining"]) == (1, 0)

    async def test_each_decision_records_the_rule(self, lab) -> None:
        work_id = await lab.paper("Yeast lipids")
        await lab.service.bulk_relevance(
            lab.actor,
            CandidateFilter(title_words=["yeast"], year_from=2020),
            relevance=RelevanceState.INCLUDED,
            apply=True,
        )
        async with lab.harness.context.uow_factory() as uow:
            decisions = await uow.works.list_decisions(lab.ws, work_id=work_id)
            events = await uow.audit.list_events(lab.ws, action="relevance_bulk_decided")
        rule = decisions[-1].after["rule"]
        assert rule["title_words"] == ["yeast"] and rule["year_from"] == 2020
        assert rule["only_unreviewed"] is True
        assert decisions[-1].decided_by == lab.actor.user_id
        assert len(events) == 1 and events[0].metadata["changed"] == 1


class TestMergeRule:
    async def test_each_guard_skips_its_pairs_and_says_why(self, lab) -> None:
        a, b = await lab.paper("Same", doi="10.1/a"), await lab.paper("Same", pmid="1")
        await lab.pair(a, b)  # merges
        c, d = await lab.paper("Low"), await lab.paper("Low")
        await lab.pair(c, d, similarity=0.9)
        e, f = await lab.paper("Year"), await lab.paper("Year", year=2020)
        await lab.pair(e, f, year_gap=4)
        g, h = await lab.paper("No year"), await lab.paper("No year", year=None)
        await lab.pair(g, h, year_gap=None)
        i, j = await lab.paper("Authors"), await lab.paper("Authors")
        await lab.pair(i, j, authors=False)
        k, m = await lab.paper("Kind"), await lab.paper("Kind", kind=WorkKind.DATASET)
        await lab.pair(k, m)
        n, o = await lab.paper("Doi", doi="10.1/n"), await lab.paper("Doi", doi="10.1/o")
        await lab.pair(n, o)
        hub = await lab.paper("Editorial")
        for _ in range(4):
            await lab.pair(hub, await lab.paper("Editorial"))

        plan = await lab.service.plan_merge(lab.actor, MergeRule())
        assert plan.groups == [sorted([a, b], key=str)]
        assert plan.skipped == {
            "below_threshold": 1,
            "different_year": 1,
            "unknown_year": 1,
            "different_authors": 1,
            "different_kind": 1,
            "different_doi": 1,
            "generic_title": 4,
        }
        # Loosening a guard is the steward's choice, and the plan follows it.
        loose = await lab.service.plan_merge(
            lab.actor, MergeRule(min_similarity=0.9, require_same_year=False)
        )
        assert len(loose.groups) == 4  # a-b, c-d, e-f, g-h

    async def test_groups_larger_than_the_rule_allows_are_left(self, lab) -> None:
        works = [await lab.paper("Chain") for _ in range(4)]
        for left, right in itertools.pairwise(works):
            await lab.pair(left, right)
        plan = await lab.service.plan_merge(lab.actor, MergeRule(max_group_size=3))
        assert plan.groups == [] and plan.skipped == {"group_too_large": 3}
        plan = await lab.service.plan_merge(lab.actor, MergeRule(max_group_size=4))
        assert [len(g) for g in plan.groups] == [4] and plan.works_merged == 3

    async def test_apply_merges_moves_identifiers_and_empties_the_queue(self, lab) -> None:
        keep = await lab.paper("Same", doi="10.1/keep", pmid="11")
        gone = await lab.paper("Same", pmid="12")
        other = await lab.paper("Same as well")
        await lab.pair(keep, gone)
        # A pair naming the merged work now names the kept one, and stays open.
        await lab.pair(gone, other, similarity=0.88)

        result = await lab.service.apply_merge(lab.actor, MergeRule())
        assert result.pop("step_id")
        assert result == {"groups_merged": 1, "works_merged": 1, "remaining_groups": 0}
        assert (await lab.work(gone)).merged_into_work_id == keep
        async with lab.harness.context.uow_factory() as uow:
            schemes = sorted(
                i.value_normalized for i in await uow.works.list_identifiers(lab.ws, keep)
            )
            decisions = await uow.works.list_decisions(lab.ws, work_id=keep)
            events = await uow.audit.list_events(lab.ws, action="identities_bulk_merged")
        assert schemes == ["10.1/keep", "11", "12"]
        assert decisions[-1].after["rule"]["min_similarity"] == 1.0
        assert len(events) == 1
        [remaining] = await lab.open_pairs()
        assert {remaining.left_work_id, remaining.right_work_id} == {keep, other}

        again = await lab.service.apply_merge(lab.actor, MergeRule())
        assert again["groups_merged"] == 0


class TestQueue:
    async def test_merging_or_dismissing_a_pair_takes_it_off_the_queue(self, lab) -> None:
        a, b = await lab.paper("Pair one", doi="10.1/x"), await lab.paper("Pair one")
        c, d = await lab.paper("Pair two"), await lab.paper("Pair two")
        merged, dismissed = await lab.pair(a, b), await lab.pair(c, d)
        assert len(await lab.open_pairs()) == 2

        decision = await lab.service.merge_pair(lab.actor, merged)
        assert decision.subject_work_ids == (a, b)  # the one with the DOI is kept
        await lab.service.dismiss(lab.actor, dismissed)
        assert await lab.open_pairs() == []

        with pytest.raises(StateTransitionError, match="already merged"):
            await lab.service.merge_pair(lab.actor, merged)
        with pytest.raises(StateTransitionError, match="already dismissed"):
            await lab.service.dismiss(lab.actor, dismissed)
        async with lab.harness.context.uow_factory() as uow:
            row = await uow.works.get_duplicate_candidate(lab.ws, dismissed)
        assert row is not None and row.status is DuplicateStatus.DISMISSED

    async def test_the_queue_pages_most_similar_first(self, lab) -> None:
        for similarity in (0.86, 1.0, 0.9, 0.95):
            await lab.pair(await lab.paper("x"), await lab.paper("y"), similarity=similarity)
        seen, after = [], None
        while True:
            rows, total, _ = await lab.service.duplicate_queue(
                lab.actor, min_similarity=0.88, limit=2, after=after
            )
            seen.extend(row.similarity for row in rows)
            assert total == 3
            if len(rows) < 2:
                break
            after = (rows[-1].similarity, rows[-1].id)
        assert seen == [1.0, 0.95, 0.9]
