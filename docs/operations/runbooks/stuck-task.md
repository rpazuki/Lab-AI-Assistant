Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/tasks.py`

# Runbook: stuck task

**Symptom.** An item is not progressing, or a stage's queue depth is not falling.

## Check, in order

```bash
./scripts/status.sh
```

1. **What state is it actually in?**

```bash
lab task inspect --workspace <slug> --task <id>
```

   It reports stage, state, attempt, error class, lease owner, lease expiry, the
   dependencies it waits on, and whether the state counts as a failure at all.
2. **`awaiting_user`?** Then it is not stuck. Someone must upload an asset or resolve an
   identity conflict. Tell the steward; do not retry it.
3. **`blocked`?** Look at the dependency. The dependency is the incident.
4. **`leased` with an expired lease?** A worker died. The lease reclaims automatically
   on the next claim cycle; if leases keep expiring on one stage, that worker is
   crashing — read its logs before restarting it.
5. **`ready` but never claimed?** The stage's concurrency cap is fully used by
   long-running tasks, or no worker is running for that stage. Check container states
   and `lab task queue --workspace <slug>`.
6. **Repeated `rate_limited`?** Expected during a large run. Confirm the stored
   `Retry-After` is being honoured before touching anything.

## Do

- restart the worker whose logs show the crash:

```bash
./scripts/restart.sh worker-network
```

- if one task is genuinely poisoned, cancel it. It is audited, and it does not remove
  the record:

```bash
lab task cancel --workspace <slug> --task <id> --reason poisoned_input
```

## Do not

- **Do not delete the task row.** The item then has no record of why it stopped, and
  re-submitting produces the same fingerprint and the same failure with no history.
- **Do not raise the concurrency cap to clear a backlog.** The cap protects the source
  and the VM. A backlog clears; a banned API key does not.
- **Do not restart the whole stack.** It reclaims every lease at once, and every
  in-flight task restarts from its last durable point — turning one stuck task into a
  hundred re-runs.
