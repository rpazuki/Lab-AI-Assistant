Status: active
Owner: steward + operator
Last verified: 2026-11-18
Source of truth: `packages/python/lab_assistant/application/archive_policy.py`

# Runbook: policy checkpoints

**When.** A rights decision was revoked, a release withdrawn or a payload taken down,
and readers of the shared archive must stop serving it; or `lab archive policy status`
is failing because the current checkpoint is going stale. Design:
[ADR-0025](../../architecture/adr/0025-policy-checkpoints.md).

Readers do not enforce checkpoints until phase 3 of the shared-archive plan. Issue them
now anyway: a reader built later applies the latest one, and a library whose policy
history starts late has nothing to say about what happened before it.

## Check

```bash
lab archive policy status --workspace <slug>
```

Prints the policy head and the checkpoint it names, checked as a reader would check
it: digest, signature against `LAB_ARCHIVE_TRUSTED_KEYS`, sequence, freshness. Exits
non-zero unless the checkpoint is current, which is what a monitor should alert on.

## Issue

```bash
lab archive policy publish --workspace <slug>            # build and report, change nothing
lab archive policy publish --workspace <slug> --apply    # issue and move the policy head
```

After a revocation, issue at once: that is how readers hear of it without a new
snapshot. Otherwise issue on a timer well inside `LAB_ARCHIVE_POLICY_VALIDITY_HOURS`
(24 by default). Re-issuing unchanged policy is normal — the report says
`"changed": false` — and it is what keeps readers serving restricted material.

`lab archive package` records the current policy head in the snapshot it writes, so
issue first and package second when both are due.

## A widening

Lifting an embargo, or renewing a review date, does **not** reach readers through a
checkpoint: a checkpoint can only narrow what a snapshot granted. Package and publish
a new snapshot. Do it before an old review date passes, or readers withhold the
material on that date.

## Do not

- **Do not re-run with the epoch a refusal reported.** A `fenced_by_epoch` refusal
  means a handover moved the right to publish elsewhere, and adopting the epoch is how
  the old publisher takes the library back. It would succeed.
- **Do not edit a checkpoint in the bucket.** Its name is its digest and it is signed;
  readers refuse an edited one. A correction is the next issue.
- **Do not delete old checkpoints.** A snapshot names the checkpoint it was built
  under, and a reader fetches exactly that one.
- **Do not lengthen the validity window to quiet an alert.** The window bounds how long
  a revocation can go unseen; fix whatever stopped the re-issue instead.
