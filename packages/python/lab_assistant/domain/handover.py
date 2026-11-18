"""Who may take over a library's right to publish, and when (phase 4).

The architecture proposal's rule, reduced to code: **one writer, and a change
of writer is a deliberate, fenced operator action — never a heartbeat timing
out.** Two publishers on one library do not fail; they both work, and diverge.
So promotion refuses in every case where it cannot establish that the old
writer has stopped and that nothing it published is about to be lost.

A promotion rewrites both heads — catalog and policy — to the new owner at the
next epoch. The two cannot be rewritten atomically, so the rules here make a
half-finished promotion resumable and harmless: a head already at the new owner
and epoch is *done*, not foreign, and the cross-checks in
`domain/publication.authority_refusal` already fence the old writer from both
heads the moment either has moved.

What a promotion checks, in order:

- **the statement is about this library**, and the epoch moves by exactly one:
  skipping epochs makes the record of handovers unreadable, and a "promotion"
  to the same epoch is two writers;
- **each head is either untouched since the drain or already transferred.** A
  head that moved after the old publisher drained means it kept publishing, and
  whatever it published is not in the state the new one recovered;
- **the new publisher holds the published state**, or the operator has accepted
  the loss explicitly. A publisher that cannot see what the library published
  would curate from an older world and publish over it;
- **an unreachable old publisher was asserted fenced.** Without a statement
  there is nothing from the old writer at all, and only an operator who has
  revoked its credentials and stopped its machine can say it will not wake up
  (scenario S53: promotion is blocked otherwise).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from lab_assistant.contracts.handover import HandoverStatement

__all__ = [
    "DrainBlocker",
    "HandoverRefusal",
    "HandoverVerdict",
    "HeadPlan",
    "drain_blockers",
    "evaluate_promotion",
]


@dataclass(frozen=True, slots=True)
class DrainBlocker:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def drain_blockers(
    *,
    role_is_maintenance: bool,
    owns_heads: bool,
    open_publications: int,
    leased_tasks: int,
) -> tuple[DrainBlocker, ...]:
    """Why this publisher may not sign off yet.

    Each blocker is work that would outlive the publisher's authority: an
    interrupted publication would commit after the handover, a leased task
    would finish — and perhaps pay a provider — on a machine that no longer
    owns the library.
    """
    blockers: list[DrainBlocker] = []
    if not role_is_maintenance:
        blockers.append(
            DrainBlocker(
                "not_in_maintenance",
                "this installation still serves and claims work; restart it with "
                "LAB_INSTALLATION_ROLE=maintenance before draining",
            )
        )
    if not owns_heads:
        blockers.append(
            DrainBlocker(
                "not_the_publisher",
                "the library's heads name another installation or epoch: there is nothing "
                "here to hand over",
            )
        )
    if open_publications:
        blockers.append(
            DrainBlocker(
                "publication_in_flight",
                f"{open_publications} publication attempt(s) are not terminal; run "
                "`lab archive reconcile --apply` so each one is known before the handover",
            )
        )
    if leased_tasks:
        blockers.append(
            DrainBlocker(
                "work_in_flight",
                f"{leased_tasks} task(s) are leased; let them finish or cancel them — work "
                "running after the handover runs without authority",
            )
        )
    return tuple(blockers)


class HandoverRefusal(StrEnum):
    WRONG_LIBRARY = "wrong_library"
    EPOCH_NOT_NEXT = "epoch_not_next"
    #: A head moved after the drain: the old publisher kept publishing.
    HEAD_MOVED_SINCE_DRAIN = "head_moved_since_drain"
    #: A head names neither the old writer nor the new one.
    FOREIGN_OWNER = "foreign_owner"
    #: The new publisher does not hold what the library published.
    STATE_NOT_RECOVERED = "state_not_recovered"
    #: No statement from the old publisher, and nobody asserted it is fenced.
    NOT_FENCED = "not_fenced"


class HeadPlan(StrEnum):
    """What a promotion does to one head."""

    TRANSFER = "transfer"
    #: Already at the new owner and epoch: a promotion that crashed between the
    #: two heads, resumed.
    ALREADY_TRANSFERRED = "already_transferred"
    #: Nothing published yet; nothing to transfer.
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class HandoverVerdict:
    permitted: bool
    catalog: HeadPlan | None = None
    policy: HeadPlan | None = None
    refusal: HandoverRefusal | None = None
    detail: str | None = None

    @property
    def complete(self) -> bool:
        """Nothing left to write: both heads are where the promotion puts them."""
        return self.permitted and all(
            plan in (HeadPlan.ALREADY_TRANSFERRED, HeadPlan.ABSENT)
            for plan in (self.catalog, self.policy)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "permitted": self.permitted,
            "catalog": self.catalog.value if self.catalog else None,
            "policy": self.policy.value if self.policy else None,
            "refusal": self.refusal.value if self.refusal else None,
            "detail": self.detail,
        }


def evaluate_promotion(
    statement: HandoverStatement | None,
    *,
    catalog_head,
    policy_head,
    library_id: UUID,
    to_installation_id: UUID,
    to_epoch: int,
    state_recovered: bool,
    accept_loss: bool = False,
) -> HandoverVerdict:
    """May this installation take the library over, and what does it write?"""

    def refuse(refusal: HandoverRefusal, detail: str) -> HandoverVerdict:
        return HandoverVerdict(permitted=False, refusal=refusal, detail=detail)

    if statement is None:
        return refuse(
            HandoverRefusal.NOT_FENCED,
            "no drain statement from the old publisher, and nobody asserted it is fenced. "
            "Promoting now could leave two writers; fence it — stop it, revoke its archive "
            "credentials — and assert that explicitly",
        )
    if statement.library_id != library_id:
        return refuse(
            HandoverRefusal.WRONG_LIBRARY,
            f"the statement is about library {statement.library_id}, not {library_id}",
        )
    if to_epoch != statement.writer_epoch + 1:
        return refuse(
            HandoverRefusal.EPOCH_NOT_NEXT,
            f"the old publisher held epoch {statement.writer_epoch}; the successor must hold "
            f"exactly {statement.writer_epoch + 1}, and this one is configured for {to_epoch}. "
            "Set LAB_WRITER_EPOCH",
        )

    plans: list[HeadPlan] = []
    for name, head, sequence, expected in (
        (
            "catalog",
            catalog_head,
            getattr(catalog_head, "commit_sequence", None),
            statement.catalog_commit_sequence,
        ),
        (
            "policy",
            policy_head,
            getattr(policy_head, "policy_sequence", None),
            statement.policy_sequence,
        ),
    ):
        if head is None:
            if expected is not None:
                return refuse(
                    HandoverRefusal.HEAD_MOVED_SINCE_DRAIN,
                    f"the drain saw a {name} head at sequence {expected} and there is none: "
                    "the archive is not the one the old publisher drained against",
                )
            plans.append(HeadPlan.ABSENT)
            continue
        if head.library_id != library_id:
            return refuse(
                HandoverRefusal.WRONG_LIBRARY,
                f"the {name} head belongs to library {head.library_id}",
            )
        if head.owner_installation_id == to_installation_id and head.writer_epoch == to_epoch:
            plans.append(HeadPlan.ALREADY_TRANSFERRED)
            continue
        if (
            head.owner_installation_id != statement.from_installation_id
            or head.writer_epoch != statement.writer_epoch
        ):
            return refuse(
                HandoverRefusal.FOREIGN_OWNER,
                f"the {name} head names installation {head.owner_installation_id} at epoch "
                f"{head.writer_epoch}, which is neither the drained publisher nor this one",
            )
        if sequence != expected:
            return refuse(
                HandoverRefusal.HEAD_MOVED_SINCE_DRAIN,
                f"the {name} head is at sequence {sequence} and the drain recorded {expected}: "
                "the old publisher published after it drained. Drain it again",
            )
        plans.append(HeadPlan.TRANSFER)

    if not state_recovered and not accept_loss:
        return refuse(
            HandoverRefusal.STATE_NOT_RECOVERED,
            "this installation does not hold the releases the library last published. "
            "Restore the old publisher's backup first, or accept the loss explicitly — a "
            "publisher that cannot see what was published curates over it",
        )

    return HandoverVerdict(permitted=True, catalog=plans[0], policy=plans[1])
