Status: proposed
Owner: technical owner + operator
Last verified: 2026-11-18
Applies to: moving the right to publish a library (phase 4)

# ADR-0027: The right to publish moves by a drained, fenced, operator-run handover

## Context

A library has one writer, enforced by owner and epoch on its two heads
([ADR-0025](0025-policy-checkpoints.md) made it two). Restoring a backup produces a
clone that may not publish (`domain/recovery.py`), and nothing so far could make a
clone the publisher. The architecture proposal is explicit about how that should
happen: operator-driven, with the old writer drained and fenced first, never on a
heartbeat timing out, and blocked when the old writer cannot be fenced.

## Decision

Three commands, each safe to repeat.

**`lab installation drain`** runs on the old publisher after it is restarted with
`LAB_INSTALLATION_ROLE=maintenance` — a new role with no capabilities, which serves
nobody (readiness answers 503) and refuses to start workers. It refuses while a
publication attempt is not terminal or a task is leased, and otherwise writes a
*statement*: the library, its installation and epoch, and the catalog and policy
sequences its heads carried.

**`lab installation promote`** runs on the successor, configured with its own
installation id and exactly the next epoch. It checks the statement against both
heads — each must be untouched since the drain, or already transferred — and against
its own database, which must hold every release the published manifest names unless
`--accept-loss` is given. With `--apply` it rewrites the catalog head and then the
policy head to itself, each a compare-and-swap, recording the predecessor. A crash
between the two is resumed by running it again; the old writer is fenced from both
heads as soon as either moves, by the cross-checks of ADR-0025.

Without a statement — the old publisher is unreachable — promotion is refused unless
the operator passes `--origin-fenced`, asserting they have stopped it and revoked its
archive credentials. The statement is then built from the heads and marked as asserted.

**`lab installation status`** reports who the heads name and whether this
installation has been fenced. Drain and promotion each write an audit row.

## Consequences

- No automatic failover. A dead publisher stays dead until an operator acts, and
  readers keep serving what was committed meanwhile.
- Fencing the old writer's *credentials* is outside the software. The heads refuse
  its publications; only revoking its archive identity stops it writing litter, and
  only stopping its machine stops paid provider calls. `--origin-fenced` is the
  operator's word that they did both.
- Leased tasks inherited in the successor's restored database are reported and left
  to expire into the queue's own reclaim, rather than cancelled.
- Proven offline, between installations sharing a test archive. Moving authority
  between two real VMs, with real IAM revocation, is the deployment-time acceptance.

## Alternatives considered

**Automatic promotion on a missed heartbeat.** Rejected by the architecture: an
atomic head fences publication, not every side effect, and a publisher that was only
slow would become a second writer.

**Promotion that adopts whatever epoch the heads carry.** Rejected: that is the fenced
writer taking the library back, the one thing epochs exist to prevent.

## Revisit when

- the lab needs failover faster than an operator can act, which needs an independently
  enforced lease rather than this;
- a real deployment's IAM can be revoked from the same command.
