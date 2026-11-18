Status: active
Owner: operator + data steward
Last verified: 2026-11-18
Source of truth: `packages/python/lab_assistant/application/archive_retention.py`

# Runbook: purging the shared archive

**When.** The archive holds snapshots nobody needs, or bytes a takedown requires to go.
Design: [ADR-0028](../../architecture/adr/0028-archive-retention.md).

## Plan

```bash
lab archive retention plan --workspace <slug> --keep-last 3 --out plan.json
```

Deletes nothing. `retained` says why each snapshot stays, `deletions` lists every key
with its reason, and `kept:` lines on stderr say what stays although something asked for
its removal — a tombstoned payload an old snapshot still names, a cited release in no
snapshot. Hold anything under dispute with `--hold-snapshot <id>` or
`--hold-digest <sha256>`.

## Review, then apply

Have the data steward read `plan.json`. Then:

```bash
lab archive retention apply --workspace <slug> plan.json
```

It recomputes the plan and refuses if the archive changed since — plan again and review
that. On S3 or GCS it refuses outright: delete with the bucket's own retention tooling,
using the reviewed plan as the list.

## Do not

- **Do not delete archive keys by hand.** A payload one snapshot drops is often one
  another still names.
- **Do not edit a plan to add keys.** Its digest will not match, and that is the point.
- **Do not purge to fix a rights problem.** Withhold first, with a policy checkpoint;
  purging is the last step, after nothing retains the bytes.
