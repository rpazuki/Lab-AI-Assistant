Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `infra/backup/`

# Backup and restore

Three things must survive: the control plane (PostgreSQL), the object store, and `.env`.
Lose any one and the other two are not enough.

| Data | Method | Frequency | Target |
|---|---|---|---|
| PostgreSQL | base backup + WAL archiving (point-in-time recovery) | continuous | RPO 15 min |
| Object store | versioned bucket, or `rsync` snapshot for a filesystem store | daily | RPO 24 h |
| `.env` | the institution's secret manager. **Not in a backup archive.** | on change | – |
| Model cache | none | – | re-downloadable |

Recovery target: **RTO 4 hours**, on the assumption that the VM must be rebuilt.

## Why objects and the database must be restored to compatible points

A database row citing an object digest that the restored store does not contain is a
broken citation. Restore the object store to a point **at or after** the database's
recovery point: an extra object is harmless, a missing one is not.

For the shared VM, the [installation guide](../fresh_install_linux_redhat.md#local-storage-and-backups)
provides a stopped-writer database dump and objects archive procedure. Copy both
off the VM; backing up PostgreSQL alone does not preserve document bytes.

## Restore procedure

```bash
docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml down
```

1. restore `pgdata` from the base backup and replay WAL to the chosen point;
2. restore the object store to the same point or later;
3. put `.env` back from the secret manager — never from the archive;
4. bring up `db`, run `alembic upgrade head` (a no-op if the backup was current);
5. **verify** before serving:

```bash
docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml run --rm api lab db verify-restore --workspace <slug>
```

`verify-restore` checks the audit hash chain per workspace, that every published
release's manifest digest recomputes, that every referenced object exists and matches
its digest, and that the alias points at a servable release. **A restore that has not
been verified is not a restore.**

6. bring up the API, workers, and web; check `/ready` and `status.sh`.

## Drill it

Restore into a scratch VM twice a year and record the date, the duration, and what
went wrong. A backup nobody has restored is a hypothesis. The verification step exists
because the first drill will fail on something, and it is better for that to be a
Tuesday afternoon.

## What is not backed up

Rotated container logs, support bundles, and the model cache — all reconstructible or
disposable.
