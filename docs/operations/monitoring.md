Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/tasks.py`, `scripts/status.sh`

# Monitoring

## What to watch

| Signal | Source | Healthy | Act when |
|---|---|---|---|
| API health and readiness | `/health`, `/ready` | both 200 | either fails twice in a row |
| Queue depth per stage | `queue_snapshot` | steady or falling | rising for 30 minutes |
| Oldest ready task age | `queue_snapshot` | minutes | over an hour |
| Dead letters | task table | zero new | any new one |
| `awaiting_user` count | task table | any value | **never pages** — it is a work queue for humans |
| Lease expiries | task table | rare | repeated on one stage, meaning a worker is dying |
| Answer latency p95 | message rows | within the gate | above the gate for an hour |
| Unsupported-handle rate | message rows | near zero | any sustained rise — a generation regression |
| Abstention rate | message rows | stable | a jump usually means retrieval or a release, not the model |
| Provider 429s | logs | occasional | sustained; raise the delay or the budget |
| Disk free | `status.sh` | over 25% | under 15%. Objects and pgdata share the disk. |
| Audit chain | `lab db verify-audit --workspace <slug>` | verifies | **immediately**, as a possible incident |

```bash
./scripts/status.sh
```

Reports the deployed commit, container states, health, queue depth per stage, and disk.

## Why `awaiting_user` must never alert

It is the normal state of assisted acquisition. Paging on it trains the operator to
ignore the alert that matters. It belongs on the steward's dashboard, not in the
on-call rotation.

## Logs

Structured JSON, one line per event, with `trace_id`, `workspace_id`, actor id, stage,
task id, and error class. Redaction happens at emit — a token cannot be in a log file
even briefly, because that file gets shared in screenshots.

```bash
./scripts/logs.sh api --since 30m --grep error
```

## Correlation

A `trace_id` flows from the browser request through the API, into every task it
creates, and into each task's audit rows. One id answers "what happened to this
request" without joining timestamps by eye.

## What is deliberately absent

No Prometheus, Grafana, or tracing backend in the supported profile. One VM, one
operator: `status.sh` plus rotated JSON logs is proportionate. `infra/monitoring/`
holds the scrape configuration for when that stops being true.
