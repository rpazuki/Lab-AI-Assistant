# Backup and restore

Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `docs/operations/backup-and-restore.md`

Three things must survive: the control plane (PostgreSQL), the object store, and
`.env`. Two of three is not a backup.

| File | What it does |
|---|---|
| `backup.sh` | base backup + object-store snapshot, both idempotent |
| `restore.sh` | restore to a point in time, then **verify before serving** |
| `postgresql.wal.conf` | the archiving settings point-in-time recovery needs |
| `crontab.example` | a schedule that meets the stated RPO |

## Rules these scripts encode

**`.env` is never written into a backup archive.** It lives in the institution's secret
manager. An archive containing the database password turns every off-site copy into a
credential leak.

**The object store is restored to a point at or after the database.** A row citing an
object the store does not have is a broken citation; an extra object is harmless.

**A restore is not finished until `lab db verify-restore` passes.** It checks the audit
hash chain, that every published release's manifest digest recomputes, that every cited
object is present, and that the alias points at something servable.

**Drill it twice a year** into a scratch VM, and write down the date, the duration, and
what went wrong. The first drill always finds something; better on a Tuesday afternoon.
