"""Abstracts in place of full text, for papers no route could fetch.

Most included papers cannot be fetched automatically: no open licence, a sign-in
page, a publisher that refuses robots. Until someone uploads a copy they are
absent from Ask altogether, though discovery holds their abstracts. With this
fallback switched on, such a paper's abstract stands in for it:

* **the setting** is a steward's recorded choice (a ``rights_policy`` config
  version, ``abstract-fallback``, with who set it and when). Off by default:
  indexing and quoting abstracts of papers whose licence does not allow keeping
  them is a rights decision, which the ``abstract-fallback`` rule of the rights
  policy grants only with that attestation;
* **building** turns each eligible paper's title and abstract into a small text
  document, an asset marked ``version: abstract``, parsed like any other text.
  Eligible: included, an abstract on record, its plan waiting for a person or
  failed, and no file stored for it yet. Its plan becomes **partial**: it is in
  the collection, but only as its abstract, and an upload is still wanted;
* **releases** take a paper's full text when there is one, and its abstract only
  while the fallback is on. Ask marks every source that is an abstract, and the
  answer says so;
* **an upload** later is a full text like any other: the next release takes it
  instead, and the plan is acquired.

Building is chunked like every bulk action: each call builds up to ``CHUNK`` and
says how many remain, and the page repeats it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.application.pagination import collect_pages
from lab_assistant.application.reconciliation import canonical_metadata
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.assets import (
    ABSTRACT_VERSION,
    Asset,
    MalwareScanStatus,
    ValidationStatus,
    is_abstract,
)
from lab_assistant.domain.scope import ApprovalState, ConfigKind, ConfigVersion
from lab_assistant.domain.screening import plain_text
from lab_assistant.domain.workspace import Capability

__all__ = ["CHUNK", "FALLBACK_ID", "AbstractFallbackService", "fallback_enabled"]

FALLBACK_ID = "abstract-fallback"

#: Documents built per call; the page repeats the call until none remain.
CHUNK = 500

#: Plans an abstract may stand in for: no route returned the paper.
WAITING = ("assisted", "failed")

#: What an abstract document may be used for (the abstract-fallback rule).
ACTIONS = frozenset(
    {
        PolicyAction.STORE_CONTENT,
        PolicyAction.PROCESS_LOCAL,
        PolicyAction.CREATE_EMBEDDINGS_LOCAL,
        PolicyAction.INDEX,
        PolicyAction.QUERY,
        PolicyAction.DISPLAY_EXCERPT,
        PolicyAction.RETAIN,
    }
)


async def _setting(uow: Any, workspace_id: UUID) -> ConfigVersion | None:
    versions = [
        config
        for config in await uow.configs.list_config_versions(
            workspace_id, kind=ConfigKind.RIGHTS_POLICY.value
        )
        if config.logical_id == FALLBACK_ID
    ]
    if not versions:
        return None
    return max(versions, key=lambda c: (c.created_at is not None, c.created_at, c.id))


async def fallback_enabled(uow: Any, workspace_id: UUID) -> bool:
    """Whether a steward has switched the abstract fallback on (and not off again)."""
    setting = await _setting(uow, workspace_id)
    return bool(setting and setting.payload.get("enabled"))


def abstract_document(title: str | None, abstract: str) -> bytes:
    """The text an abstract asset holds: its title, then the abstract, as paragraphs."""
    parts = [part for part in (title, abstract) if part]
    return ("\n\n".join(parts) + "\n").encode("utf-8")


class AbstractFallbackService:
    def __init__(self, context: ServiceContext, orchestrator: Any | None = None) -> None:
        self._context = context
        self._orchestrator = orchestrator

    async def view(self, actor: ActorContext) -> dict[str, Any]:
        """The setting, who set it, and how many papers it covers or could."""
        actor.require(Capability.ACQUIRE)
        ws = actor.workspace_id
        async with self._context.uow_factory() as uow:
            setting = await _setting(uow, ws)
            plans = await uow.assets.count_plans_by_status(ws)
        return {
            "enabled": bool(setting and setting.payload.get("enabled")),
            "set_at": setting.created_at.isoformat() if setting and setting.created_at else None,
            "set_by": str(setting.created_by) if setting and setting.created_by else None,
            "partial": plans.get("partial", 0),
            "eligible": len(await self._eligible(ws, limit=None)),
        }

    async def set_enabled(self, actor: ActorContext, enabled: bool) -> dict[str, Any]:
        """Record the choice. Turning it off takes abstracts out of the next release."""
        actor.require(Capability.DECIDE_RIGHTS)
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            previous = await _setting(uow, ws)
            number = int(previous.semantic_version.split(".")[0]) + 1 if previous else 1
            config = ConfigVersion(
                id=context.new_id(),
                workspace_id=ws,
                kind=ConfigKind.RIGHTS_POLICY,
                logical_id=FALLBACK_ID,
                semantic_version=f"{number}.0.0",
                payload={"enabled": enabled},
                status=ApprovalState.APPROVED,
                created_at=context.now(),
                created_by=actor.user_id,
            )
            await uow.configs.add_config_version(config)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ABSTRACT_FALLBACK_CHANGED,
                    subject_type="config_version",
                    subject_id=config.id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="enabled" if enabled else "disabled",
                    metadata={"enabled": enabled},
                )
            )
            await uow.commit()
        return await self.view(actor)

    async def build(self, actor: ActorContext) -> dict[str, int]:
        """Build up to ``CHUNK`` abstract documents; say how many remain."""
        actor.require(Capability.ACQUIRE)
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            setting = await _setting(uow, ws)
        if not (setting and setting.payload.get("enabled")):
            return {"built": 0, "remaining": 0, "enabled": 0}
        attestation = (
            f"abstract fallback switched on by {setting.created_by} "
            f"at {setting.created_at.isoformat() if setting.created_at else 'unknown time'}"
        )
        chosen = await self._eligible(ws, limit=CHUNK + 1)
        batch, more = chosen[:CHUNK], len(chosen) > CHUNK

        from lab_assistant.application.acquisition import AcquisitionService

        rights = AcquisitionService(context)
        built: list[UUID] = []
        for plan_id, work_id in batch:
            async with context.uow_factory() as uow:
                plan = await uow.assets.get_plan(ws, plan_id)
                if plan is None or plan.status not in WAITING:
                    continue
                if await uow.assets.list_assets(ws, work_id=work_id):
                    continue
                metadata = canonical_metadata(
                    list(await uow.observations.list_assertions(ws, work_id=work_id))
                )
            abstract = plain_text(metadata.get("abstract"))
            if not abstract:
                continue
            content = abstract_document(plain_text(metadata.get("title")), abstract)
            asset_id = context.new_id()
            decision = await rights.decide_rights(
                actor,
                subject_type="asset",
                subject_id=asset_id,
                licence_expression=None,
                access_basis="bibliographic_record",
                route=None,
                content_kind="abstract",
                user_attestation=attestation,
                requested_actions=ACTIONS,
                decided_by_type="human_steward",
            )
            if not decision.permits(PolicyAction.PROCESS_LOCAL, now=context.now()):
                continue
            stored = await context.object_store.put(
                content, workspace_id=ws, object_class="asset", media_type="text/plain"
            )
            asset = Asset(
                id=asset_id,
                workspace_id=ws,
                sha256=stored.sha256,
                byte_length=stored.byte_length,
                object_id=stored.id,
                media_type_detected="text/plain",
                original_filename_safe="abstract.txt",
                # Text this system wrote from the paper's own record: there is
                # nothing to scan, and the exception is recorded, not absent.
                malware_scan_status=MalwareScanStatus.SKIPPED_WITH_EXCEPTION,
                validation_status=ValidationStatus.PASSED,
                manifestation_id=plan.manifestation_ids[0] if plan.manifestation_ids else None,
                work_id=work_id,
                rights_decision_id=decision.id,
                # Built from the paper's own record, so it is that paper's.
                identity_verified=True,
                created_at=context.now(),
                metadata={
                    "version": ABSTRACT_VERSION,
                    "route": "discovery_abstract",
                    "identity_basis": "record",
                    "plan_id": str(plan_id),
                },
            )
            async with context.uow_factory() as uow:
                await register_object(uow, stored)
                await uow.assets.add_asset(asset)
                plan = await uow.assets.get_plan(ws, plan_id)
                if plan is not None:
                    plan.status = "partial"
                    plan.version += 1
                    await uow.assets.update_plan(plan)
                await uow.commit()
            built.append(asset_id)

        if built:
            async with context.uow_factory() as uow:
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=ws,
                        actor_type=ActorType.USER,
                        actor_id=actor.user_id,
                        action=AuditAction.ABSTRACT_DOCUMENTS_BUILT,
                        subject_type="workspace",
                        subject_id=ws,
                        previous_hash=await uow.audit.latest_hash(ws),
                        reason_code="abstract_fallback",
                        metadata={"built": len(built)},
                    )
                )
                await uow.commit()
        if self._orchestrator is not None:
            for asset_id in built:
                await self._orchestrator.submit(
                    workspace_id=ws,
                    actor_id=actor.user_id,
                    stage=StageName.PARSING,
                    inputs={"asset_id": str(asset_id)},
                )
        remaining = len(await self._eligible(ws, limit=None)) if more else 0
        return {"built": len(built), "remaining": remaining, "enabled": 1}

    async def _eligible(self, ws: UUID, *, limit: int | None) -> list[tuple[UUID, UUID]]:
        """(plan, work) for included papers with an abstract that no route fetched.

        A few bulk reads, not one per plan: the Collections page asks for the
        count each time it loads, over tens of thousands of plans.
        """
        out: list[tuple[UUID, UUID]] = []
        async with self._context.uow_factory() as uow:
            plans = [
                plan
                for status in WAITING
                for plan in await uow.assets.list_plans(ws, status=status)
            ]
            if not plans:
                return out
            with_file = {
                asset.work_id
                for asset in await uow.assets.list_assets(ws)
                if not is_abstract(asset)
            }
            works = {
                work.id: work
                for work in await collect_pages(
                    lambda after, size: uow.works.list_works(ws, limit=size, after_id=after),
                    page_size=5_000,
                )
            }
            summaries = await uow.works.get_summaries(ws, [plan.work_id for plan in plans])
        for plan in sorted(plans, key=lambda p: str(p.id)):
            summary = summaries.get(plan.work_id)
            work = works.get(plan.work_id)
            if summary is None or not summary.has_abstract or plan.work_id in with_file:
                continue
            if work is None or not work.is_active or not work.relevance.is_included:
                continue
            out.append((plan.id, plan.work_id))
            if limit is not None and len(out) >= limit:
                break
        return out
