"""Issuing a library's policy checkpoints (phase 2).

The catalog says what the corpus *is*. A checkpoint says what may be done with
it *now*, and it is issued far more often: after every revocation, and
otherwise on a timer, because a checkpoint carries a validity window and a
reader stops trusting one that has not been renewed. Re-issuing identical
content is therefore the normal heartbeat rather than a duplicate, and each
issue takes the next sequence.

The protocol is the catalog head's, made smaller by the fact that a checkpoint
is one object rather than a directory:

1. **check both heads** — the policy head about to move and the catalog head
   beside it (`domain/archive_policy.evaluate_policy_commit`). A writer fenced
   on either is fenced on both;
2. **build** the checkpoint from the producer's database and sign it;
3. **upload** it create-only, under a key that contains its digest. A crash now
   leaves an object nobody references: litter, not a policy;
4. **swap the policy head**, conditional on the version read at step 1. The
   only moment anything becomes visible.

There is no outbox, deliberately. The catalog needs one because publishing the
same work twice claims two sequences for one snapshot. A checkpoint is state,
not work: if the swap succeeds and the process dies before it can say so, the
head already says what happened, and the next issue simply supersedes it. And
for the reason the catalog protocol gives, a failed swap is never retried — it
can mean a handover fenced this writer.

**What a checkpoint covers is the published closure and nothing else**: rights
decisions reachable from a release that was ever published, those releases'
lifecycle states, retracted or corrected works among them, and tombstones on
their payloads. A reader can hold nothing outside that closure, and the
producer's drafts, candidates and unpublished acquisitions are not a reader's
business — listing them would disclose what the lab is working on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from lab_assistant.application.archive_control import (
    library_prefix,
    read_catalog_head,
    read_policy_head,
)
from lab_assistant.application.context import ServiceContext
from lab_assistant.application.pagination import iter_pages
from lab_assistant.application.ports import ArchiveControlStore
from lab_assistant.contracts.archive_policy import (
    POLICY_HEAD_LOCATION,
    PolicyCheckpoint,
    PolicyHeadDescriptor,
    ReleaseStanding,
    RightsStanding,
    TombstonedArtifact,
    WorkStanding,
)
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.signing import policy_statement_for
from lab_assistant.domain.archive_policy import evaluate_policy_commit, validity_window
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.publication import CommitRefusal, CommitVerdict

__all__ = [
    "PolicyCheckpointBuilder",
    "PolicyIssueReport",
    "PolicyService",
]


@dataclass(frozen=True, slots=True)
class PolicyIssueReport:
    """What one issue did, or would have done."""

    verdict: CommitVerdict
    checkpoint: PolicyCheckpoint | None = None
    head: PolicyHeadDescriptor | None = None
    published: bool = False
    #: False when the content is identical to the checkpoint it supersedes: a
    #: renewal of the validity window and nothing else.
    changed: bool = True
    uploaded: bool = False
    problems: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.verdict.permitted and not self.problems

    def as_dict(self) -> dict[str, Any]:
        checkpoint = self.checkpoint
        return {
            "verdict": self.verdict.as_dict(),
            "published": self.published,
            "changed": self.changed,
            "uploaded": self.uploaded,
            "checkpoint": {
                "checkpoint_id": str(checkpoint.checkpoint_id),
                "policy_sequence": checkpoint.policy_sequence,
                "digest": checkpoint.signing_digest(),
                "content_digest": checkpoint.content_digest(),
                "issued_at": checkpoint.issued_at.isoformat(),
                "valid_until": checkpoint.valid_until.isoformat(),
                "public_valid_until": checkpoint.public_valid_until.isoformat(),
                "signed": checkpoint.is_signed,
                "key_identifier": checkpoint.key_identifier,
                "counts": {
                    "rights": len(checkpoint.rights),
                    "works": len(checkpoint.works),
                    "releases": len(checkpoint.releases),
                    "tombstones": len(checkpoint.tombstones),
                },
            }
            if checkpoint
            else None,
            "head": {
                "policy_sequence": self.head.policy_sequence,
                "checkpoint_digest": self.head.checkpoint_digest,
                "writer_epoch": self.head.writer_epoch,
                "owner_installation_id": str(self.head.owner_installation_id),
            }
            if self.head
            else None,
            "problems": list(self.problems),
            "next_steps": list(self.next_steps),
        }


class PolicyCheckpointBuilder:
    """Reads the producer's current policy over the published closure."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        library_id: UUID,
        installation_id: UUID,
        writer_epoch: int = 0,
        validity: timedelta = timedelta(hours=24),
        public_validity: timedelta | None = None,
        signer: Any | None = None,
    ) -> None:
        self._context = context
        self._library_id = library_id
        self._installation_id = installation_id
        self._writer_epoch = writer_epoch
        self._validity = validity
        self._public_validity = public_validity
        #: Optional, as for snapshots: an unsigned checkpoint is readable and
        #: says nothing about who issued it.
        self._signer = signer

    async def build(
        self,
        workspace_id: UUID,
        *,
        policy_sequence: int,
        parent_checkpoint_digest: str | None = None,
    ) -> PolicyCheckpoint:
        rights, works, releases, digests = await self._walk(workspace_id)
        tombstones = await self._tombstones(workspace_id, digests)

        issued_at = self._context.now()
        valid_until, public_valid_until = validity_window(
            issued_at, validity=self._validity, public_validity=self._public_validity
        )
        checkpoint = PolicyCheckpoint(
            library_id=self._library_id,
            source_workspace_id=workspace_id,
            checkpoint_id=self._context.new_id(),
            policy_sequence=policy_sequence,
            parent_checkpoint_digest=parent_checkpoint_digest,
            origin_installation_id=self._installation_id,
            writer_epoch=self._writer_epoch,
            issued_at=issued_at,
            valid_until=valid_until,
            public_valid_until=public_valid_until,
            # Sorted, so two builds over one state produce one content digest:
            # the difference between a renewal and a change has to be visible.
            rights=tuple(sorted(rights.values(), key=lambda item: str(item.decision_id))),
            works=tuple(sorted(works.values(), key=lambda item: str(item.work_id))),
            releases=tuple(sorted(releases.values(), key=lambda item: str(item.release_id))),
            tombstones=tuple(sorted(tombstones, key=lambda item: item.sha256)),
        )
        return self._sign(checkpoint)

    async def _walk(self, workspace_id: UUID):
        """Releases ever published, and everything a reader of them may hold."""
        rights: dict[UUID, RightsStanding] = {}
        works: dict[UUID, WorkStanding] = {}
        releases: dict[UUID, ReleaseStanding] = {}
        digests: set[str] = set()
        seen_assets: set[UUID] = set()
        seen_works: set[UUID] = set()
        seen_renditions: set[UUID] = set()

        async with self._context.uow_factory() as uow:
            published = [
                release
                async for release in iter_pages(
                    lambda after, size: uow.releases.list_all_releases(
                        workspace_id, limit=size, after_id=after
                    )
                )
                # Ever published, not currently servable: a withdrawn release
                # is exactly the one a reader must be told about.
                if release.published_at is not None
            ]

        for release in published:
            releases[release.id] = ReleaseStanding(
                release_id=release.id, status=_value(release.status)
            )
            async with self._context.uow_factory() as uow:
                for item in await uow.releases.list_items(workspace_id, release.id):
                    record = item.record
                    if record.work_id not in seen_works:
                        seen_works.add(record.work_id)
                        work = await uow.works.get(workspace_id, record.work_id)
                        if work is not None and (
                            work.retraction_status != "none" or work.correction_status != "none"
                        ):
                            works[work.id] = WorkStanding(
                                work_id=work.id,
                                retraction_status=work.retraction_status,
                                correction_status=work.correction_status,
                            )
                    if record.asset_id is not None and record.asset_id not in seen_assets:
                        seen_assets.add(record.asset_id)
                        asset = await uow.assets.get_asset(workspace_id, record.asset_id)
                        if asset is not None:
                            digests.add(asset.sha256)
                            await self._standing(
                                workspace_id, asset.rights_decision_id, uow, rights
                            )
                    if record.rendition_id is not None and record.rendition_id not in (
                        seen_renditions
                    ):
                        seen_renditions.add(record.rendition_id)
                        rendition = await uow.renditions.get_rendition(
                            workspace_id, record.rendition_id
                        )
                        if rendition is not None:
                            digests.add(rendition.sha256)
                            # A rendition's asset may differ from the entry's,
                            # and its decision governs the rendition's text.
                            if rendition.asset_id not in seen_assets:
                                seen_assets.add(rendition.asset_id)
                                source = await uow.assets.get_asset(
                                    workspace_id, rendition.asset_id
                                )
                                if source is not None:
                                    digests.add(source.sha256)
                                    await self._standing(
                                        workspace_id, source.rights_decision_id, uow, rights
                                    )
        return rights, works, releases, digests

    async def _standing(
        self,
        workspace_id: UUID,
        decision_id: UUID | None,
        uow,
        rights: dict[UUID, RightsStanding],
    ) -> None:
        if decision_id is None or decision_id in rights:
            return
        decision = await uow.policies.get_rights_decision(workspace_id, decision_id)
        if decision is None:
            # Left out, which a reader reads as withheld. A decision the
            # producer cannot find is not one it can vouch for.
            return
        rights[decision_id] = RightsStanding(
            decision_id=decision.id,
            status=_value(decision.status),
            allowed_actions=tuple(sorted(_value(action) for action in decision.allowed_actions)),
            restrictions=tuple(decision.restrictions),
            restricted_to_group_ids=tuple(decision.restricted_to_group_ids),
            review_due_at=decision.review_due_at,
            decided_at=decision.decided_at,
            reason_code=decision.note,
        )

    async def _tombstones(self, workspace_id: UUID, digests: set[str]) -> list[TombstonedArtifact]:
        """Tombstones on payloads a reader may hold. Keyset-paged: every row."""
        found: dict[str, TombstonedArtifact] = {}
        async with self._context.uow_factory() as uow:
            async for record in iter_pages(
                lambda after, size: uow.objects.list_objects(
                    workspace_id, limit=size, after_id=after
                )
            ):
                if record.tombstoned and record.sha256 in digests:
                    found[record.sha256] = TombstonedArtifact(
                        sha256=record.sha256, reason=record.tombstone_reason
                    )
        return list(found.values())

    def _sign(self, checkpoint: PolicyCheckpoint) -> PolicyCheckpoint:
        if self._signer is None:
            return checkpoint
        statement = policy_statement_for(checkpoint)
        return checkpoint.model_copy(
            update={
                "signature": self._signer.sign(statement.to_bytes()),
                "key_identifier": self._signer.key_identifier,
            }
        )


