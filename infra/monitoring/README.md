# Monitoring

Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `docs/operations/monitoring.md`

The supported profile is one VM with one operator, where `./scripts/status.sh` plus
rotated JSON logs is proportionate. Nothing here runs by default.

This directory holds what is needed **when that stops being true** — a second VM, a
second operator, or an availability commitment:

| File | Purpose |
|---|---|
| `prometheus.yml` | scrape configuration for the API and the queue exporter |
| `alerts.yml` | the alert rules that follow from `docs/operations/monitoring.md` |
| `queue-exporter.sql` | the query behind the queue metrics, so the numbers are auditable |

## Turning it on

Add a `prometheus` service to a local compose overlay, mount `prometheus.yml`, and
point it at the API. Do **not** publish the metrics port outside the compose network:
queue depth and workspace ids are internal.

## The one alerting rule that matters

`awaiting_user` never pages. It is the normal state of assisted acquisition, and paging
on it trains the operator to ignore the alert that does matter. It belongs on the
steward's dashboard.
