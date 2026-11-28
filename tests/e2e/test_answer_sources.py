"""An answer keeps its sources, and they say enough to find the paper again.

Reopening a conversation used to show no sources at all: only the citations were
kept, and they name neither the paper nor the passage. Now each answer keeps the
sources it was written from, checked again whenever they are shown, and each
names its paper's authors and journal, on a mirror as on the publisher.

Uses the two-installation world of `test_reader_import`.
"""

# ruff: noqa: F811  -- `world` is the fixture shared from test_reader_import.
from __future__ import annotations

from lab_assistant.application.chat import ChatService
from lab_assistant.application.mirror_access import MirrorAccessService
from lab_assistant.domain.observation import Predicate, SourceAssertion
from lab_assistant.fixtures.archive import MEASURED
from tests.e2e.test_reader_import import world  # noqa: F401

QUESTION = "What lipid yield was reported for the engineered strain?"


async def _credit_the_measured_paper(world) -> None:
    """Give the publisher's paper authors and a journal, as a source would."""
    context, ws = world.producer.context, world.producer_ws.id
    work_id = world.fixture.work_ids[MEASURED]
    async with context.uow_factory() as uow:
        title = (await uow.observations.list_assertions(ws, work_id=work_id))[0]

        def asserting(predicate: Predicate, value) -> SourceAssertion:
            return SourceAssertion(
                id=context.new_id(),
                workspace_id=ws,
                source_observation_id=title.source_observation_id,
                subject_hint=title.subject_hint,
                predicate=predicate,
                value_json=value,
                normalised_value_json=value,
                source_priority_class="biomedical_index",
                work_id=work_id,
            )

        await uow.observations.add_assertions(
            [
                asserting(Predicate.AUTHOR, {"last_name": "Pazuki", "fore_name": "Roozbeh"}),
                asserting(Predicate.AUTHOR, {"last_name": "Ledesma-Amaro", "fore_name": "R"}),
                asserting(Predicate.JOURNAL, "Metabolic Engineering"),
            ]
        )
        await uow.commit()


async def _mirror_answer(world):
    await _credit_the_measured_paper(world)
    await world.publish()
    report = await world.importer().import_snapshot(world.reader_ws.id, await world.fetch())
    assert report.ok, report.problems
    chat = ChatService(world.reader.context)
    session = await chat.create_session(world.reader_actor)
    answer = await chat.ask(world.reader_actor, session_id=session.id, question=QUESTION)
    return chat, session, answer


class TestAReopenedAnswer:
    async def test_shows_the_sources_it_was_written_from(self, world) -> None:
        chat, session, answer = await _mirror_answer(world)

        _session, messages = await chat.get_session(world.reader_actor, session_id=session.id)
        described = await chat.describe_answers(world.reader_actor, messages)
        shown = described[answer.message.id]

        assert shown["sources"], "a reopened answer showed no sources"
        assert len(shown["sources"]) == len(answer.message.sources)
        measured = next(s for s in shown["sources"] if s["title"] == MEASURED)
        assert measured["text"], "the passage did not come back with the source"
        # The release by its name, not by an id nobody can read.
        assert shown["release_label"] and shown["release_label"].endswith(" 1.1.0")

    async def test_names_the_papers_authors_and_journal_on_a_mirror(self, world) -> None:
        chat, session, answer = await _mirror_answer(world)
        _session, messages = await chat.get_session(world.reader_actor, session_id=session.id)
        shown = (await chat.describe_answers(world.reader_actor, messages))[answer.message.id]
        measured = next(s for s in shown["sources"] if s["title"] == MEASURED)
        assert measured["authors"] == ["Pazuki R", "Ledesma-Amaro R"]
        assert measured["journal"] == "Metabolic Engineering"

    async def test_from_before_sources_were_kept_shows_its_cited_papers(self, world) -> None:
        chat, _session, answer = await _mirror_answer(world)
        old = answer.message.model_copy(update={"sources": ()})
        shown = (await chat.describe_answers(world.reader_actor, [old]))[old.id]
        assert shown["sources"]
        assert {s["handle"] for s in shown["sources"]} == {
            c.handle for c in answer.message.citations
        }
        assert all(s["from_citation"] and s["text"] == "" for s in shown["sources"])
        assert MEASURED in {s["title"] for s in shown["sources"]}

    async def test_hides_passages_this_mirror_stopped_serving(self, world) -> None:
        chat, session, answer = await _mirror_answer(world)
        access = MirrorAccessService(world.reader.context)
        view = await access.view(world.reader_actor)
        await access.save(
            world.reader_actor,
            withheld_scope_ids=[scope["scope_id"] for scope in view["scopes"]],
            withheld_release_ids=[],
        )
        _session, messages = await chat.get_session(world.reader_actor, session_id=session.id)
        shown = (await chat.describe_answers(world.reader_actor, messages))[answer.message.id]
        measured = next(s for s in shown["sources"] if s["title"] == MEASURED)
        assert measured["withheld"] and measured["text"] == "" and measured["snippet"] == ""


class TestAnOpenedCitation:
    async def test_says_where_the_passage_is_and_which_release(self, world) -> None:
        chat, _session, answer = await _mirror_answer(world)
        opened = await chat.resolve_citation(
            world.reader_actor,
            message_id=answer.message.id,
            handle=answer.message.citations[0].handle,
        )
        assert opened["resolved"], opened
        assert opened["release_label"].endswith(" 1.1.0")
        assert opened["place"]["kind"]
        assert "section_path" in opened["place"]
