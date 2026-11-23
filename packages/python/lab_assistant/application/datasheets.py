"""Datasheets: extraction templates, extraction runs, and the working sheet.

The predecessor's datasheet workflow — templates, runs, a cost estimate, re-extract,
cancel, the rows and a CSV — rebuilt on this system's stages:

* **A template is workspace data.** A steward registers a version (the JSON or YAML
  shape of ``configs/examples/templates/*.yaml``); a version is immutable, and the
  same id and version with different content is refused. Approving a version makes
  it the one extraction uses when no template is named. Until a template is
  approved, extraction after parsing does nothing, by design: no domain template is
  compiled into core code.
* **A run is a batch of extraction tasks**, one per extraction-eligible rendition,
  grouped under one run id so the batch can be followed and cancelled. A re-run
  extracts again even where this template already ran; extraction keeps unchanged
  targets' revisions and reviews.
* **The estimate is an estimate.** It reads a sample of renditions, measures the
  evidence the selector would send, and prices it at the configured rates. The
  model may answer more or less; the run records what it actually used.
* **The working sheet** is the current revision of every claim the template
  produced, reviewed or not, in the export's long and wide shapes. It is for the
  people building the datasheet. The citable datasheet is the one in a release's
  export package.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import NotFound, StateTransitionError, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.extraction import TemplateVersionSpec
from lab_assistant.contracts.stage import StageName, TaskRequest
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.scope import ApprovalState, ConfigKind, ConfigVersion
from lab_assistant.domain.tasks import Task
from lab_assistant.domain.workspace import Capability

__all__ = ["DatasheetService", "Estimate", "active_template", "template_json"]

#: Renditions read to estimate a run. Enough for a stable mean, few enough that
#: the estimate answers in a request.
ESTIMATE_SAMPLE = 25
#: Instructions, template and schema sent with every call, in tokens. Measured on
#: the worked example; a richer template costs more, which the estimate understates.
PROMPT_OVERHEAD_TOKENS = 1_500
#: Output per target, in tokens: a value, its unit, a quote, a locator.
OUTPUT_TOKENS_PER_TARGET = 120
CHARS_PER_TOKEN = 4


def template_json(config: ConfigVersion, *, active_id: UUID | None = None) -> dict[str, Any]:
    payload = config.payload
    return {
        "id": str(config.id),
        "template_id": config.logical_id,
        "version": config.semantic_version,
        "key": f"{config.logical_id}@{config.semantic_version}",
        "title": payload.get("title", config.logical_id),
        "scientific_purpose": payload.get("scientific_purpose", ""),
        "cardinality": payload.get("cardinality", "per_experiment"),
        "targets": [
            {
                "target_id": target.get("target_id"),
                "predicate": target.get("predicate"),
                "object_kind": target.get("object_kind"),
                "unit_expectation": target.get("unit_expectation"),
                "required": bool(target.get("required", False)),
                "deterministic_metadata": bool(target.get("deterministic_metadata", False)),
            }
            for target in payload.get("targets", [])
        ],
        "status": config.status.value,
        "active": active_id == config.id,
        "checksum": config.checksum[:16],
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "definition": payload,
    }


def active_template(configs: list[ConfigVersion]) -> ConfigVersion | None:
    """The approved template extraction uses when none is named: the newest approved.

    Drafts are never used by default — an unreviewed template should not fill the
    review queue — and a retired one is retired.
    """
    approved = [config for config in configs if config.status is ApprovalState.APPROVED]
    if not approved:
        return None
    return max(approved, key=lambda config: (config.created_at is not None, config.created_at))


@dataclass(frozen=True, slots=True)
class Estimate:
    template_key: str
    renditions: int
    already_extracted: int
    to_extract: int
    sampled: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    model: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "template": self.template_key,
            "renditions": self.renditions,
            "already_extracted": self.already_extracted,
            "to_extract": self.to_extract,
            "sampled": self.sampled,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "model": self.model,
        }


class DatasheetService:
    def __init__(self, context: ServiceContext, orchestrator: Any | None = None) -> None:
        self._context = context
        self._orchestrator = orchestrator

    # ── templates ───────────────────────────────────────────────────────────
    async def list_templates(self, actor: ActorContext) -> list[dict[str, Any]]:
        actor.require(Capability.RUN_EXTRACTION)
        configs = await self._templates(actor.workspace_id)
        active = active_template(configs)
        return [
            template_json(config, active_id=active.id if active else None)
            for config in sorted(
                configs, key=lambda config: (config.logical_id, config.semantic_version)
            )
        ]

    async def register_template(
        self, actor: ActorContext, definition: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Register a template version as a draft. Identical content again is a no-op."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.RUN_EXTRACTION)
        try:
            spec = TemplateVersionSpec.model_validate(definition)
        except Exception as exc:  # pydantic's error lists every problem at once
            problems = (
                [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
                if hasattr(exc, "errors")
                else [str(exc)]
            )
            raise ValidationFailure(
                "the template is not valid", context={"problems": problems}
            ) from exc
        if not spec.targets:
            raise ValidationFailure(
                "the template is not valid", context={"problems": ["targets: at least one"]}
            )
        ids = [target.target_id for target in spec.targets]
        if len(set(ids)) != len(ids):
            raise ValidationFailure(
                "the template is not valid",
                context={"problems": ["targets: every target_id must be unique"]},
            )
        payload = spec.model_dump(mode="json")
        context = self._context
        async with context.uow_factory() as uow:
            existing = await uow.configs.find_config_version(
                actor.workspace_id,
                kind=ConfigKind.EXTRACTION_TEMPLATE.value,
                logical_id=spec.template_id,
                semantic_version=spec.semantic_version,
            )
            if existing is not None:
                probe = existing.model_copy(update={"payload": payload})
                if probe.checksum != existing.checksum:
                    raise ValidationFailure(
                        f"{spec.logical_key} is already registered with different content; "
                        "register the change as a new semantic_version"
                    )
                configs = await self._templates(actor.workspace_id)
                active = active_template(configs)
                return template_json(existing, active_id=active.id if active else None), False
            config = ConfigVersion(
                id=context.new_id(),
                workspace_id=actor.workspace_id,
                kind=ConfigKind.EXTRACTION_TEMPLATE,
                logical_id=spec.template_id,
                semantic_version=spec.semantic_version,
                payload=payload,
                schema_version=spec.schema_version,
                status=ApprovalState.DRAFT,
                created_at=context.now(),
                created_by=actor.user_id,
            )
            await uow.configs.add_config_version(config)
            await self._audit(uow, actor, config, reason="template_registered")
            await uow.commit()
        return template_json(config), True

    async def approve_template(
        self, actor: ActorContext, *, template_id: str, version: str
    ) -> dict[str, Any]:
        """Make a version the one extraction uses by default.

        Other approved versions of the same template are retired, so "the template"
        has one meaning at a time. Claims already extracted keep the version that
        produced them.
        """
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.RUN_EXTRACTION)
        actor.require(Capability.MANAGE_SCOPE)
        context = self._context
        async with context.uow_factory() as uow:
            configs = await uow.configs.list_config_versions(
                actor.workspace_id, kind=ConfigKind.EXTRACTION_TEMPLATE.value
            )
            target = next(
                (
                    config
                    for config in configs
                    if config.logical_id == template_id and config.semantic_version == version
                ),
                None,
            )
            if target is None:
                raise NotFound(f"extraction template {template_id}@{version} not found")
            for config in configs:
                if (
                    config.logical_id == template_id
                    and config.id != target.id
                    and config.status is ApprovalState.APPROVED
                ):
                    await uow.configs.add_config_version(
                        config.model_copy(update={"status": ApprovalState.RETIRED})
                    )
            approved = target.model_copy(
                update={"status": ApprovalState.APPROVED, "created_at": target.created_at}
            )
            await uow.configs.add_config_version(approved)
            await self._audit(uow, actor, approved, reason="template_approved")
            await uow.commit()
            configs = await uow.configs.list_config_versions(
                actor.workspace_id, kind=ConfigKind.EXTRACTION_TEMPLATE.value
            )
        # Approving an older version over a newer one is allowed, and wins: it is
        # the newest *approval* a steward made that counts, so the approved row of
        # this template is the only approved row of it.
        active = active_template(configs)
        return template_json(approved, active_id=active.id if active else None)

    async def delete_template(self, actor: ActorContext, *, template_id: str, version: str) -> None:
        """Delete a draft template version that nothing has used.

        An approved or retired version is the record of what produced reviewed
        values, so only a draft can go, and only while no extraction task named it
        and no value carries it.
        """
        from lab_assistant.application.pagination import collect_pages

        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.RUN_EXTRACTION)
        actor.require(Capability.MANAGE_SCOPE)
        context = self._context
        key = f"{template_id}@{version}"
        async with context.uow_factory() as uow:
            config = await uow.configs.find_config_version(
                actor.workspace_id,
                kind=ConfigKind.EXTRACTION_TEMPLATE.value,
                logical_id=template_id,
                semantic_version=version,
            )
            if config is None:
                raise NotFound(f"extraction template {key} not found")
            if config.status is not ApprovalState.DRAFT:
                raise StateTransitionError(
                    f"a {config.status.value} template version cannot be deleted; only a draft can",
                    context={"status": config.status.value},
                )
            tasks = await uow.tasks.list_tasks(
                actor.workspace_id, stage=StageName.EXTRACTION.value, limit=100_000
            )
            if any(task.inputs.get("template") in (key, template_id) for task in tasks):
                raise StateTransitionError(
                    "an extraction run used this template version; it cannot be deleted"
                )
            claims = await collect_pages(
                lambda after, size: uow.claims.list_claims(
                    actor.workspace_id, limit=size, after_id=after
                )
            )
            if any(
                claim.template_id == template_id and claim.template_version == version
                for claim in claims
            ):
                raise StateTransitionError(
                    "values were extracted with this template version; it cannot be deleted"
                )
            await uow.configs.delete_config_version(actor.workspace_id, config.id)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.EXTRACTION_TEMPLATE_DELETED,
                    subject_type="extraction_template",
                    subject_id=config.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="draft_deleted",
                    metadata={"template": key, "checksum": config.checksum[:16]},
                )
            )
            await uow.commit()

    # ── runs ────────────────────────────────────────────────────────────────
    async def estimate(self, actor: ActorContext, *, template: str | None) -> Estimate:
        """What a run would extract, and roughly what it would cost."""
        actor.require(Capability.RUN_EXTRACTION)
        spec, key = await self._resolve(actor.workspace_id, template)
        eligible, done = await self._eligible(actor.workspace_id, key)
        pending = [rendition for rendition in eligible if rendition.id not in done]
        from lab_assistant.application.extraction import select_evidence
        from lab_assistant.application.parsing import ParsingService

        parsing = ParsingService(self._context)
        sample = random.Random(key).sample(pending, min(ESTIMATE_SAMPLE, len(pending)))
        characters: list[int] = []
        for rendition in sample:
            try:
                document = await parsing.load_document(actor.workspace_id, rendition)
            except Exception:
                continue
            characters.append(len(select_evidence(document, template=spec).text))
        mean_chars = sum(characters) / len(characters) if characters else 0
        per_call_input = int(mean_chars / CHARS_PER_TOKEN) + PROMPT_OVERHEAD_TOKENS
        per_call_output = OUTPUT_TOKENS_PER_TARGET * len(spec.targets)
        input_tokens = per_call_input * len(pending)
        output_tokens = per_call_output * len(pending)
        settings = self._context.settings
        priced = settings.llm_provider != "fake"
        cost = (
            round(
                input_tokens / 1_000_000 * settings.llm_input_usd_per_mtok
                + output_tokens / 1_000_000 * settings.llm_output_usd_per_mtok,
                2,
            )
            if priced
            else None
        )
        return Estimate(
            template_key=key,
            renditions=len(eligible),
            already_extracted=len(eligible) - len(pending),
            to_extract=len(pending),
            sampled=len(characters),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            model=settings.llm_model if priced else None,
        )

    async def start_run(
        self, actor: ActorContext, *, template: str | None, rerun: bool = False
    ) -> dict[str, Any]:
        """Queue extraction over every eligible rendition the template has not covered.

        With ``rerun`` every eligible rendition is extracted again, the predecessor's
        re-extract: after a model change, or a correction to the parser.
        """
        self._context.require_installation(InstallationCapability.PROCESS_CONTENT)
        actor.require(Capability.RUN_EXTRACTION)
        if self._orchestrator is None:
            raise RuntimeError("DatasheetService needs a task orchestrator to queue a run")
        _, key = await self._resolve(actor.workspace_id, template)
        eligible, done = await self._eligible(actor.workspace_id, key)
        targets = eligible if rerun else [r for r in eligible if r.id not in done]
        if not targets:
            raise ValidationFailure(
                "nothing to extract: every eligible document already has this template's "
                "claims; choose re-run to extract again"
                if eligible
                else "nothing to extract: no document has been parsed to an eligible rendition yet"
            )
        run_id = self._context.new_id()
        requests = tuple(
            TaskRequest(
                stage=StageName.EXTRACTION,
                # The run id makes a re-run a new task: the queue returns an earlier
                # task for identical inputs, which is right for retries and wrong here.
                inputs={"rendition_id": str(r.id), "template": key, "batch": str(run_id)},
            )
            for r in targets
        )
        tasks = await self._orchestrator.submit_requests(
            requests, workspace_id=actor.workspace_id, actor_id=actor.user_id, run_id=run_id
        )
        async with self._context.uow_factory() as uow:
            await uow.audit.append(
                self._context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ADMIN_ACTION,
                    subject_type="extraction_run",
                    subject_id=run_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="extraction_rerun" if rerun else "extraction_run",
                    metadata={"template": key, "tasks": len(tasks)},
                )
            )
            await uow.commit()
        return {"run_id": str(run_id), "template": key, "tasks": len(tasks), "rerun": rerun}

    async def list_runs(
        self, actor: ActorContext, *, limit: int = 50, after: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        """Extraction batches, newest first, with their tasks counted by state.

        A run is its tasks, so every extraction task is read, a page at a time:
        reading a fixed number of the oldest tasks lost every run past them.
        Returns one page of runs, the cursor to the next, and how many there are.
        """
        from lab_assistant.application.pagination import slice_page
        from lab_assistant.domain.states import TaskState

        actor.require(Capability.RUN_EXTRACTION)
        every_state = [state.value for state in TaskState]
        tasks: list[Task] = []
        cursor: tuple[datetime, UUID] | None = None
        async with self._context.uow_factory() as uow:
            while True:
                page = await uow.tasks.page_tasks(
                    actor.workspace_id,
                    states=every_state,
                    stage=StageName.EXTRACTION.value,
                    limit=2_000,
                    after=cursor,
                )
                tasks.extend(page)
                if len(page) < 2_000 or page[-1].created_at is None:
                    break
                cursor = (page[-1].created_at, page[-1].id)
        runs: dict[UUID, dict[str, Any]] = {}
        for task in tasks:
            if task.stage is not StageName.EXTRACTION or task.run_id is None:
                continue
            if "batch" not in task.inputs:
                continue
            run = runs.setdefault(
                task.run_id,
                {
                    "run_id": str(task.run_id),
                    "template": task.inputs.get("template"),
                    "states": Counter(),
                    "created_at": task.created_at,
                    "errors": [],
                },
            )
            run["states"][task.state.value] += 1
            if task.created_at and (
                run["created_at"] is None or task.created_at < run["created_at"]
            ):
                run["created_at"] = task.created_at
            if task.error_detail and len(run["errors"]) < 5:
                run["errors"].append(task.error_detail[:300])

        def key(run: dict[str, Any]) -> str:
            at = run["created_at"]
            return f"{at.isoformat() if at else ''}|{run['run_id']}"

        ordered = sorted(runs.values(), key=key, reverse=True)
        shown, next_cursor = slice_page(ordered, key=key, limit=limit, after=after, descending=True)
        terminal = {"succeeded", "failed", "dead_letter", "cancelled", "quarantined"}
        items = [
            {
                **run,
                "states": dict(run["states"]),
                "total": sum(run["states"].values()),
                "finished": all(state in terminal for state in run["states"]),
                "created_at": run["created_at"].isoformat() if run["created_at"] else None,
            }
            for run in shown
        ]
        return items, next_cursor, len(ordered)

    async def cancel_run(self, actor: ActorContext, *, run_id: UUID) -> int:
        actor.require(Capability.RUN_EXTRACTION)
        if self._orchestrator is None:
            raise RuntimeError("DatasheetService needs a task orchestrator to cancel a run")
        return await self._orchestrator.cancel_run(workspace_id=actor.workspace_id, run_id=run_id)

    async def delete_run(self, actor: ActorContext, *, run_id: UUID) -> int:
        """Delete an extraction run in which no batch succeeded. Returns its task count.

        A run is its tasks, so that is what goes. Only when every one failed or
        was cancelled: a succeeded batch marks its papers as done for its
        template, and deleting it would queue them again. Values a failed batch
        managed to write stay, with their reviews; they are not the run's to take.
        """
        from lab_assistant.domain.states import TaskState

        self._context.require_installation(InstallationCapability.PROCESS_CONTENT)
        actor.require(Capability.RUN_EXTRACTION)
        context = self._context
        failed = {TaskState.FAILED, TaskState.DEAD_LETTER, TaskState.CANCELLED}
        async with context.uow_factory() as uow:
            tasks = await uow.tasks.list_run_tasks(actor.workspace_id, run_id)
            if not any(task.stage is StageName.EXTRACTION for task in tasks):
                raise NotFound(f"extraction run {run_id} not found")
            kept = [task for task in tasks if task.state not in failed]
            if kept:
                raise StateTransitionError(
                    "only a run whose every batch failed or was cancelled can be deleted",
                    context={"states": sorted({task.state.value for task in kept})},
                )
            await uow.tasks.delete_tasks(actor.workspace_id, [task.id for task in tasks])
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.EXTRACTION_RUN_DELETED,
                    subject_type="extraction_run",
                    subject_id=run_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    run_id=run_id,
                    reason_code="run_deleted",
                    metadata={
                        "tasks": len(tasks),
                        "template": next(
                            (t.inputs.get("template") for t in tasks if t.inputs.get("template")),
                            None,
                        ),
                    },
                )
            )
            await uow.commit()
        return len(tasks)

    # ── the working sheet ───────────────────────────────────────────────────
    async def sheet_page(
        self,
        actor: ActorContext,
        *,
        template: str | None,
        words: Sequence[str] = (),
        limit: int = 50,
        after: str | None = None,
    ) -> dict[str, Any]:
        """One page of the working sheet's wide rows, one row per (work, experiment).

        The counts are over the whole sheet. ``words`` keep the rows holding every
        word, case-insensitively, in any column, as a table search does, so a
        search covers every row and not only the page on screen. ``long_form`` is
        the claims of the rows on this page.
        """
        from lab_assistant.application.export import wide_rows
        from lab_assistant.application.pagination import slice_page

        long_form = await self.rows(actor, template=template)
        statuses: dict[str, int] = {}
        for row in long_form:
            statuses[row["review_status"]] = statuses.get(row["review_status"], 0) + 1
        columns, wide = wide_rows(long_form)
        wanted = [word.casefold() for word in words if word.strip()]
        if wanted:
            shown = [c for c in columns if c != "revision_ids"]
            wide = [
                row
                for row in wide
                if all(
                    word in " ".join(str(row.get(c, "")) for c in shown).casefold()
                    for word in wanted
                )
            ]

        def key(row: dict[str, Any]) -> str:
            return f"{row['work_id']}|{row['experiment_id']}"

        page, next_cursor = slice_page(wide, key=key, limit=limit, after=after)
        on_page = {(row["work_id"], row["experiment_id"]) for row in page}
        return {
            "template": long_form[0]["template_version"] if long_form else template,
            "claims": len(long_form),
            "review_status": statuses,
            "long_form": [
                row for row in long_form if (row["work_id"], row["experiment_id"]) in on_page
            ],
            "wide": {
                "columns": columns,
                "rows": page,
                "total": len(wide),
                "next_cursor": next_cursor,
            },
        }

    async def rows(self, actor: ActorContext, *, template: str | None) -> list[dict[str, Any]]:
        """The current revision of every claim the template produced, long form."""
        actor.require(Capability.RUN_EXTRACTION)
        from lab_assistant.application.pagination import collect_pages

        _, key = await self._resolve(actor.workspace_id, template)
        template_id, _, version = key.rpartition("@")
        long_form: list[dict[str, Any]] = []
        async with self._context.uow_factory() as uow:
            claims = await collect_pages(
                lambda after, size: uow.claims.list_claims(
                    actor.workspace_id, limit=size, after_id=after
                )
            )
            for claim in claims:
                if claim.template_id != template_id or claim.template_version != version:
                    continue
                if claim.lifecycle_status == "withdrawn" or claim.current_revision_id is None:
                    continue
                revision = await uow.claims.get_revision(
                    actor.workspace_id, claim.current_revision_id
                )
                if revision is None:
                    continue
                long_form.append(
                    {
                        "work_id": str(claim.work_id),
                        "experiment_id": str(claim.experiment_id) if claim.experiment_id else "",
                        "claim_id": str(claim.id),
                        "revision_id": str(revision.id),
                        "subject": claim.subject_label or "",
                        "predicate": claim.predicate,
                        "value_verbatim": revision.value.get("verbatim") or "",
                        "value_normalised": revision.value.get("normalised") or "",
                        "unit": revision.value.get("unit_canonical")
                        or revision.value.get("unit_verbatim")
                        or "",
                        "basis": revision.value.get("basis") or "",
                        "qualifiers": "",
                        "evidence_span_id": ";".join(str(i) for i in revision.evidence_span_ids),
                        "source_locator": "",
                        "review_status": revision.review_status.value,
                        "abstention_reason": revision.abstention_reason.value,
                        "template_version": key,
                        "validation_status": revision.validation_status,
                    }
                )
        return sorted(
            long_form, key=lambda row: (row["work_id"], row["experiment_id"], row["predicate"])
        )

    # ── helpers ─────────────────────────────────────────────────────────────
    async def _templates(self, workspace_id: UUID) -> list[ConfigVersion]:
        async with self._context.uow_factory() as uow:
            return await uow.configs.list_config_versions(
                workspace_id, kind=ConfigKind.EXTRACTION_TEMPLATE.value
            )

    async def _resolve(
        self, workspace_id: UUID, template: str | None
    ) -> tuple[TemplateVersionSpec, str]:
        configs = await self._templates(workspace_id)
        if template:
            match = next(
                (
                    config
                    for config in configs
                    if f"{config.logical_id}@{config.semantic_version}" == template
                ),
                None,
            )
            if match is None:
                raise NotFound(f"extraction template {template} not found")
        else:
            match = active_template(configs)
            if match is None:
                raise ValidationFailure(
                    "no extraction template is approved; register one and approve it"
                )
        return (
            TemplateVersionSpec.model_validate(match.payload),
            f"{match.logical_id}@{match.semantic_version}",
        )

    async def _eligible(self, workspace_id: UUID, key: str) -> tuple[list[Any], set[UUID]]:
        """Current eligible renditions, and those this template version has covered.

        Covered means a finished extraction task named this template for the
        rendition, or the rendition's work already holds this template's claims.
        Anything this misses costs nothing to queue: extraction reuses a recorded
        result for identical input instead of calling the model again.
        """
        from lab_assistant.application.pagination import collect_pages
        from lab_assistant.domain.states import TaskState

        template_id, _, version = key.rpartition("@")
        async with self._context.uow_factory() as uow:
            renditions = [
                rendition
                for rendition in await uow.renditions.list_renditions(workspace_id)
                if rendition.is_extraction_eligible
            ]
            tasks = await uow.tasks.list_tasks(workspace_id, limit=5000)
            claims = await collect_pages(
                lambda after, size: uow.claims.list_claims(workspace_id, limit=size, after_id=after)
            )
        # A newer rendition of the same asset supersedes an older one.
        superseded = {r.supersedes_rendition_id for r in renditions if r.supersedes_rendition_id}
        current = [r for r in renditions if r.id not in superseded]
        covered: set[UUID] = {
            UUID(str(task.inputs["rendition_id"]))
            for task in tasks
            if task.stage is StageName.EXTRACTION
            and task.state is TaskState.SUCCEEDED
            and task.inputs.get("template") in (key, template_id)
            and task.inputs.get("rendition_id")
        }
        works = {
            claim.work_id
            for claim in claims
            if claim.template_id == template_id and claim.template_version == version
        }
        covered |= {r.id for r in current if r.work_id in works}
        return current, covered

    async def _audit(
        self, uow: Any, actor: ActorContext, config: ConfigVersion, *, reason: str
    ) -> None:
        await uow.audit.append(
            self._context.audit.build(
                workspace_id=actor.workspace_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                action=AuditAction.ADMIN_ACTION,
                subject_type="extraction_template",
                subject_id=config.id,
                previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                reason_code=reason,
                metadata={
                    "template": f"{config.logical_id}@{config.semantic_version}",
                    "checksum": config.checksum[:16],
                },
            )
        )
