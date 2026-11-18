Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: the runbooks themselves

# Runbooks

One page per situation. Each states the symptom, what to check, what to do, and what
**not** to do — the last section is usually the one that saves the incident.

| Runbook | When |
|---|---|
| [Stuck task](stuck-task.md) | an item is not progressing |
| [Dead letter triage](dead-letter-triage.md) | tasks have exhausted their retries |
| [Release rollback](release-rollback.md) | a published release is wrong |
| [Provider outage](provider-outage.md) | the model or a source is failing |
| [Malware detected](malware-detected.md) | a scan quarantined an asset |
| [Disk pressure](disk-pressure.md) | the VM is running out of space |
| [Audit chain failure](audit-chain-failure.md) | chain verification does not pass |
| [Suspected access breach](suspected-access-breach.md) | data may have crossed a boundary |
| [Policy checkpoints](policy-checkpoints.md) | a revocation must reach readers, or policy is going stale |
| [Fetching a snapshot](archive-fetch.md) | a reader is taking a library from the archive |

## Rules for all of them

**Capture before you change.** Take a support bundle and a queue snapshot first. Most
diagnostic evidence is destroyed by the first fix attempt.

**Never query across a workspace boundary to investigate one.** It writes an audit event
indistinguishable from the thing you are investigating.

**Never edit the database by hand.** Every state change has a command that audits it. A
manual `UPDATE` leaves an unexplained state and a hash chain that still verifies,
which is worse than the original problem.

**Health is not correctness.** A green `/ready` says the process is up, not that the
release is right.
