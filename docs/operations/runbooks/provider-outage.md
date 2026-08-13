Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/adapters/models/`

# Runbook: provider outage

**Symptom.** Answers fail or hang; or extraction tasks accumulate `provider_error` and
`rate_limited`.

## Distinguish the two failure surfaces

**Answer generation is on the request path.** Users see it immediately. Retrieval still
works: the search surface returns evidence without generation, and that is the
stop-gap to tell people about.

**Extraction is queued.** It degrades invisibly and recovers on its own. Do not treat a
growing extraction queue as an outage in its first hour.

## Check

```bash
./scripts/logs.sh api --since 15m --grep provider
```

- sustained 429 → the budget is too tight, or a bulk extraction run is competing with
  interactive questions. Lower the extraction stage's cap first; interactive answers
  matter more.
- 5xx or timeouts → the provider. Check its status page. Retries with backoff are
  already running.
- `provider_refusal` → not an outage. Inspect the stored envelope.

## Do

- reduce the extraction concurrency cap so interactive answers keep working;
- if a local model is configured, switch generation to it — answers get worse, and
  members should be told they are working from a fallback;
- otherwise say plainly that answers are unavailable and search still works.

## Do not

- **Do not raise retry counts.** Longer retries against a failing provider hold leases
  and starve the queue.
- **Do not disable the policy check to try a different provider.** The transfer decision
  is per provider. A provider not in the policy is not an approved recipient of licensed
  content, however convenient it is at 5pm.
- **Do not clear the queue.** It drains when the provider returns.
