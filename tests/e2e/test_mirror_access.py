"""A mirror serves its publisher's corpus, keeps its own asks, and can serve less.

Everything from scopes to published releases comes from the publisher, through
the snapshot. What stays on each installation is what its own people do there:
chat sessions, messages and feedback travel in no snapshot and are touched by no
import. And a mirror's admin can stop serving some of what was imported, a scope
or a release, for everyone on that mirror.

Uses the two-installation world of `test_reader_import`: a producer and a reader
sharing only an archive directory.
"""

# ruff: noqa: F811  -- `world` is the fixture shared from test_reader_import.
from __future__ import annotations

import pytest

from lab_assistant.application.chat import ChatService
from lab_assistant.application.export import ExportService
from lab_assistant.application.mirror_access import MirrorAccessService
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.contracts.errors import (
    InstallationDenied,
    NotFound,
    PolicyDenied,
    ValidationFailure,
)
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures.archive import MEASURED
from lab_assistant.testing import make_actor
from tests.e2e.test_reader_import import world  # noqa: F401

QUERY = "lipid yield engineered strain"


async def _synced(world):
    await world.publish()
    report = await world.importer().import_snapshot(world.reader_ws.id, await world.fetch())
    assert report.ok, report.problems
    return report


def _researcher(actor):
    """The reader's own member, without the admin role that sees past scope rules."""
    return make_actor(
        user_id=actor.user_id,
        workspace_id=actor.workspace_id,
        role=Role.RESEARCHER,
        grants=actor.grants,
    )


async def _found(world, actor, **kwargs) -> set[str]:
    result = await RetrievalService(world.reader.context).retrieve(actor, query=QUERY, **kwargs)
    return {block.work_title for block in result.evidence}


async def _view(world):
    return await MirrorAccessService(world.reader.context).view(world.reader_actor)


async def _remove(world, *, scopes=(), releases=()):
    return await MirrorAccessService(world.reader.context).save(
        world.reader_actor,
        withheld_scope_ids=list(scopes),
        withheld_release_ids=list(releases),
    )


class TestAsksBelongToTheInstallation:
    async def test_a_mirrors_chats_survive_its_imports_and_never_reach_the_publisher(
        self, world
    ) -> None:
        await _synced(world)
        chat = ChatService(world.reader.context)
        session = await chat.create_session(world.reader_actor)
        await chat.ask(world.reader_actor, session_id=session.id, question=QUERY)

        await _synced(world)  # the publisher publishes again; the mirror imports it

        async with world.reader.context.uow_factory() as uow:
            kept = await uow.chat.list_messages(world.reader_ws.id, session.id)
        assert [message.role.value for message in kept] == ["user", "assistant"]
        async with world.producer.context.uow_factory() as uow:
            assert await uow.chat.get_session(world.producer_ws.id, session.id) is None


class TestScopesTravelWithTheirPapers:
    async def test_the_mirror_learns_which_scopes_found_each_paper(self, world) -> None:
        await _synced(world)
        view = await _view(world)
        assert view["scopes"], "no scope reached the mirror"
        assert all(scope["papers"] > 0 for scope in view["scopes"])
        assert not any(scope["withheld"] for scope in view["scopes"])

    async def test_a_member_who_is_not_an_admin_still_finds_papers(self, world) -> None:
        """The publisher's scope rules do not travel, so they cannot hide anything here."""
        await _synced(world)
        assert MEASURED in await _found(world, _researcher(world.reader_actor))


class TestRemovingAScope:
    async def test_its_papers_are_served_to_nobody_on_the_mirror(self, world) -> None:
        await _synced(world)
        scope_ids = [scope["scope_id"] for scope in (await _view(world))["scopes"]]
        view = await _remove(world, scopes=scope_ids)
        assert view["withheld_papers"] > 0
        assert all(scope["withheld"] for scope in view["scopes"])
        assert MEASURED not in await _found(world, world.reader_actor)  # admins too
        assert MEASURED not in await _found(world, _researcher(world.reader_actor))

    async def test_an_import_does_not_put_it_back_but_the_admin_can(self, world) -> None:
        await _synced(world)
        scope_ids = [scope["scope_id"] for scope in (await _view(world))["scopes"]]
        await _remove(world, scopes=scope_ids)
        await _synced(world)
        assert MEASURED not in await _found(world, world.reader_actor)
        await _remove(world)
        assert MEASURED in await _found(world, world.reader_actor)

    async def test_an_old_answers_passages_are_no_longer_opened(self, world) -> None:
        await _synced(world)
        chat = ChatService(world.reader.context)
        session = await chat.create_session(world.reader_actor)
        answer = await chat.ask(world.reader_actor, session_id=session.id, question=QUERY)
        handle = answer.message.citations[0].handle
        await _remove(world, scopes=[s["scope_id"] for s in (await _view(world))["scopes"]])
        opened = await chat.resolve_citation(
            world.reader_actor, message_id=answer.message.id, handle=handle
        )
        assert not opened["resolved"]
        assert "removed from this mirror" in opened["reason"]


class TestRemovingARelease:
    async def test_the_newest_one_left_is_served_instead(self, world) -> None:
        await _synced(world)
        view = await _view(world)
        current = next(r for r in view["releases"] if r["publisher_choice"])
        older = next(r for r in view["releases"] if not r["publisher_choice"])
        assert current["serving"]

        view = await _remove(world, releases=[current["release_id"]])
        assert view["serving_release_id"] == older["release_id"]
        result = await RetrievalService(world.reader.context).retrieve(
            world.reader_actor, query=QUERY
        )
        assert str(result.plan.release_id) == older["release_id"]

        # Pinned by id, it is absent, and it cannot be exported either.
        with pytest.raises(NotFound):
            await _found(world, world.reader_actor, release_id=current["release_id"])
        with pytest.raises(NotFound):
            await ExportService(world.reader.context).build(
                world.reader_actor, release_id=current["release_id"]
            )

        # Put back, the publisher's choice is served again: nothing was moved.
        view = await _remove(world)
        assert view["serving_release_id"] == current["release_id"]

    async def test_with_every_release_removed_nothing_is_served(self, world) -> None:
        await _synced(world)
        releases = [r["release_id"] for r in (await _view(world))["releases"]]
        await _remove(world, releases=releases)
        with pytest.raises(NotFound, match="every published release"):
            await _found(world, world.reader_actor)


class TestWhoMayChangeIt:
    async def test_only_a_mirror_keeps_the_list(self, world) -> None:
        with pytest.raises(InstallationDenied):
            await MirrorAccessService(world.producer.context).view(world.producer_actor)

    async def test_only_an_admin_changes_it(self, world) -> None:
        await _synced(world)
        with pytest.raises(PolicyDenied):
            await MirrorAccessService(world.reader.context).save(
                _researcher(world.reader_actor), withheld_scope_ids=[], withheld_release_ids=[]
            )

    async def test_an_unknown_scope_or_release_is_refused(self, world) -> None:
        from uuid import uuid4

        await _synced(world)
        with pytest.raises(ValidationFailure):
            await _remove(world, scopes=[uuid4()])
