# evaluation worker

Runs the `evaluation` stage on the **compute**-bound queue.

```bash
lab worker compute          # same loop this container runs
lab worker compute --once   # claim one task and exit, for debugging
```

- **Queue**: durable task rows in PostgreSQL; no broker.
- **Concurrency**: capped per stage by `LAB_STAGE_CONCURRENCY`, not by replica
  count. Scaling replicas adds resilience, not request rate.
- **Leases**: a killed worker's task becomes claimable again when its lease
  expires; the retry is idempotent because the handler's fingerprint is stable.
- **Human waits**: a task in `awaiting_user` holds no lease and is not a failure.

Operational runbook: [`docs/operations/runbooks/stuck-task.md`](../../docs/operations/runbooks/stuck-task.md).
