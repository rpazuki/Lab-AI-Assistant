Status: active
Owner: security owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/provenance/audit.py`

# Runbook: audit chain failure

**Symptom.** `lab db verify-audit --workspace <slug>` reports a break: a row's hash
does not match its predecessor.

**Treat this as a security incident until proven otherwise.** The chain exists so that a
deleted or reordered audit row is detectable. A break means either tampering, or a bug
in something that had no business writing to that table.

## Capture first

1. record the break: workspace, the last verifying sequence number, the first failing
   row's id and timestamp.
2. take a support bundle and a database backup **now**, before any remediation.
3. tell the security owner. Do not "have a quick look" across workspaces.

## Then determine which it is

**Restore artefact.** A partial restore can truncate the chain at the recovery point.
Check whether the break coincides with a restore. A clean truncation at the tail is
consistent with recovery; a break in the middle is not.

**Bug.** On PostgreSQL a trigger refuses updates and deletes on the audit table, so a
mid-chain break is not something the application can do through its normal path. Check
migration history and any manual SQL run on that VM.

**Tampering.** If neither of the above explains it, follow
[suspected access breach](suspected-access-breach.md).

## Do not

- **Do not re-chain the rows.** Recomputing hashes so verification passes destroys the
  only evidence and produces a chain that verifies while being false.
- **Do not delete the failing row.**
- **Do not resume normal operation before the break is explained in writing.** An audit
  trail nobody trusts cannot support a published release.