class PolicyService:
    """Issues checkpoints and moves the policy head, exactly once per issue."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        control: ArchiveControlStore,
        builder: PolicyCheckpointBuilder,
        library_id: UUID,
        installation_id: UUID,
        writer_epoch: int = 0,
    ) -> None:
        self._context = context
        self._control = control
        self._builder = builder
        self._library_id = library_id
        self._installation_id = installation_id
        self._writer_epoch = writer_epoch

    # ── reading ────────────────────────────────────────────────────────────

    def _key(self, relative: str) -> str:
        return f"{library_prefix(self._library_id)}{relative}"

    async def read_head(self) -> tuple[PolicyHeadDescriptor | None, str | None]:
        """The policy head and its version token, or ``(None, None)``.

        A denial raises rather than reading as "no policy yet": a writer that
        concluded the library had no policy would start again at sequence 1
        over the top of one it simply could not see.
        """
        return await read_policy_head(self._control, self._library_id)

    async def current(self) -> tuple[PolicyHeadDescriptor | None, PolicyCheckpoint | None]:
        """The head and the checkpoint it names, as a reader would fetch them."""
        head, _version = await self.read_head()
        if head is None:
            return None, None
        return head, await self.fetch(head)

    async def fetch_bytes(self, head: PolicyHeadDescriptor) -> bytes:
        """The checkpoint a head names, as stored: what a reader fetches."""
        key = self._key(head.location)
        payload = await self._control.read(key)
        if payload is None:
            raise LabAssistantError(
                "the policy head names a checkpoint the archive does not hold",
                context={"key": key},
            ).with_class(ErrorClass.NOT_FOUND)
        return payload.data

    async def fetch(self, head: PolicyHeadDescriptor) -> PolicyCheckpoint:
        """The checkpoint a head names, checked against the digest it names.

        A key holding something other than what the head expected is refused
        here: the digest is part of the key, so a mismatch is a rewritten
        object, never a race.
        """
        key = self._key(head.location)
        data = await self.fetch_bytes(head)
        try:
            checkpoint = PolicyCheckpoint.model_validate(json.loads(data))
        except Exception as exc:
            raise LabAssistantError(
                "the current policy checkpoint does not parse",
                context={"key": key, "detail": str(exc)},
            ).with_class(ErrorClass.INVALID_CONTENT) from exc
        if checkpoint.signing_digest() != head.checkpoint_digest:
            raise LabAssistantError(
                "the policy checkpoint does not digest to what its head records",
                context={"key": key},
            ).with_class(ErrorClass.INVALID_CONTENT)
        return checkpoint

    # ── issuing ────────────────────────────────────────────────────────────

    async def issue(self, workspace_id: UUID, *, dry_run: bool = False) -> PolicyIssueReport:
        """Check both heads, build, upload, swap. In that order."""
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)

        head, version = await self.read_head()
        catalog_head, _ = await read_catalog_head(self._control, self._library_id)
        verdict = evaluate_policy_commit(
            head,
            catalog_head=catalog_head,
            library_id=self._library_id,
            owner_installation_id=self._installation_id,
            writer_epoch=self._writer_epoch,
        )
        if not verdict.permitted:
            return PolicyIssueReport(
                verdict=verdict,
                head=head,
                problems=(verdict.detail or "this writer may not issue policy",),
                next_steps=_refusal_steps(verdict),
            )

        checkpoint = await self._builder.build(
            workspace_id,
            policy_sequence=verdict.commit_sequence,
            parent_checkpoint_digest=head.checkpoint_digest if head else None,
        )
        changed, notes = await self._compare_with_predecessor(head, checkpoint)

        if dry_run:
            return PolicyIssueReport(
                verdict=verdict,
                checkpoint=checkpoint,
                head=head,
                changed=changed,
                next_steps=(*notes, "re-run with --apply to issue this checkpoint"),
            )

        uploaded = await self._upload(checkpoint)
        descriptor = PolicyHeadDescriptor(
            library_id=self._library_id,
            owner_installation_id=self._installation_id,
            writer_epoch=self._writer_epoch,
            policy_sequence=checkpoint.policy_sequence,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_digest=checkpoint.signing_digest(),
            updated_at=self._context.now(),
        )
        key = self._key(POLICY_HEAD_LOCATION)
        # Deliberately not retried. A failed swap means somebody else moved the
        # head, and adopting what it now says is how a fenced writer comes back.
        if head is None or version is None:
            await self._control.create(key, descriptor.serialise())
        else:
            await self._control.compare_and_swap(
                key, descriptor.serialise(), expected_version=version
            )

        return PolicyIssueReport(
            verdict=verdict,
            checkpoint=checkpoint,
            head=descriptor,
            published=True,
            changed=changed,
            uploaded=uploaded,
            next_steps=(
                *notes,
                f"policy sequence {checkpoint.policy_sequence} is current until "
                f"{checkpoint.valid_until.isoformat()}; issue again before then or readers "
                "stop serving restricted material",
            ),
        )

    async def _compare_with_predecessor(
        self, head: PolicyHeadDescriptor | None, checkpoint: PolicyCheckpoint
    ) -> tuple[bool, tuple[str, ...]]:
        """Did anything change since the checkpoint this one supersedes?

        A predecessor that is missing or will not parse does not stop the
        issue: superseding it is the repair. A denial or an outage still
        raises, because "I could not look" is not "there was nothing there".
        """
        if head is None:
            return True, ()
        try:
            previous = await self.fetch(head)
        except LabAssistantError as exc:
            if exc.error_class not in (ErrorClass.NOT_FOUND, ErrorClass.INVALID_CONTENT):
                raise
            return True, (
                f"the checkpoint the head named could not be read ({exc.detail}); this "
                "issue supersedes it",
            )
        return previous.content_digest() != checkpoint.content_digest(), ()

    async def _upload(self, checkpoint: PolicyCheckpoint) -> bool:
        """Create-only, under a key that contains the digest.

        A key that already exists holds this checkpoint — the digest is in the
        name — so a conflict is a resumed upload, not a collision.
        """
        try:
            await self._control.create(self._key(checkpoint.location), checkpoint.serialise())
        except LabAssistantError as exc:
            if exc.error_class is not ErrorClass.CONFLICT:
                raise
            return False
        return True


def _refusal_steps(verdict: CommitVerdict) -> tuple[str, ...]:
    if verdict.refusal is CommitRefusal.FENCED_BY_EPOCH:
        return (
            "this installation has been fenced by a handover. Stop issuing policy here.",
            "do NOT re-run with the epoch a head reported: that is the fenced writer "
            "taking the library back, and it would succeed",
        )
    if verdict.refusal is CommitRefusal.FOREIGN_OWNER:
        return (
            "another installation holds this library. Establish which is the publisher "
            "before either issues policy again",
        )
    return ("resolve the refusal before issuing again",)


def _value(item: Any) -> Any:
    return item.value if hasattr(item, "value") else item
