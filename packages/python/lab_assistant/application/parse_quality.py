"""The two parse-quality limits a steward can adjust, and re-checking against them.

A parsed document is *degraded* when it misses a limit of its format's parser
profile, and a degraded document is not extracted or released. Two limits decide
most degraded documents, and both are judgement calls about a collection rather
than facts about a parser:

* the share of paragraphs that may repeat (running heads, page furniture), and
* whether the reference list must come out separate from the main text.

They are kept as a versioned configuration (``parser_profile`` kind, logical id
``parse-quality-limits``), so every change is on the record with who made it.
An unset value keeps each format's own default.

New parses use the limits in force. Documents already degraded are re-checked
when a steward next presses **Fetch included papers**: the metrics a parse
recorded are compared with the limits again, without parsing anything again, and
a document that now passes goes on to extraction. Tightening the limits never
demotes a document already accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.parsing import ParserProfile
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.renditions import rendition_quality_blockers
from lab_assistant.domain.scope import ApprovalState, ConfigKind, ConfigVersion
from lab_assistant.domain.states import ParseState
from lab_assistant.domain.workspace import Capability

__all__ = ["LIMITS_ID", "ParseQualityLimits", "ParseQualityService", "apply_limits"]

LIMITS_ID = "parse-quality-limits"

#: The formats a person reads as a document; tables and workbooks are not judged
#: by duplicated paragraphs or a reference list.
DOCUMENT_TYPES = ("application/pdf", "application/xml")


@dataclass(frozen=True, slots=True)
class ParseQualityLimits:
    """A steward's choice; ``None`` keeps each format's own default."""

    max_duplicate_paragraph_ratio: float | None = None
    require_reference_separation: bool | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ParseQualityLimits:
        ratio = payload.get("max_duplicate_paragraph_ratio")
        separation = payload.get("require_reference_separation")
        return cls(
            max_duplicate_paragraph_ratio=float(ratio) if ratio is not None else None,
            require_reference_separation=bool(separation) if separation is not None else None,
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "max_duplicate_paragraph_ratio": self.max_duplicate_paragraph_ratio,
            "require_reference_separation": self.require_reference_separation,
        }


def apply_limits(profile: ParserProfile, limits: ParseQualityLimits) -> ParserProfile:
    """The profile with the steward's limits in place of its defaults."""
    if not profile.media_types & set(DOCUMENT_TYPES):
        return profile
    update: dict[str, Any] = {}
    if limits.max_duplicate_paragraph_ratio is not None:
        update["max_duplicate_paragraph_ratio"] = limits.max_duplicate_paragraph_ratio
    if limits.require_reference_separation is not None:
        update["require_reference_separation"] = limits.require_reference_separation
    return profile.model_copy(update=update) if update else profile


async def current_limits(uow: Any, workspace_id: UUID) -> tuple[ParseQualityLimits, Any]:
    """The limits in force (the newest version) and that version, if any."""
    versions = [
        config
        for config in await uow.configs.list_config_versions(
            workspace_id, kind=ConfigKind.PARSER_PROFILE.value
        )
        if config.logical_id == LIMITS_ID
    ]
    if not versions:
        return ParseQualityLimits(), None
    newest = max(versions, key=lambda c: (c.created_at is not None, c.created_at, c.id))
    return ParseQualityLimits.from_payload(newest.payload), newest


