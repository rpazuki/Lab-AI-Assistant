"""Every long list pages the same way in every adapter: each row once, in order.

Over memory, SQLite and (with ``LAB_TEST_DATABASE_URL``) Postgres, through the
bulk-review fixture. Rows share timestamps, and one has none, so the walks prove
the id breaks ties and that a row without a time is still reached.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from test_bulk_review import lab  # noqa: F401  (the fixture)
from test_scope_rules import ScopeLab

from lab_assistant.domain.chat import ChatSession
from lab_assistant.domain.claims import Claim, ClaimRevision
from lab_assistant.domain.observation import DiscoveryRun, DiscoveryRunStatus
from lab_assistant.domain.release import CorpusRelease
from lab_assistant.domain.states import ReleaseState, ReviewState

#: Minutes after the start each row is stamped: ties, and one row with no time.
STAMPS: list[int | None] = [0, 5, 5, 5, 2, None, 9, 1]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


async def _walk(fetch, size: int) -> list[UUID]:
    """Follow ``before`` cursors, newest first, until a short page.

    ``fetch`` answers ``(cursor key, id)`` pairs; the last pair's key is the cursor.
    """
    seen: list[UUID] = []
    before: tuple[datetime, UUID] | None = None
    while True:
        page = await fetch(size, before)
        seen.extend(ident for _, ident in page)
        if len(page) < size:
            return seen
        before = page[-1][0]


def _newest_first(stamps: list[datetime | None], ids: list[UUID]) -> list[UUID]:
    epoch = EPOCH
    pairs = sorted(zip(stamps, ids, strict=True), key=lambda p: (p[0] or epoch, str(p[1])))
    return [ident for _, ident in reversed(pairs)]


class TestNewestFirstLists:
    async def test_discovery_runs_releases_and_conversations(self, lab) -> None:  # noqa: F811
        context = lab.harness.context
        start = context.now()
        stamps = [None if m is None else start + timedelta(minutes=m) for m in STAMPS]
        scope = await ScopeLab(lab.harness, lab.actor).scope()
        runs, releases, sessions = [], [], []
        async with context.uow_factory() as uow:
            for at in stamps:
                run = DiscoveryRun(
                    id=context.new_id(),
                    workspace_id=lab.ws,
                    scope_version_id=scope.id,
                    status=DiscoveryRunStatus.FROZEN,
                    run_fingerprint=str(context.new_id()),
                    requested_by=lab.actor.user_id,
                    started_at=at,
                )
                await uow.observations.add_run(run)
                runs.append(run.id)
                release = CorpusRelease(
                    id=context.new_id(),
                    workspace_id=lab.ws,
                    name="Corpus",
                    semantic_version=f"1.0.{len(releases)}",
                    status=ReleaseState.DRAFT,
                    created_at=at or start,
                )
                await uow.releases.add(release)
                releases.append((release.created_at, release.id))
                session = ChatSession(
                    id=context.new_id(),
                    workspace_id=lab.ws,
                    user_id=lab.actor.user_id,
                    created_at=start,
                    updated_at=at,
                )
                await uow.chat.add_session(session)
                sessions.append(session.id)
            await uow.commit()

        def key(row: Any, at: str) -> tuple[datetime, UUID]:
            moment = getattr(row, at) or EPOCH
            return (moment, row.id)

        for size in (1, 3, 50):
            async with context.uow_factory() as uow:

                async def page_runs(n, before):
                    rows = await uow.observations.list_runs(lab.ws, limit=n, before=before)
                    return [(key(r, "started_at"), r.id) for r in rows]

                async def page_releases(n, before):
                    rows = await uow.releases.list_releases(lab.ws, limit=n, before=before)
                    return [(key(r, "created_at"), r.id) for r in rows]

                async def page_sessions(n, before):
                    rows = await uow.chat.list_sessions(
                        lab.ws, lab.actor.user_id, limit=n, before=before
                    )
                    return [(key(r, "updated_at"), r.id) for r in rows]

                got_runs = await _walk(page_runs, size)
                got_releases = await _walk(page_releases, size)
                got_sessions = await _walk(page_sessions, size)
                assert await uow.observations.count_runs(lab.ws) == len(STAMPS)
                assert await uow.releases.count_releases(lab.ws) == len(STAMPS)
                assert await uow.chat.count_sessions(lab.ws, lab.actor.user_id) == len(STAMPS)
            assert got_runs == _newest_first(stamps, runs)
            assert got_releases == _newest_first(
                [at for at, _ in releases], [i for _, i in releases]
            )
            assert got_sessions == _newest_first(stamps, sessions)


class TestTheReviewQueue:
    async def test_only_waiting_values_every_one_once(self, lab) -> None:  # noqa: F811
        context = lab.harness.context
        work = await ScopeLab(lab.harness, lab.actor).screened("Yarrowia lipid factories")
        states = [
            (ReviewState.UNREVIEWED, "valid"),
            (ReviewState.ACCEPTED, "valid"),  # reviewed: not in the queue
            (ReviewState.UNREVIEWED, "invalid"),  # only with include_invalid
            (ReviewState.CORRECTED, "valid"),  # reviewed: not in the queue
        ] * 3
        waiting, invalid = [], []
        async with context.uow_factory() as uow:
            for review, validity in states:
                claim_id, revision_id = context.new_id(), context.new_id()
                await uow.claims.add_claim(
                    Claim(
                        id=claim_id,
                        workspace_id=lab.ws,
                        work_id=work,
                        predicate="lipid_titre",
                        current_revision_id=revision_id,
                        created_at=context.now(),
                    )
                )
                await uow.claims.add_revision(
                    ClaimRevision(
                        id=revision_id,
                        workspace_id=lab.ws,
                        claim_id=claim_id,
                        revision_number=1,
                        value={"verbatim": "1 g/L"},
                        confidence_components={},
                        validation_status=validity,
                        review_status=review,
                        created_at=context.now(),
                    )
                )
                if review is ReviewState.UNREVIEWED:
                    (waiting if validity == "valid" else invalid).append(str(claim_id))
            await uow.commit()

        for include_invalid, expected in ((False, waiting), (True, waiting + invalid)):
            async with context.uow_factory() as uow:
                total = await uow.claims.count_review_queue(lab.ws, include_invalid=include_invalid)
                seen, after = [], None
                while True:
                    page = await uow.claims.list_review_queue(
                        lab.ws, include_invalid=include_invalid, limit=2, after_id=after
                    )
                    seen.extend(str(claim.id) for claim in page)
                    if len(page) < 2:
                        break
                    after = page[-1].id
            assert total == len(expected)
            assert sorted(seen) == sorted(expected) and len(seen) == len(set(seen))
