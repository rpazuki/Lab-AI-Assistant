"""Datasheets: templates are registered and approved, runs queue extraction, and
the working sheet shows what extraction proposed.

Uses the archive fixture's papers (one reports a lipid yield and a temperature,
one reports nothing) published as a demo corpus, with no template yet: the state
a fresh installation is in.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from lab_assistant.application.datasheets import DatasheetService
from lab_assistant.contracts.errors import NotFound, PolicyDenied, ValidationFailure
from lab_assistant.contracts.policy import Effect, PolicyAction, ResourceType, SubjectType
from lab_assistant.contracts.stage import StageCommand, StageName
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import replicating_rights_policy
from lab_assistant.fixtures.archive import _BODIES, MEASURED, SILENT, _template_payload
from lab_assistant.testing import build_harness, make_actor, publish_demo_corpus

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)


@pytest.fixture
async def sheet() -> dict[str, Any]:
    harness = build_harness(rights_policy=replicating_rights_policy())
    user = await harness.accounts.create_user(email="s@lab.invalid", password="a-long-passphrase")
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
            policy_basis="datasheet_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    corpus = await publish_demo_corpus(
        harness, actor, works=tuple((title, _BODIES[title]) for title in (MEASURED, SILENT))
    )
    return {
        "harness": harness,
        "actor": actor,
        "corpus": corpus,
        "service": DatasheetService(harness.context, harness.orchestrator),
    }


class TestTemplates:
    async def test_a_template_registers_as_a_draft_once(self, sheet) -> None:
        service, actor = sheet["service"], sheet["actor"]
        template, created = await service.register_template(actor, _template_payload())
        assert created is True
        assert (template["key"], template["status"], template["active"]) == (
            "yeast-lipid-datasheet@1.0.0",
            "draft",
            False,
        )
        _, again = await service.register_template(actor, _template_payload())
        assert again is False
        changed = {**_template_payload(), "title": "Renamed"}
        with pytest.raises(ValidationFailure, match="new semantic_version"):
            await service.register_template(actor, changed)

    async def test_the_example_file_registers(self, sheet) -> None:
        from pathlib import Path

        example = Path(__file__).resolve().parents[2] / "configs/examples/templates"
        definition = yaml.safe_load((example / "yeast-lipid-datasheet.yaml").read_text())
        template, created = await sheet["service"].register_template(sheet["actor"], definition)
        assert created and len(template["targets"]) >= 5

    async def test_an_invalid_template_lists_its_problems(self, sheet) -> None:
        with pytest.raises(ValidationFailure) as refused:
            await sheet["service"].register_template(sheet["actor"], {"template_id": "x"})
        assert any("semantic_version" in problem for problem in refused.value.context["problems"])

    async def test_approving_makes_one_version_active(self, sheet) -> None:
        service, actor = sheet["service"], sheet["actor"]
        await service.register_template(actor, _template_payload())
        await service.register_template(actor, {**_template_payload(), "semantic_version": "1.1.0"})
        await service.approve_template(actor, template_id="yeast-lipid-datasheet", version="1.0.0")
        approved = await service.approve_template(
            actor, template_id="yeast-lipid-datasheet", version="1.1.0"
        )
        assert approved["active"] is True
        states = {item["version"]: item["status"] for item in await service.list_templates(actor)}
        assert states == {"1.0.0": "retired", "1.1.0": "approved"}

    async def test_a_researcher_may_not_register(self, sheet) -> None:
        researcher = make_actor(
            user_id=sheet["actor"].user_id,
            workspace_id=sheet["actor"].workspace_id,
            role=Role.RESEARCHER,
        )
        with pytest.raises(PolicyDenied):
            await sheet["service"].register_template(researcher, _template_payload())


class TestRuns:
    async def test_without_an_approved_template_extraction_does_nothing(self, sheet) -> None:
        service, actor = sheet["service"], sheet["actor"]
        await service.register_template(actor, _template_payload())  # a draft only
        with pytest.raises(ValidationFailure, match="no extraction template is approved"):
            await service.start_run(actor, template=None)
        async with sheet["harness"].context.uow_factory() as uow:
            rendition = (await uow.renditions.list_renditions(actor.workspace_id))[0]
        result = (
            await sheet["harness"]
            .handlers.get(StageName.EXTRACTION)
            .execute(
                StageCommand(
                    stage=StageName.EXTRACTION,
                    workspace_id=actor.workspace_id,
                    actor_id=actor.user_id,
                    inputs={"rendition_id": str(rendition.id)},
                )
            )
        )
        # A draft never fills the review queue.
        assert result.status.value == "succeeded_noop"

    async def test_estimate_run_and_the_working_sheet(self, sheet) -> None:
        service, actor, harness = sheet["service"], sheet["actor"], sheet["harness"]
        await service.register_template(actor, _template_payload())
        await service.approve_template(actor, template_id="yeast-lipid-datasheet", version="1.0.0")

        estimate = await service.estimate(actor, template=None)
        assert (estimate.renditions, estimate.to_extract, estimate.already_extracted) == (2, 2, 0)
        assert estimate.sampled == 2
        assert estimate.input_tokens > 0 and estimate.output_tokens == 2 * 2 * 120
        # The fake provider is not priced: a cost would be invented.
        assert estimate.cost_usd is None

        started = await service.start_run(actor, template=None)
        assert started["tasks"] == 2 and started["template"] == "yeast-lipid-datasheet@1.0.0"
        [run] = await service.list_runs(actor)
        assert run["run_id"] == started["run_id"] and run["total"] == 2
        await harness.run_all_tasks()
        [run] = await service.list_runs(actor)
        assert run["finished"] is True and run["states"] == {"succeeded": 2}

        # Two papers by two targets: the measured paper yields a value, and every
        # target the silent paper lacks is an abstention with its reason.
        rows = await service.rows(actor, template=None)
        assert len(rows) == 4
        assert {row["predicate"] for row in rows} == {"lipid_yield", "culture_temperature"}
        measured = str(sheet["corpus"].work_ids[MEASURED])
        assert any(row["value_verbatim"] for row in rows if row["work_id"] == measured)
        silent = str(sheet["corpus"].work_ids[SILENT])
        assert {row["abstention_reason"] for row in rows if row["work_id"] == silent} == {
            "not_reported_after_complete_search"
        }
        assert {row["review_status"] for row in rows} == {"unreviewed"}

        # Nothing left to do now, unless asked to extract again.
        after = await service.estimate(actor, template=None)
        assert after.to_extract == 0
        with pytest.raises(ValidationFailure, match="re-run"):
            await service.start_run(actor, template=None)
        rerun = await service.start_run(actor, template=None, rerun=True)
        assert rerun["tasks"] == 2 and rerun["run_id"] != started["run_id"]

    async def test_a_run_can_be_cancelled(self, sheet) -> None:
        service, actor = sheet["service"], sheet["actor"]
        await service.register_template(actor, _template_payload())
        await service.approve_template(actor, template_id="yeast-lipid-datasheet", version="1.0.0")
        started = await service.start_run(actor, template=None)
        from uuid import UUID

        assert await service.cancel_run(actor, run_id=UUID(started["run_id"])) == 2
        [run] = await service.list_runs(actor)
        assert run["states"] == {"cancelled": 2} and run["finished"] is True

    async def test_an_unknown_template_is_not_found(self, sheet) -> None:
        with pytest.raises(NotFound):
            await sheet["service"].estimate(sheet["actor"], template="nope@1")