class ParseQualityService:
    def __init__(self, context: ServiceContext, orchestrator: Any | None = None) -> None:
        self._context = context
        self._orchestrator = orchestrator

    async def view(self, actor: ActorContext) -> dict[str, Any]:
        """The limits in force, each format's default, and why documents are degraded."""
        actor.require(Capability.REVIEW_PARSE)
        parsers = self._context.require_parsers()
        ws = actor.workspace_id
        async with self._context.uow_factory() as uow:
            limits, version = await current_limits(uow, ws)
            reasons, degraded = await self._degraded_reasons(uow, ws)
        defaults = {
            media_type: {
                "profile": parsers.profile_for(media_type).profile_id,
                "max_duplicate_paragraph_ratio": parsers.profile_for(
                    media_type
                ).max_duplicate_paragraph_ratio,
                "require_reference_separation": parsers.profile_for(
                    media_type
                ).require_reference_separation,
            }
            for media_type in DOCUMENT_TYPES
        }
        return {
            "limits": limits.as_payload(),
            "defaults": defaults,
            "set_at": version.created_at.isoformat() if version and version.created_at else None,
            "degraded": degraded,
            "degraded_by_reason": reasons,
        }

    async def set_limits(self, actor: ActorContext, limits: ParseQualityLimits) -> dict[str, Any]:
        """Record new limits. They apply to new parses at once and to degraded ones on re-check."""
        actor.require(Capability.REVIEW_PARSE)
        ratio = limits.max_duplicate_paragraph_ratio
        if ratio is not None and not 0.0 <= ratio <= 1.0:
            raise ValidationFailure("the duplicate paragraph share is between 0 and 1")
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            _, previous = await current_limits(uow, ws)
            number = 1
            if previous is not None:
                number = int(previous.semantic_version.split(".")[0]) + 1
            config = ConfigVersion(
                id=context.new_id(),
                workspace_id=ws,
                kind=ConfigKind.PARSER_PROFILE,
                logical_id=LIMITS_ID,
                semantic_version=f"{number}.0.0",
                payload=limits.as_payload(),
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
                    action=AuditAction.PARSE_LIMITS_CHANGED,
                    subject_type="config_version",
                    subject_id=config.id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="parse_quality_limits",
                    metadata={
                        "limits": limits.as_payload(),
                        "previous": previous.payload if previous else None,
                    },
                )
            )
            await uow.commit()
        return await self.view(actor)

    async def recheck_degraded(self, actor: ActorContext) -> dict[str, int]:
        """Compare every degraded document with the limits in force; promote what passes.

        Reads the metrics each parse recorded; nothing is parsed again. A document
        that now passes is marked ready and queued for extraction.
        """
        actor.require(Capability.REVIEW_PARSE)
        context = self._context
        parsers = context.require_parsers()
        ws = actor.workspace_id
        promoted: list[UUID] = []
        still = 0
        async with context.uow_factory() as uow:
            limits, _ = await current_limits(uow, ws)
            for rendition in await uow.renditions.list_renditions(ws):
                if rendition.quality_status is not ParseState.DEGRADED:
                    continue
                run = await uow.renditions.get_parse_run(ws, rendition.parse_run_id)
                asset = await uow.assets.get_asset(ws, rendition.asset_id)
                if run is None or run.quality_metrics is None or asset is None:
                    still += 1
                    continue
                profile = apply_limits(parsers.profile_for(asset.media_type_detected), limits)
                if rendition_quality_blockers(run.quality_metrics, profile):
                    still += 1
                    continue
                rendition.quality_status = ParseState.READY
                rendition.version += 1
                await uow.renditions.update_rendition(rendition)
                promoted.append(rendition.id)
            if promoted:
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=ws,
                        actor_type=ActorType.USER,
                        actor_id=actor.user_id,
                        action=AuditAction.PARSE_LIMITS_CHANGED,
                        subject_type="workspace",
                        subject_id=ws,
                        previous_hash=await uow.audit.latest_hash(ws),
                        reason_code="degraded_rechecked",
                        metadata={
                            "promoted": len(promoted),
                            "still_degraded": still,
                            "limits": limits.as_payload(),
                        },
                    )
                )
            await uow.commit()
        if self._orchestrator is not None:
            for rendition_id in promoted:
                await self._orchestrator.submit(
                    workspace_id=ws,
                    actor_id=actor.user_id,
                    stage=StageName.EXTRACTION,
                    inputs={"rendition_id": str(rendition_id)},
                )
        return {"promoted": len(promoted), "still_degraded": still}

    async def _degraded_reasons(self, uow: Any, ws: UUID) -> tuple[dict[str, int], int]:
        """How many degraded documents miss each limit, worded as the parse recorded it."""
        reasons: dict[str, int] = {}
        degraded = 0
        parsers = self._context.require_parsers()
        for rendition in await uow.renditions.list_renditions(ws):
            if rendition.quality_status is not ParseState.DEGRADED:
                continue
            degraded += 1
            run = await uow.renditions.get_parse_run(ws, rendition.parse_run_id)
            asset = await uow.assets.get_asset(ws, rendition.asset_id)
            if run is None or run.quality_metrics is None or asset is None:
                continue
            # Against the format's own defaults: what the parse missed, not what
            # it would miss under the limits being considered.
            for blocker in rendition_quality_blockers(
                run.quality_metrics, parsers.profile_for(asset.media_type_detected)
            ):
                key = _reason(blocker)
                reasons[key] = reasons.get(key, 0) + 1
        return dict(sorted(reasons.items(), key=lambda item: -item[1])), degraded


def _reason(blocker: str) -> str:
    """A blocker without its measured number, so like ones are counted together."""
    if blocker.startswith("duplicate paragraph ratio"):
        return "too many repeated paragraphs"
    if blocker.startswith("reference section"):
        return "reference list not separated"
    if blocker.startswith("page coverage"):
        return "pages without text"
    if blocker.startswith("replacement character"):
        return "unreadable characters"
    if blocker.startswith("only "):
        return "too little text"
    return blocker
