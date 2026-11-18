Status: active
Owner: operator
Last verified: 2026-11-18
Source of truth: `packages/python/lab_assistant/application/handover.py`

# Runbook: moving the publisher to another installation

**When.** The publisher VM is being replaced, or has died, and another installation
must take over publishing a library. Design:
[ADR-0027](../../architecture/adr/0027-publisher-handover.md).

## Planned handover (the old publisher is reachable)

1. On the **old** publisher, set `LAB_INSTALLATION_ROLE=maintenance` and restart. It
   stops serving and stops claiming work.
2. Let leased tasks finish (`lab task queue --workspace <slug>`) and reconcile
   publications (`lab archive reconcile --workspace <slug> --apply`).
3. Drain, and keep the statement:

```bash
lab installation drain --workspace <slug> --statement handover.json
```

4. Take a backup (`lab backup create`) and restore it on the **new** installation as a
   clone, with its own `LAB_INSTALLATION_ID` and `LAB_WRITER_EPOCH` one higher.
5. On the new installation, check, then apply:

```bash
lab installation promote --workspace <slug> --statement handover.json
```

```bash
lab installation promote --workspace <slug> --statement handover.json --apply
```

6. Revoke the old publisher's archive credentials, and restart it as a reader or not
   at all. `lab installation status` on it should report `"fenced": true`.

## The old publisher is gone

Stop it for good and revoke its archive credentials first. Then promote without a
statement, asserting that:

```bash
lab installation promote --workspace <slug> --origin-fenced --apply
```

Anything it published after your last backup is not in the new database. The command
refuses unless that database holds every release the archive last published; if you
accept losing the difference, add `--accept-loss`.

## Do not

- **Do not promote on a hunch that the old publisher is down.** If it wakes up
  unfenced, two installations hold credentials to one library.
- **Do not set the new installation's epoch to whatever a head reports.** Exactly one
  higher than the drained publisher's, or the promotion refuses — correctly.
- **Do not reuse the old installation's id for a clone.** A replacement on the same
  identity is a `restore --intent replace` of a fenced origin, not a promotion.
