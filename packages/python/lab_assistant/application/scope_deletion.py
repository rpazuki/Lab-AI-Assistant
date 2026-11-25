"""Deleting a whole scope: every version, its discovery runs, and its candidates.

A scope is deleted as a whole, whichever version the request names. What goes:

* every version of it, approved, retired or draft, and the review history filed
  under them;
* every discovery run of those versions, with its pages, records, assertions and
  cursors;
* every paper only those runs found, with everything made from it: decisions,
  duplicate pairs, acquisitions, files, parses, extracted values and their
  reviews, and any papers merged into it;
* the draft or rejected releases that hold those papers, and the settled
  background tasks that name any of it.

A paper another scope's runs also found stays. Only this scope's records of it
go, and its summary is rebuilt from what is left.

Refused, with every reason at once, when deleting would rewrite what someone has
relied on: a release past draft holds its papers or names its versions (it has
been indexed and may have been served); a conversation, a question or an
evaluation used a release that would go; work for any of it is still queued or
running; or another scope searched a source with the same query, whose run may
rest on this scope's pages.

The audit log is not touched: it records the deletion, with the counts and the
versions' checksums. The bytes in the object store stay too: they are shared by
digest, as ``delete_run`` documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.discovery import cursor_lineage_key
from lab_assistant.application.pagination import collect_pages, every_session
from lab_assistant.application.reconciliation import refresh_summaries
from lab_assistant.contracts.errors import NotFound, StateTransitionError, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.observation import DiscoveryRun
from lab_assistant.domain.release import CorpusRelease
from lab_assistant.domain.scope import ScopeVersion
from lab_assistant.domain.states import ReleaseState
from lab_assistant.domain.workspace import Capability

__all__ = ["ScopeDeletionPlan", "ScopeDeletionService"]

#: A release in one of these states has never been indexed or served.
_DELETABLE_RELEASES = frozenset({ReleaseState.DRAFT, ReleaseState.VALIDATED, ReleaseState.REJECTED})

#: Task inputs that name something a scope deletion removes.
_TASK_REFERENCES = (
    "discovery_run_id",
    "work_id",
    "plan_id",
    "asset_id",
    "rendition_id",
    "release_id",
)


@dataclass(slots=True)
class ScopeDeletionPlan:
    """What deleting a scope would remove, and what stops it."""

    scope_id: UUID
    name: str
    versions: list[ScopeVersion]
    runs: list[DiscoveryRun] = field(default_factory=list)
    #: Papers that go: found only by this scope's runs, and those merged into them.
    papers: set[UUID] = field(default_factory=set)
    #: Papers other scopes also found: they stay, without this scope's records.
    shared: set[UUID] = field(default_factory=set)
    dependants: dict[str, set[UUID]] = field(default_factory=dict)
    #: Of ``papers``, how many a person decided.
    decided_by_person: int = 0
    releases: list[CorpusRelease] = field(default_factory=list)
    task_ids: list[UUID] = field(default_factory=list)
    lineage_keys: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        d = self.dependants
        return {
            "versions": len(self.versions),
            "runs": len(self.runs),
            "papers": len(self.papers),
            "papers_kept": len(self.shared),
            "decided_by_person": self.decided_by_person,
            "files": len(d.get("assets", ())),
            "parsed_texts": len(d.get("renditions", ())),
            "values": len(d.get("claims", ())),
            "value_reviews": len(d.get("reviews", ())),
            "draft_releases": len(self.releases),
            "tasks": len(self.task_ids),
        }

    def view(self) -> dict[str, Any]:
        return {
            "scope_id": str(self.scope_id),
            "name": self.name,
            "versions": [
                {"id": str(v.id), "version": v.version, "approval_state": v.approval_state.value}
                for v in sorted(self.versions, key=lambda v: v.version)
            ],
            "counts": self.counts(),
            "blockers": list(self.blockers),
            "deletable": not self.blockers,
        }


def _searches(run: DiscoveryRun) -> set[tuple[str, str, str]]:
    return {
        (state.connector_id, state.connector_version, state.query_fingerprint)
        for state in run.connector_states
    }


class ScopeDeletionService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def plan(self, actor: ActorContext, scope_version_id: UUID) -> ScopeDeletionPlan:
        """What deleting the scope of this version would remove. Changes nothing."""
        actor.require(Capability.MANAGE_SCOPE)
        async with self._context.uow_factory() as uow:
            return await self._plan(uow, actor, scope_version_id)

    async def delete(
        self, actor: ActorContext, scope_version_id: UUID, *, confirm_name: str
    ) -> dict[str, int]:
        """Delete the scope of this version, as ``plan`` describes. Returns the counts."""
        context = self._context
        context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.MANAGE_SCOPE)
        async with context.uow_factory() as uow:
            plan = await self._plan(uow, actor, scope_version_id)
            if confirm_name.strip() != plan.name.strip():
                raise ValidationFailure(
                    "type the scope's name exactly to confirm deleting it",
                    context={"name": plan.name},
                )
            if plan.blockers:
                raise StateTransitionError(
                    "this scope cannot be deleted: " + "; ".join(plan.blockers),
                    context={"blockers": plan.blockers},
                )
            ws = actor.workspace_id
            await uow.tasks.delete_tasks(ws, plan.task_ids)
            for release in plan.releases:
                await uow.releases.delete(ws, release.id)
            await uow.purge.purge(
                ws,
                work_ids=sorted(plan.papers, key=str),
                dependants=plan.dependants,
                run_ids=[run.id for run in plan.runs],
                scope_version_ids=[v.id for v in plan.versions],
                lineage_keys=plan.lineage_keys,
            )
            await refresh_summaries(uow, ws, sorted(plan.shared, key=str), now=context.now())
            counts = plan.counts()
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.SCOPE_DELETED,
                    subject_type="scope",
                    subject_id=plan.scope_id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="scope_deleted",
                    metadata={
                        "name": plan.name,
                        "counts": counts,
                        "versions": [
                            {
                                "id": str(v.id),
                                "version": v.version,
                                "approval_state": v.approval_state.value,
                                "checksum": v.config_checksum,
                            }
                            for v in plan.versions
                        ],
                        "runs": [str(run.id) for run in plan.runs],
                        "releases": [str(release.id) for release in plan.releases],
                    },
                )
            )
            await uow.commit()
        if context.search_index is not None:
            for release in plan.releases:
                await context.search_index.delete_release_projections(ws, release.id)
        return counts

    async def _plan(
        self, uow: Any, actor: ActorContext, scope_version_id: UUID
    ) -> ScopeDeletionPlan:
        ws = actor.workspace_id
        named = await uow.configs.get_scope_version(ws, scope_version_id)
        if named is None:
            raise NotFound(f"scope version {scope_version_id} not found")
        all_versions = await uow.configs.list_scope_versions(ws)
        versions = [v for v in all_versions if v.scope_id == named.scope_id]
        latest = max(versions, key=lambda v: v.version)
        plan = ScopeDeletionPlan(scope_id=named.scope_id, name=latest.name, versions=versions)
        version_ids = {v.id for v in versions}
        blockers = plan.blockers

        all_runs = await uow.observations.list_runs(ws, limit=100_000)
        plan.runs = [run for run in all_runs if run.scope_version_id in version_ids]
        run_ids = {run.id for run in plan.runs}
        mine = set().union(*(_searches(run) for run in plan.runs)) if plan.runs else set()
        names = {v.id: v.name for v in all_versions}
        for other in all_runs:
            if other.id in run_ids or not (_searches(other) & mine):
                continue
            blockers.append(
                f"“{names.get(other.scope_version_id, 'another scope')}” searched a source with "
                "the same query, and its run may rest on this scope's pages"
            )
            break
        plan.lineage_keys = sorted(
            {
                cursor_lineage_key(
                    workspace_id=ws,
                    scope_version_id=run.scope_version_id,
                    connector_id=state.connector_id,
                    connector_version=state.connector_version,
                    query_fingerprint=state.query_fingerprint,
                    cursor_semantics_version="1",
                )
                for run in plan.runs
                for state in run.connector_states
            }
        )

        # The papers: found only by these runs, and every paper merged into one.
        only, shared = await uow.purge.works_found_by_runs(ws, sorted(run_ids, key=str))
        works = await collect_pages(
            lambda after, size: uow.works.list_works(ws, limit=size, after_id=after),
            page_size=5_000,
        )
        children: dict[UUID, list[UUID]] = {}
        for work in works:
            if work.merged_into_work_id is not None:
                children.setdefault(work.merged_into_work_id, []).append(work.id)
        papers, frontier = set(only), list(only)
        while frontier:
            for child in children.get(frontier.pop(), ()):
                if child not in papers:
                    papers.add(child)
                    frontier.append(child)
        plan.papers, plan.shared = papers, shared - papers
        plan.decided_by_person = len(papers & await uow.works.human_relevance_work_ids(ws))
        plan.dependants = await uow.purge.work_dependants(ws, sorted(papers, key=str))

        # Releases: any past draft stops it; a draft goes with it unless used.
        holding = await uow.purge.releases_holding(ws, sorted(papers, key=str))
        releases = await collect_pages(
            lambda after, size: uow.releases.list_all_releases(ws, limit=size, after_id=after)
        )
        touched = [r for r in releases if r.id in holding or version_ids & set(r.scope_version_ids)]
        for release in touched:
            label = f"release “{release.name}” {release.semantic_version}"
            if release.status not in _DELETABLE_RELEASES:
                blockers.append(
                    f"{label} is {release.status.value} and holds its papers; a release "
                    "from approval on has been indexed and stays"
                )
        going = [r for r in touched if r.status in _DELETABLE_RELEASES]
        going_ids = {r.id for r in going}
        for release in going:
            label = f"release “{release.name}” {release.semantic_version}"
            if await uow.evaluations.list_runs(ws, release_id=release.id, limit=1):
                blockers.append(f"an evaluation ran against {label}")
            if any(
                r.id not in going_ids and release.id in (r.parent_release_id, r.superseded_by_id)
                for r in releases
            ):
                blockers.append(f"another release is built on {label}")
        if going_ids:
            for alias in await uow.releases.list_aliases(ws):
                if alias.release_id in going_ids:
                    blockers.append(
                        f"the “{alias.alias}” alias serves a release that holds its papers"
                    )
            for session in await every_session(uow, ws):
                if session.pinned_release_id in going_ids:
                    blockers.append("a conversation is pinned to a release that holds its papers")
                    break
                messages = await uow.chat.list_messages(ws, session.id)
                if any(message.release_id in going_ids for message in messages):
                    blockers.append("a question was answered from a release that holds its papers")
                    break
        plan.releases = going

        # Background work: settled tasks go; anything still pending stops it.
        d = plan.dependants
        named_ids = {
            str(i)
            for i in (
                run_ids
                | papers
                | going_ids
                | d.get("plans", set())
                | d.get("assets", set())
                | d.get("renditions", set())
            )
        }
        unsettled = 0
        for task in await uow.tasks.list_tasks(ws, limit=100_000):
            refers = (task.run_id is not None and task.run_id in run_ids) or any(
                str(task.inputs.get(key)) in named_ids
                for key in _TASK_REFERENCES
                if task.inputs.get(key) is not None
            )
            if not refers:
                continue
            if task.state.is_settled:
                plan.task_ids.append(task.id)
            else:
                unsettled += 1
        if unsettled:
            blockers.append(
                f"{unsettled} background task{'s' if unsettled != 1 else ''} for it "
                f"{'are' if unsettled != 1 else 'is'} still queued or running; cancel or wait"
            )
        return plan
