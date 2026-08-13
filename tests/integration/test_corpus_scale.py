"""Corpus-scale paths, above the fixed limits that used to cap them.

The audit found hard limits of 500, 1,000, and 5,000 works in acquisition planning,
release drafting, retrieval visibility, and the conflict queue. The predecessor
corpus already exceeded several of them, so the migrated system could have produced
a release that looked complete and silently omitted material.

The corpus here is larger than every limit that was removed. It is metadata-only —
no renditions, no embeddings — so it stays a seconds-long test rather than a
minutes-long one, while still crossing the boundary that mattered.
"""

from __future__ import annotations

from typing import Any

import pytest

from lab_assistant.application.pagination import DEFAULT_PAGE_SIZE, collect_pages, iter_pages
from lab_assistant.application.reconciliation import IdentityService
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.domain.identity import Work, WorkKind
from lab_assistant.domain.states import IdentityState, RelevanceState
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor

#: Above the largest limit the audit found (5,000) and above the predecessor's
#: corpus size, which is the number that made the limits a real defect.
CORPUS_SIZE = 13_200

CONFLICTS = 7


@pytest.fixture(scope="module")
async def large_corpus() -> dict[str, Any]:
    harness = build_harness()
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    context = harness.context
    async with context.uow_factory() as uow:
        for index in range(CORPUS_SIZE):
            await uow.works.add(
                Work(
                    id=context.new_id(),
                    workspace_id=workspace.id,
                    work_kind=WorkKind.ARTICLE,
                    relevance=RelevanceState.INCLUDED,
                    identity_status=(
                        IdentityState.CONFLICT if index < CONFLICTS else IdentityState.RESOLVED
                    ),
                    created_at=context.now(),
                )
            )
        await uow.commit()
    return {
        "harness": harness,
        "workspace": workspace,
        "actor": make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN),
    }


class TestPaginationHelper:
    async def test_it_walks_past_one_page(self, large_corpus) -> None:
        harness, workspace = large_corpus["harness"], large_corpus["workspace"]
        async with harness.context.uow_factory() as uow:
            first_page = await uow.works.list_works(workspace.id, limit=DEFAULT_PAGE_SIZE)
            everything = await collect_pages(
                lambda after, size: uow.works.list_works(
                    workspace.id, limit=size, after_id=after
                )
            )
        assert len(first_page) == DEFAULT_PAGE_SIZE
        assert len(everything) == CORPUS_SIZE
        # Every row exactly once: a keyset cursor must not skip or repeat.
        assert len({work.id for work in everything}) == CORPUS_SIZE

    async def test_a_repository_that_never_advances_fails_instead_of_looping(self) -> None:
        class _Stuck:
            id = "same"

        async def stuck(after, size):
            return [_Stuck()] * size

        with pytest.raises(RuntimeError):
            await collect_pages(stuck, page_size=2)

    async def test_a_short_page_ends_iteration(self, large_corpus) -> None:
        harness, workspace = large_corpus["harness"], large_corpus["workspace"]
        seen = 0
        async with harness.context.uow_factory() as uow:

            async def page(after, size):
                return await uow.works.list_works(workspace.id, limit=size, after_id=after)

            async for _row in iter_pages(page, page_size=5_000):
                seen += 1
        assert seen == CORPUS_SIZE


class TestCorpusScalePaths:
    async def test_retrieval_visibility_covers_the_whole_corpus(self, large_corpus) -> None:
        """The old limit made this an access filter that silently stopped being complete."""
        visible, denied = await RetrievalService(
            large_corpus["harness"].context
        ).visible_work_ids(large_corpus["actor"])
        assert len(visible) + denied == CORPUS_SIZE

    async def test_the_conflict_queue_is_not_truncated(self, large_corpus) -> None:
        conflicts = await IdentityService(large_corpus["harness"].context).list_conflicts(
            large_corpus["actor"]
        )
        # With the old limit of 500 ordered by id, these seven were a coin toss.
        assert len(conflicts) == CONFLICTS

    async def test_release_drafting_includes_every_work_it_can(self, large_corpus) -> None:
        """No renditions here, so the release is empty — but for the right reason.

        The assertion that matters is that drafting *considered* the whole corpus:
        with a fixed limit it would have considered a prefix and reported the same
        empty result, which is indistinguishable from correct.
        """
        harness = large_corpus["harness"]
        async with harness.context.uow_factory() as uow:
            considered = await collect_pages(
                lambda after, size: uow.works.list_works(
                    large_corpus["workspace"].id,
                    relevance=RelevanceState.INCLUDED.value,
                    limit=size,
                    after_id=after,
                )
            )
        assert len(considered) == CORPUS_SIZE

    async def test_the_limit_argument_is_still_a_page_size(self, large_corpus) -> None:
        """A caller asking for one page gets one page: this is not a global change."""
        harness = large_corpus["harness"]
        async with harness.context.uow_factory() as uow:
            page = await uow.works.list_works(large_corpus["workspace"].id, limit=10)
        assert len(page) == 10
