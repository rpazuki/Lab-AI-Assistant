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
release's manifest digest recomputes and parses, that the alias points at a servable
release, and — through the same sweep `lab storage scan` runs — that every row
pointing at an object has one, and that every registered object's bytes are present
and digest to what was recorded. **A restore that has not been verified is not a
restore.**

Everything pages with a keyset cursor. Until 2026-11 this read the newest thousand
releases and checked one object per release, then printed `restore verified`; the
sentence above described what it was supposed to do rather than what it did.

To sweep storage without the release and audit checks — or more cheaply, during
routine operation rather than after a restore:

```bash
lab storage scan --workspace <slug> --verify exists    # metadata only, cheap
lab storage scan --workspace <slug> --verify digest    # reads every object
```

`--verify digest` reads every byte in the store, so it is a maintenance-window
operation on a large corpus. `--verify exists` still catches a restore that brought
the database and not the objects, which is the common failure. Add `--skip-orphans`
to stop it holding referenced ids in memory; that is the only part of the sweep whose
cost grows with the corpus.

Findings that block serving are separated from findings that do not. A payload that
is missing, denied, corrupt or still being restored from a cold class blocks. A
registered object nothing references — usually a transaction that rolled back after
its upload — is reported and does not. Reclaiming those is a separate, privileged
decision that has to account for retention and legal holds.

## Moving the object store

Changing `LAB_OBJECT_STORE_KIND` points the application at an empty bucket. The
evidence stays where it was, every citation stops resolving, and the next write goes
somewhere with no history. The configuration change is the *last* step, not the
operation.

The destination is described by `LAB_MIGRATE_OBJECT_STORE_*`, mirroring
`LAB_OBJECT_STORE_*`, so both stores are configured at once and credentials stay in
the environment rather than on a command line that lands in shell history.

```bash
LAB_MIGRATE_OBJECT_STORE_KIND=s3 LAB_MIGRATE_OBJECT_STORE_BUCKET=... lab storage migrate
```

That inventories both sides and reports the plan without writing. Then:

```bash
lab storage migrate --apply
```

What it does, and what it deliberately does not:

- Copies every object the registry knows, across every workspace and including the
  shared objects that belong to none — a per-workspace sweep would leave those behind
  every time.
- Preserves identity by construction rather than by copying: an object's id is
  derived from its workspace and digest, so the destination computes the same id from
  the same bytes. There is no mapping table to get wrong.
- Carries the original creation time. Evidence restamped with the hour it was moved
  has lost something real.
- Verifies each copy. `--verify digest` reads the destination back and recomputes,
  and is the default because it is the level that catches a truncated transfer.
  `--verify metadata` is the cheap pass.
- Journals progress, so an interrupted run resumes. The journal holds object ids and
  counts — no credentials, no signed URLs — and **belongs in the backup** while a
  migration is in flight.
- Carries payloads the database does not describe, usually uploads whose transaction
  rolled back. `--skip-source-only` leaves them, as a decision rather than an
  omission.
- Does **not** copy tombstoned payloads. The denial is recorded in the database,
  which a migration does not move, so access stays denied either way; copying
  withdrawn material into a new store would be the wrong default. Their bytes stay in
  the old store, and the report names them.
- Does **not** switch the application over, and does **not** make the old store
  read-only. Both are deliberate operator steps, in that order, after a person has
  read the verification.

Re-running after a successful migration copies nothing and verifies everything, which
is the acceptance pass to make before cutover:

```bash
lab storage migrate --apply --verify digest
```

Then point `LAB_OBJECT_STORE_*` at the new store for the API and every worker
together, run `lab db verify-restore` against it, and check search, a citation and a
download before enabling writes. Keep the old store read-only until acceptance and
your retention requirements are both satisfied: once new writes land on the new store,
going back needs a reverse delta, not a configuration change.

If a sweep reports dangling references, `lab storage backfill --workspace <slug>`
reports what it could rebuild from the store's own metadata, and `--apply` writes the
rows. Note that PostgreSQL's foreign keys make dangling references impossible to
create in the first place: seeing them means the database was restored without its
constraints, or predates them. Where the store cannot account for a reference either,
backfill says so rather than inventing a row that would make the next sweep look
clean.

6. bring up the API, workers, and web; check `/ready` and `status.sh`.

## Drill it

Restore into a scratch VM twice a year and record the date, the duration, and what
went wrong. A backup nobody has restored is a hypothesis. The verification step exists
because the first drill will fail on something, and it is better for that to be a
Tuesday afternoon.

## What is not backed up

Rotated container logs, support bundles, and the model cache — all reconstructible or
disposable.
