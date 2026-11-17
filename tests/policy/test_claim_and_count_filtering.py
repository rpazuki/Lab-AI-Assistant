"""Policy before scoring for structured claims, and for the counts.

Two audit findings, one property: a caller must not be able to see, rank against, or
*measure* material they may not open.

* claim search returned the top-k over every claim and filtered afterwards, so a
  restricted claim could occupy the window and push a visible one out;
* the chunk total counted the whole release, so a member could read the volume of
  material hidden from them off an analytics page.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from lab_assistant.adapters.memory.search import MemorySearchIndex

WORKSPACE = uuid4()
RELEASE = uuid4()
VISIBLE_WORK = uuid4()
RESTRICTED_WORK = uuid4()


def _claim(work_id, predicate: str, value: str) -> dict[str, Any]:
    return {
        "revision_id": str(uuid4()),
        "claim_id": str(uuid4()),
        "work_id": str(work_id),
        "predicate": predicate,
        "subject": "YL-1",
        "object_kind": "quantity",
        "value": value,
        "unit": "g/g",
        "review_status": "accepted",
        "abstention_reason": "reported",
        "evidence_span_ids": [str(uuid4())],
    }


def _chunk(work_id, key: str) -> dict[str, Any]:
    return {
        "chunk_key": key,
        "work_id": str(work_id),
        "rendition_id": str(uuid4()),
        "node_ids": ["p-1"],
        "content": f"content for {key}",
        "content_sha256": key,
        "chunk_type": "paragraph",
        "section_label": "Results",
        "page": None,
        "token_count": 8,
        "chunker_id": "structural-v1",
        "chunker_version": "1",
        "metadata": {},
    }


@pytest.fixture
async def index() -> MemorySearchIndex:
    search = MemorySearchIndex()
    # Nine restricted claims first, so a naive top-k of five sees only those.
    restricted = [_claim(RESTRICTED_WORK, "lipid_yield", f"0.{n}") for n in range(9)]
    visible = [_claim(VISIBLE_WORK, "lipid_yield", "0.21")]
    await search.write_claim_projection(WORKSPACE, RELEASE, [*restricted, *visible])
    await search.write_chunks(
        WORKSPACE,
        RELEASE,
        [
            *[_chunk(RESTRICTED_WORK, f"restricted-{n}") for n in range(9)],
            _chunk(VISIBLE_WORK, "visible-1"),
        ],
    )
    return search


class TestClaimSearchFiltersBeforeRanking:
    async def test_a_restricted_claim_cannot_fill_the_window(self, index) -> None:
        rows = await index.claim_search(WORKSPACE, RELEASE, top_k=5, work_ids=[VISIBLE_WORK])
        assert [row["work_id"] for row in rows] == [str(VISIBLE_WORK)]

    async def test_without_the_filter_the_visible_claim_is_pushed_out(self, index) -> None:
        """The behaviour the fix removes, kept as the reason the fix exists."""
        unfiltered = await index.claim_search(WORKSPACE, RELEASE, top_k=5)
        assert str(VISIBLE_WORK) not in {row["work_id"] for row in unfiltered}

    async def test_an_empty_allow_list_returns_nothing(self, index) -> None:
        assert await index.claim_search(WORKSPACE, RELEASE, top_k=5, work_ids=[]) == []

    async def test_no_filter_still_means_no_filter_for_stewardship_paths(self, index) -> None:
        rows = await index.claim_search(WORKSPACE, RELEASE, top_k=50, work_ids=None)
        assert len(rows) == 10


class TestChunkCountsFollowTheFilter:
    async def test_the_count_is_scoped_to_visible_works(self, index) -> None:
        assert await index.count_chunks(WORKSPACE, RELEASE, work_ids=[VISIBLE_WORK]) == 1
        assert await index.count_chunks(WORKSPACE, RELEASE) == 10

    async def test_an_empty_allow_list_counts_nothing(self, index) -> None:
        assert await index.count_chunks(WORKSPACE, RELEASE, work_ids=[]) == 0

    async def test_the_visible_count_does_not_reveal_hidden_volume(self, index) -> None:
        """Nine hidden chunks must not be inferable from the one visible chunk."""
        visible = await index.count_chunks(WORKSPACE, RELEASE, work_ids=[VISIBLE_WORK])
        everything = await index.count_chunks(WORKSPACE, RELEASE)
        assert visible < everything
        assert visible == 1
