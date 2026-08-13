Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `scripts/collect-logs.sh`, `scripts/lib/redact.sh`

# Support bundles

```bash
./scripts/collect-logs.sh --since 2h --out ~/reports
```

Produces a redacted `.tar.gz` for a bug report.

## Contents

Container logs for the window, container states and images, the deployed commit,
migration head, `lab config print` output (secrets as digests), a queue snapshot, disk
and memory, and the compose files in force.

## Never in a bundle

Secrets, tokens, cookies, database rows, document content, evidence quotes, prompts,
model responses, or member email addresses. A bundle is handed to someone outside the
deployment; the data plane is not part of a bug report.

## The bundle verifies its own redaction

`collect-logs.sh` redacts, then greps the result for anything still secret-shaped, and
**deletes the bundle and fails** if it finds something. "We redacted it" is a claim; a
verification pass is a control. Patterns live in `scripts/lib/redact.sh` and apply to
the live log view too — an operator sharing a screenshot is the most common way a token
escapes.

## Handing one over

State the window, the deployed commit, and what you were doing. If you believe content
is needed to diagnose the problem, say which record — do not attach it. Ask the data
steward whether it may be shared, and share it separately from the bundle.

## Bundles are not backups

Nothing in a bundle can restore the system. See
[backup and restore](backup-and-restore.md).
