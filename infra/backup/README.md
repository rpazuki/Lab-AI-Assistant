# Backup and restore

Status: active
Owner: operator
Last verified: 2026-11-17
Source of truth: `docs/operations/backup-and-restore.md`

Three things must survive: the control plane (PostgreSQL), the object store, and
`.env`. Two of three is not a backup.

| File | What it does |
|---|---|
| `backup.sh` | the portable backup (`lab backup create`) and the physical one, both idempotent |
| `restore.sh` | portable restore by default, `--physical` for point-in-time, then **verify before serving** |
| `lib/volumes.sh` | resolves Compose volume names from the host; refuses to guess |
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

**A restore establishes compatibility before it touches anything.** `lab backup restore`
without `--apply` compares the backup's manifest against this destination — app version,
Alembic revision, PostgreSQL major, extension versions — and refuses when they disagree.
Every one of those was an assumption in the script this replaces, and all but one of them
fails after the restore, during serving.

**Restoring does not confer the right to publish.** The default `--intent clone` runs the
restored installation under a new identity and withholds publishing. `--intent replace`
takes the original's identity and needs `--origin-is-fenced`: two publishers on one
library do not fail, they both work and diverge.

**Volume names are resolved, never guessed.** `lib/volumes.sh` fails when no volume
matches, when more than one does, or when the target is not empty. The old script
hardcoded `lab-assistant_pgdata` and removed its contents.

**Drill it twice a year** into a scratch VM, and write down the date, the duration, and
what went wrong. The first drill always finds something; better on a Tuesday afternoon.
