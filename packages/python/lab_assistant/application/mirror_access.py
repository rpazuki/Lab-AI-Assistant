"""What a mirror chooses not to serve.

A mirror (a reader installation) serves the corpus its publisher published:
every scope, paper and release in it comes from the publisher, and an import
adds to it. What the mirror's own admin decides is narrower: which of those
scopes and releases this mirror stops serving, for everyone on it, admins
included.

- **A removed scope** takes its papers with it: a paper is withheld when every
  scope that found it is removed. A paper another scope also found stays, as it
  stays reachable through that scope on the publisher. A paper no scope found
  is untouched.
- **A removed release** is never served. Ask, Search and Analytics follow the
  publisher's choice of release while it is not removed, and otherwise the
  newest published release that is not. Putting a release back restores the
  publisher's choice at once, because nothing was moved to remove it.

Stored as a ``workspace_profile`` config version, ``mirror-access``, so each
change is a new version with who made it and when, and an import never touches
it: imports write corpus rows, never this mirror's own settings.

Only a reader keeps this list. A publisher decides what it serves by publishing
and by scope access rules, and a second way to hide material there would leave
two answers to "why can nobody see this paper".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import iter_pages
from lab_assistant.contracts.errors import InstallationDenied, NotFound, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.domain.installation import InstallationRole
from lab_assistant.domain.scope import ApprovalState, ConfigKind, ConfigVersion
from lab_assistant.domain.workspace import Capability

__all__ = [
    "MIRROR_ACCESS_ID",
    "MirrorAccessService",
    "Withheld",
    "read_withheld",
    "served_release_id",
    "withheld_work_ids",
]

MIRROR_ACCESS_ID = "mirror-access"


@dataclass(frozen=True, slots=True)
class Withheld:
    """The scopes and releases this mirror does not serve."""

    scope_ids: frozenset[UUID] = field(default_factory=frozenset)
    release_ids: frozenset[UUID] = field(default_factory=frozenset)


async def _setting(uow: Any, workspace_id: UUID) -> ConfigVersion | None:
    versions = [
        config
        for config in await uow.configs.list_config_versions(
            workspace_id, kind=ConfigKind.WORKSPACE_PROFILE.value
        )
        if config.logical_id == MIRROR_ACCESS_ID
    ]
    if not versions:
        return None
    return max(versions, key=lambda c: (c.created_at is not None, c.created_at, c.id))


async def read_withheld(uow: Any, workspace_id: UUID) -> Withheld:
    """This mirror's list, or nothing withheld when it has none (as on a publisher)."""
    setting = await _setting(uow, workspace_id)
    if setting is None:
        return Withheld()
    payload = setting.payload or {}
    return Withheld(
        scope_ids=frozenset(UUID(str(v)) for v in payload.get("withheld_scope_ids") or ()),
        release_ids=frozenset(UUID(str(v)) for v in payload.get("withheld_release_ids") or ()),
    )


async def withheld_work_ids(uow: Any, workspace_id: UUID, withheld: Withheld) -> set[UUID]:
    """Papers every one of whose scopes is removed. None when no scope is."""
    if not withheld.scope_ids:
        return set()
    scopes = await uow.imports.list_work_scopes(workspace_id)
    return {
        work_id
        for work_id, scope_ids in scopes.items()
        if scope_ids and set(scope_ids) <= withheld.scope_ids
    }


async def _servable(uow: Any, workspace_id: UUID) -> list[Any]:
    """Published and superseded releases, newest first."""
    releases = [
        release
        async for release in iter_pages(
            lambda after, size: uow.releases.list_all_releases(
                workspace_id, limit=size, after_id=after
            )
        )
        if release.status.is_servable
    ]
    return sorted(
        releases,
        key=lambda r: (r.published_at is not None, r.published_at, r.semantic_version),
        reverse=True,
    )


async def served_release_id(
    uow: Any, workspace_id: UUID, *, release_id: UUID | None, alias: str
) -> UUID:
    """The release a request is answered from, keeping out what this mirror removed.

    A pinned release that is removed is refused as absent, the way a release
    the caller has never heard of is. An alias on a removed release falls back
    to the newest published release that is not removed.
    """
    withheld = await read_withheld(uow, workspace_id)
    if release_id is not None:
        release_id = UUID(str(release_id))
        if release_id in withheld.release_ids:
            raise NotFound(f"release {release_id} is not served here")
        return release_id
    search_alias = await uow.releases.get_alias(workspace_id, alias)
    if search_alias is None or search_alias.release_id is None:
        raise NotFound(f"no release is published on alias {alias!r}")
    if search_alias.release_id not in withheld.release_ids:
        return search_alias.release_id
    for release in await _servable(uow, workspace_id):
        if release.id not in withheld.release_ids:
            return release.id
    raise NotFound("every published release has been removed from this mirror")


