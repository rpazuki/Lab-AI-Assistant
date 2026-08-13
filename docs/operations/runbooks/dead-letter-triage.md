Status: active
Owner: operator + steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/tasks.py`

# Runbook: dead letter triage

**Symptom.** Tasks in `dead_letter`. They exhausted their retries; each one is audited,
so none of them failed invisibly.

## Triage by error class

| Error class | Meaning | Action |
|---|---|---|
| `source_unavailable`, `timeout` | the source was down for the whole retry window | retry after confirming the source is back |
| `rate_limited` | the budget is too tight for the run size | lower the rate, re-queue, then retry |
| `invalid_content` | the asset is not what its media type claimed | steward decision: re-acquire by another route, or mark unavailable |
| `schema_violation` | the extractor's output failed validation | a template or prompt defect. **Do not retry** — fix it, which changes the fingerprint |
| `identity_mismatch` | the acquired asset is a different work | steward decision; never retry blind |
| `provider_refusal` | the model declined | inspect the envelope; usually a prompt or a content problem |
| `internal` | our bug | read the trace id, open an issue, attach a support bundle |

List them first with `lab task queue --workspace <slug>`, which reports the dead
letters with their stage and error.

## Retrying

```bash
lab task retry --workspace <slug> --task <id>
```

Resets attempts and re-queues. It is audited and records who retried and why.

Retry in **small batches**, oldest first. Retrying two hundred rate-limited tasks at
once re-creates the condition that dead-lettered them.

## Do not

- **Do not retry a permanent class.** `policy_blocked`, `robots_blocked`,
  `malware_detected`, `licence_not_permitted` will fail identically forever. The
  taxonomy already made this decision.
- **Do not retry a `schema_violation` before fixing the cause.** The fingerprint is
  unchanged, so you are asking for the same failure.
- **Do not clear the dead letter queue to make a dashboard green.** It is the only
  record of what the corpus is missing, and a release built while ignoring it is a
  release with an unrecorded gap.
