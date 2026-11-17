Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/tasks.py`, `scripts/status.sh`

# Monitoring

## What to watch

| Signal | Source | Healthy | Act when |
|---|---|---|---|
| API liveness | `/health` | 200 | fails twice in a row — the process is gone |
| API readiness | `/ready` | 200 | **503** — running, and not fit to serve |
| Queue depth per stage | `queue_snapshot` | steady or falling | rising for 30 minutes |
| Oldest ready task age | `queue_snapshot` | minutes | over an hour |
| Dead letters | task table | zero new | any new one |
| `awaiting_user` count | task table | any value | **never pages** — it is a work queue for humans |
| Lease expiries | task table | rare | repeated on one stage, meaning a worker is dying |
| Answer latency p95 | message rows | within the gate | above the gate for an hour |

`/health` answers whether the process is up and deliberately touches no
dependency: a database blip that made every container report unhealthy would
have an orchestrator restart all of them, which is the opposite of what that
situation needs. `/ready` answers whether this process should receive a request
and returns **503** when it should not — until 2026-11 it returned 200 with
`"status": "degraded"` in the body, so every tool that reads the status code
kept routing to a process that had just said it could not reach its database.

A not-ready body names the failing check and why, so an alert can carry the
reason instead of sending someone to the logs for it.

The checks differ by installation role, so the same alert means different things
on the two deployments:

| Check | Publisher | Reader |
|---|---|---|
| `object_store` | lists the bucket | fetches the manifest of the release it serves |
| `corpus` | advisory — no release yet is a normal state while one is being built | **blocks** — nothing servable means nothing to answer with |

A reader is probed with a GET rather than a LIST because the credential it ought
to have cannot list: scope a reader to the objects it serves and a LIST-based
probe reports it unfit while every object it needs is readable.

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