class MirrorAccessService:
    """Reading and changing what this mirror does not serve.

    Changing it needs ``MANAGE_POLICY``, which only an admin holds, as for scope
    access on the publisher: deciding who sees what is an administrative act.
    """

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def _require_mirror(self) -> None:
        if self._context.installation.role is not InstallationRole.READER:
            raise InstallationDenied(
                "only a mirror removes scopes or releases; on the publisher, use scope "
                "access rules and the Releases page",
                context={"installation_role": self._context.installation.role.value},
            )

    async def view(self, actor: ActorContext) -> dict[str, Any]:
        """Every scope and release this mirror holds, and which ones it does not serve."""
        actor.require(Capability.MANAGE_POLICY)
        self._require_mirror()
        ws = actor.workspace_id
        async with self._context.uow_factory() as uow:
            setting = await _setting(uow, ws)
            withheld = await read_withheld(uow, ws)
            versions = await uow.configs.list_scope_versions(ws)
            scopes_by_work = await uow.imports.list_work_scopes(ws)
            releases = await _servable(uow, ws)
            alias = await uow.releases.get_alias(ws, "current")
            try:
                serving = await served_release_id(uow, ws, release_id=None, alias="current")
            except NotFound:
                serving = None
        newest: dict[UUID, Any] = {}
        for version in versions:
            known = newest.get(version.scope_id)
            if known is None or (version.version or 0) > (known.version or 0):
                newest[version.scope_id] = version
        papers: dict[UUID, int] = {}
        for scope_ids in scopes_by_work.values():
            for scope_id in scope_ids:
                papers[scope_id] = papers.get(scope_id, 0) + 1
        hidden = await self._withheld_count(ws, withheld)
        return {
            "scopes": [
                {
                    "scope_id": str(scope_id),
                    "name": version.name,
                    "papers": papers.get(scope_id, 0),
                    "withheld": scope_id in withheld.scope_ids,
                }
                for scope_id, version in sorted(
                    newest.items(), key=lambda item: item[1].name.lower()
                )
            ],
            "releases": [
                {
                    "release_id": str(release.id),
                    "name": release.name,
                    "semantic_version": release.semantic_version,
                    "status": release.status.value,
                    "published_at": (
                        release.published_at.isoformat() if release.published_at else None
                    ),
                    "withheld": release.id in withheld.release_ids,
                    "publisher_choice": alias is not None and alias.release_id == release.id,
                    "serving": release.id == serving,
                }
                for release in releases
            ],
            "serving_release_id": str(serving) if serving else None,
            "withheld_papers": hidden,
            "set_at": setting.created_at.isoformat() if setting and setting.created_at else None,
            "set_by": str(setting.created_by) if setting and setting.created_by else None,
        }

    async def _withheld_count(self, workspace_id: UUID, withheld: Withheld) -> int:
        async with self._context.uow_factory() as uow:
            return len(await withheld_work_ids(uow, workspace_id, withheld))

    async def save(
        self,
        actor: ActorContext,
        *,
        withheld_scope_ids: list[UUID],
        withheld_release_ids: list[UUID],
    ) -> dict[str, Any]:
        """Record the whole list: what is named is removed, what is not is served."""
        actor.require(Capability.MANAGE_POLICY)
        self._require_mirror()
        withheld_scope_ids = [UUID(str(value)) for value in withheld_scope_ids]
        withheld_release_ids = [UUID(str(value)) for value in withheld_release_ids]
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            known_scopes = {v.scope_id for v in await uow.configs.list_scope_versions(ws)}
            known_releases = {release.id for release in await _servable(uow, ws)}
            unknown = [str(s) for s in withheld_scope_ids if s not in known_scopes] + [
                str(r) for r in withheld_release_ids if r not in known_releases
            ]
            if unknown:
                raise ValidationFailure(
                    "these are not scopes or published releases on this mirror",
                    context={"unknown": unknown},
                )
            previous = await _setting(uow, ws)
            number = int(previous.semantic_version.split(".")[0]) + 1 if previous else 1
            payload = {
                "withheld_scope_ids": sorted({str(s) for s in withheld_scope_ids}),
                "withheld_release_ids": sorted({str(r) for r in withheld_release_ids}),
            }
            config = ConfigVersion(
                id=context.new_id(),
                workspace_id=ws,
                kind=ConfigKind.WORKSPACE_PROFILE,
                logical_id=MIRROR_ACCESS_ID,
                semantic_version=f"{number}.0.0",
                payload=payload,
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
                    action=AuditAction.ADMIN_ACTION,
                    subject_type="config_version",
                    subject_id=config.id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="mirror_access_changed",
                    metadata=payload,
                )
            )
            await uow.commit()
        return await self.view(actor)
