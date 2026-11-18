"""Draining a publisher and promoting its successor (phase 4).

Operator-driven, in three commands, each safe to repeat:

1. ``lab installation drain`` on the **old** publisher, restarted in
   maintenance so it serves nothing and claims nothing. It refuses while a
   publication or a leased task is in flight, and otherwise writes a statement
   of what the library last published.
2. On the **new** installation — restored from the old one's backup, configured
   with its own installation id and the next writer epoch — ``lab installation
   promote`` checks the statement against both heads and against its own
   database, then rewrites both heads to itself. Catalog first, then policy; a
   crash between them is resumed by running it again.
3. ``lab installation status`` anywhere: who the heads name, at which epoch.
   The old publisher, once promoted away from, sees that it is fenced and is
   restarted as a reader or not at all.

What this does not and cannot do is revoke the old publisher's credentials.
The heads fence its *publications*; its archive credentials and its machine
are an operator's to revoke (architecture §8), and promotion with
``--origin-fenced`` is the operator saying they have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.archive_catalog import MANIFEST_NAME
from lab_assistant.application.archive_control import (
    library_prefix,
    read_catalog_head,
    read_policy_head,
)
from lab_assistant.application.context import ServiceContext
from lab_assistant.application.ports import ArchiveControlStore
from lab_assistant.contracts.archive import SnapshotManifest
from lab_assistant.contracts.archive_policy import POLICY_HEAD_LOCATION
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.handover import HandoverStatement
from lab_assistant.contracts.publication import HEAD_LOCATION
from lab_assistant.domain.handover import (
    DrainBlocker,
    HandoverVerdict,
    HeadPlan,
    drain_blockers,
    evaluate_promotion,
)
from lab_assistant.domain.installation import InstallationRole
from lab_assistant.domain.states import TaskState

__all__ = ["DrainReport", "HandoverService", "PromotionReport"]


@dataclass(frozen=True, slots=True)
class DrainReport:
    statement: HandoverStatement | None
    blockers: tuple[DrainBlocker, ...] = ()

    @property
    def ok(self) -> bool:
        return self.statement is not None and not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "statement": self.statement.model_dump(mode="json") if self.statement else None,
            "blockers": [blocker.as_dict() for blocker in self.blockers],
        }


@dataclass(frozen=True, slots=True)
class PromotionReport:
    verdict: HandoverVerdict
    applied: bool = False
    #: Leased tasks inherited from the old publisher's database. Not reclaimed
    #: here: their leases expire, and the queue reclaims them as this
    #: installation's own. Reported so nobody is surprised by the work.
    inherited_leases: int = 0
    next_steps: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.verdict.permitted

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied": self.applied,
            "verdict": self.verdict.as_dict(),
            "inherited_leases": self.inherited_leases,
            "next_steps": list(self.next_steps),
        }


class HandoverService:
    def __init__(
        self,
        context: ServiceContext,
        *,
        control: ArchiveControlStore,
        library_id: UUID,
        installation_id: UUID,
        writer_epoch: int,
    ) -> None:
        self._context = context
        self._control = control
        self._library_id = library_id
        self._installation_id = installation_id
        self._writer_epoch = writer_epoch

    # ── status ─────────────────────────────────────────────────────────────

    async def status(self) -> dict[str, Any]:
        catalog, _ = await read_catalog_head(self._control, self._library_id)
        policy, _ = await read_policy_head(self._control, self._library_id)
        heads = [head for head in (catalog, policy) if head is not None]
        owns = bool(heads) and all(
            head.owner_installation_id == self._installation_id
            and head.writer_epoch == self._writer_epoch
            for head in heads
        )
        fenced = any(head.writer_epoch > self._writer_epoch for head in heads)
        return {
            "library_id": str(self._library_id),
            "this_installation": str(self._installation_id),
            "this_epoch": self._writer_epoch,
            "role": self._context.installation.role.value,
            "catalog_head": _head_summary(catalog, "commit_sequence"),
            "policy_head": _head_summary(policy, "policy_sequence"),
            "publishes_here": owns,
            # A later epoch on either head is a handover that moved past this
            # installation. It must not publish, and should not claim work.
            "fenced": fenced,
        }

    # ── drain ──────────────────────────────────────────────────────────────

    async def drain(self, workspace_id: UUID) -> DrainReport:
        catalog, _ = await read_catalog_head(self._control, self._library_id)
        policy, _ = await read_policy_head(self._control, self._library_id)
        heads = [head for head in (catalog, policy) if head is not None]
        owns = all(
            head.owner_installation_id == self._installation_id
            and head.writer_epoch == self._writer_epoch
            for head in heads
        )
        async with self._context.uow_factory() as uow:
            open_publications = len(await uow.publications.list_open(self._library_id))
            leased = len(
                await uow.tasks.list_tasks(workspace_id, state=TaskState.LEASED.value, limit=10_000)
            )
        blockers = drain_blockers(
            role_is_maintenance=self._context.installation.role is InstallationRole.MAINTENANCE,
            owns_heads=owns,
            open_publications=open_publications,
            leased_tasks=leased,
        )
        if blockers:
            return DrainReport(statement=None, blockers=blockers)

        statement = HandoverStatement(
            library_id=self._library_id,
            from_installation_id=self._installation_id,
            writer_epoch=self._writer_epoch,
            catalog_commit_sequence=catalog.commit_sequence if catalog else None,
            catalog_snapshot_id=catalog.snapshot_id if catalog else None,
            policy_sequence=policy.policy_sequence if policy else None,
            policy_checkpoint_digest=policy.checkpoint_digest if policy else None,
            drained_at=self._context.now(),
        )
        await self._audit(
            workspace_id,
            AuditAction.INSTALLATION_DRAINED,
            {"statement": statement.model_dump(mode="json")},
        )
        return DrainReport(statement=statement)

    # ── promote ────────────────────────────────────────────────────────────

    @staticmethod
    def fenced_statement(
        *, library_id: UUID, catalog_head, policy_head, now
    ) -> HandoverStatement | None:
        """A statement for an old publisher that cannot be reached.

        Built from the heads alone, and only because an operator asserted the
        old publisher is fenced. ``None`` when there is nothing to take over.
        """
        owner = catalog_head or policy_head
        if owner is None:
            return None
        return HandoverStatement(
            library_id=library_id,
            from_installation_id=owner.owner_installation_id,
            writer_epoch=owner.writer_epoch,
            catalog_commit_sequence=catalog_head.commit_sequence if catalog_head else None,
            catalog_snapshot_id=catalog_head.snapshot_id if catalog_head else None,
            policy_sequence=policy_head.policy_sequence if policy_head else None,
            policy_checkpoint_digest=policy_head.checkpoint_digest if policy_head else None,
            drained_at=now,
            asserted_fenced=True,
        )

    async def promote(
        self,
        workspace_id: UUID,
        *,
        statement: HandoverStatement | None,
        origin_fenced: bool = False,
        accept_loss: bool = False,
        dry_run: bool = True,
    ) -> PromotionReport:
        catalog, catalog_version = await read_catalog_head(self._control, self._library_id)
        policy, policy_version = await read_policy_head(self._control, self._library_id)
        if statement is None and origin_fenced:
            statement = self.fenced_statement(
                library_id=self._library_id,
                catalog_head=catalog,
                policy_head=policy,
                now=self._context.now(),
            )

        verdict = evaluate_promotion(
            statement,
            catalog_head=catalog,
            policy_head=policy,
            library_id=self._library_id,
            to_installation_id=self._installation_id,
            to_epoch=self._writer_epoch,
            state_recovered=await self._holds_published_state(workspace_id, catalog),
            accept_loss=accept_loss,
        )
        async with self._context.uow_factory() as uow:
            inherited = len(
                await uow.tasks.list_tasks(workspace_id, state=TaskState.LEASED.value, limit=10_000)
            )
        if not verdict.permitted or dry_run:
            return PromotionReport(
                verdict=verdict,
                inherited_leases=inherited,
                next_steps=()
                if not verdict.permitted
                else ("re-run with --apply to transfer both heads",),
            )

        assert statement is not None
        for plan, version in ((verdict.catalog, catalog_version), (verdict.policy, policy_version)):
            if plan is HeadPlan.TRANSFER and version is None:
                # A head with no version token cannot be swapped conditionally,
                # and an unconditional write here is how two promotions race.
                raise LabAssistantError(
                    "a library head carries no version token, so it cannot be transferred "
                    "conditionally"
                ).with_class(ErrorClass.CONFLICT)
        prefix = library_prefix(self._library_id)
        # Catalog first, then policy. Each swap is conditional on the version
        # just read, so a head that moved in between fails rather than being
        # overwritten — and a crash between the two leaves a promotion that
        # this same command resumes.
        if verdict.catalog is HeadPlan.TRANSFER and catalog is not None:
            moved = catalog.model_copy(
                update={
                    "owner_installation_id": self._installation_id,
                    "writer_epoch": self._writer_epoch,
                    "fenced_predecessor_id": statement.from_installation_id,
                    "updated_at": self._context.now(),
                }
            )
            await self._control.compare_and_swap(
                f"{prefix}{HEAD_LOCATION}",
                moved.serialise(),
                expected_version=catalog_version or "",
            )
        if verdict.policy is HeadPlan.TRANSFER and policy is not None:
            moved_policy = policy.model_copy(
                update={
                    "owner_installation_id": self._installation_id,
                    "writer_epoch": self._writer_epoch,
                    "fenced_predecessor_id": statement.from_installation_id,
                    "updated_at": self._context.now(),
                }
            )
            await self._control.compare_and_swap(
                f"{prefix}{POLICY_HEAD_LOCATION}",
                moved_policy.serialise(),
                expected_version=policy_version or "",
            )

        await self._audit(
            workspace_id,
            AuditAction.INSTALLATION_PROMOTED,
            {
                "from_installation_id": str(statement.from_installation_id),
                "from_epoch": statement.writer_epoch,
                "to_epoch": self._writer_epoch,
                "asserted_fenced": statement.asserted_fenced,
                "accepted_loss": accept_loss,
                "verdict": verdict.as_dict(),
            },
        )
        return PromotionReport(
            verdict=verdict,
            applied=True,
            inherited_leases=inherited,
            next_steps=(
                f"this installation publishes library {self._library_id} at epoch "
                f"{self._writer_epoch}",
                "restart the old publisher as a reader, or not at all; its publications are "
                "refused from now on, and its credentials are yours to revoke",
            ),
        )

    async def _holds_published_state(self, workspace_id: UUID, catalog) -> bool:
        """Does this database hold every release the library last published?

        Read from the published manifest rather than trusted from the
        statement: the releases a reader can see are the ones a successor must
        not lose.
        """
        if catalog is None:
            return True
        payload = await self._control.read(
            f"{library_prefix(self._library_id)}snapshots/{catalog.snapshot_id}/{MANIFEST_NAME}"
        )
        if payload is None:
            return False
        manifest = SnapshotManifest.model_validate(json.loads(payload.data))
        async with self._context.uow_factory() as uow:
            for release_id in manifest.scientific_release_ids:
                if await uow.releases.get(workspace_id, release_id) is None:
                    return False
        return True

    async def _audit(self, workspace_id: UUID, action: AuditAction, metadata: dict) -> None:
        async with self._context.uow_factory() as uow:
            await uow.audit.append(
                self._context.audit.build(
                    workspace_id=workspace_id,
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action=action,
                    subject_type="library",
                    subject_id=self._library_id,
                    previous_hash=await uow.audit.latest_hash(workspace_id),
                    reason_code=action.value,
                    metadata={
                        "installation_id": str(self._installation_id),
                        "writer_epoch": self._writer_epoch,
                        **metadata,
                    },
                )
            )
            await uow.commit()


def _head_summary(head, sequence_field: str) -> dict[str, Any] | None:
    if head is None:
        return None
    return {
        "owner_installation_id": str(head.owner_installation_id),
        "writer_epoch": head.writer_epoch,
        "sequence": getattr(head, sequence_field),
        "fenced_predecessor_id": (
            str(head.fenced_predecessor_id) if head.fenced_predecessor_id else None
        ),
    }
