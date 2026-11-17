Status: active
Owner: operator
Last verified: 2026-11-17
Source of truth: `infra/backup/`

# Backup and restore

Three things must survive: the control plane (PostgreSQL), the object store, and `.env`.
Lose any one and the other two are not enough.

| Data | Method | Frequency | Target |
|---|---|---|---|
| Everything portable | `lab backup create` — logical dump, every object, a manifest | daily | RPO 24 h |
| PostgreSQL, finer | base backup + WAL archiving (point-in-time recovery) | continuous | RPO 15 min |
| `.env` | the institution's secret manager. **Not in a backup archive.** | on change | – |
| Model cache | none | – | re-downloadable |

Recovery target: **RTO 4 hours**, on the assumption that the VM must be rebuilt.

## Two backups, two questions

They are not redundant, and neither replaces the other.

**`lab backup create` is the portable one.** A logical `pg_dump`, every object
copied through the application's own store port, and a manifest recording the app
version, Alembic revision, PostgreSQL version, extension versions, per-workspace
counts and the names of the secrets a restore will need. It restores onto a VM
nobody has provisioned yet, and because the objects go through the store port it
backs up an S3 installation exactly as well as a local one — which the `rsync`
it replaces could not do at all.

**`pg_basebackup` plus archived WAL is the fast one.** Point-in-time recovery to
any moment since the base, which is what the 15-minute RPO actually rests on. It
restores only onto compatible infrastructure: same major version, same platform,
same page layout. Treat it as the shortcut, not as the portability contract.

`infra/backup/backup.sh` takes both. Set `WITH_PHYSICAL=0` on a deployment that
has accepted a 24-hour RPO and does not archive WAL.

## Why objects and the database must be restored to compatible points

A database row citing an object digest that the restored store does not contain is a
broken citation. Restore the object store to a point **at or after** the database's
recovery point: an extra object is harmless, a missing one is not.

For the shared VM, the [installation guide](../fresh_install_linux_redhat.md#local-storage-and-backups)
provides a stopped-writer database dump and objects archive procedure. Copy both
off the VM; backing up PostgreSQL alone does not preserve document bytes.

## Taking one

```bash
lab backup create /var/backups/lab-assistant/$(date -u +%Y%m%dT%H%M%SZ)/portable
```

It refuses to write into a directory that already holds a backup: merging two
runs produces a manifest describing neither, and the mixture verifies as long as
nobody looks closely. `--dry-run` fingerprints and inventories without writing,
which is the cheap way to see what a backup would contain.

The result holds password hashes, unreviewed evidence and chat history. It
belongs in **encrypted** off-site storage. The command does not encrypt it:
rolling our own would be worse than stating the requirement and leaving it to
storage that is actually audited.

## Checking one

```bash
lab backup verify /var/backups/lab-assistant/<stamp>/portable
```

Reads only the backup, never the running installation — so it runs against the
off-site copy, which is the one whose integrity is actually in question. It
re-digests the dump, re-digests every payload, and catches a manifest edited
after it was written. `--skip-objects` does the cheap half; the object sweep is
what finds bit rot, so skip it for a routine check and not before a drill.

## Putting one back

A restore decides what it is before it touches anything:

```bash
lab backup restore /backup/portable --intent clone
```

That writes nothing. It verifies the backup, fingerprints this destination, and
reports a verdict — every finding separated into what blocks the restore and
what an operator must still do. Blocking findings are refusals, not warnings to
override:

| Finding | Why it blocks |
|---|---|
| `extension_missing`, `extension_older` | the dump will not replay, or its indexes will not read |
| `server_older` | `pg_restore` does not go backwards across a major version |
| `schema_from_the_future` | older code restores the rows and silently ignores the columns it does not know |
| `destination_not_empty` | a logical restore replays into an empty database; over existing tables it fails partway, after the payloads are in |
| `backup_incomplete` | a known hole is not a recovery point |
| `origin_not_fenced` | see below |

`--clean` is the deliberate alternative to `destination_not_empty`: it drops what is
there first, which destroys it. The operator who meant a scratch database and typed the
address of a live one should be stopped, not accommodated.

Add `--apply` to carry it out. **Restore into a fresh, empty database.** The dump
brings the schema with it.

The database goes back first and the objects second, which is the opposite of
the order a backup writes them in — and the order is forced, not chosen. The
object copy is driven by the *restored* registry: it copies what the restored
control plane actually references and verifies each payload against the digest
that control plane recorded. Before the dump is replayed there is no registry to
read. (A backup puts payloads first for its own good reason: it must never hold
a control plane newer than its objects. Restoring is not the mirror image of
backing up, and assuming it was cost this implementation a defect that only a
real database exposed.)

If payloads fail to arrive after the database is in place, the restore reports
it and does **not** call itself applied: re-run it, and the object copy skips
what already arrived.

### Clone or takeover

A backup is a faithful copy, so restoring one always produces something that
believes it is the publisher. Restore it twice and both work — and diverge,
until somebody notices a release on one that is missing from the other.

So the intent is a deliberate word:

- `--intent clone` (the default) runs the restored installation under a new
  identity, and it **may not publish** until a promotion under the handover
  protocol. A drill, a scratch VM and a staging copy are all clones.
- `--intent replace` takes over the original's identity and its right to
  publish. It requires `--origin-is-fenced`, which is your assertion that you
  have stopped the original. Without it the restore is refused.

### The rest of the procedure

1. preflight with `lab backup restore` and read the verdict;
2. `--apply` once it is permitted;
3. put the secrets back from the secret manager — the manifest's
   `secret_references` lists them by name, and never carries a value;
4. run `alembic upgrade head` (a no-op if the backup was current);
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

## Point-in-time recovery, and the volumes it writes into

`infra/backup/restore.sh --physical` is the WAL route. It writes into Docker
volumes, which is why it needed the most repair: the script it replaces named
`lab-assistant_pgdata` literally and then ran `rm -rf` on whatever it found
there. Compose prefixes volume names with the project name, which defaults to
the directory the stack came up from, so on a host running a second deployment
that name belonged to somebody else.

`infra/backup/lib/volumes.sh` resolves the name from the host instead, and
refuses rather than guesses:

- no volume matching the suffix → refuse, because an empty volume restores into
  nothing and the stack comes up looking healthy with no data;
- more than one → refuse, because two candidates means two deployments and
  picking one is how a restore lands in the wrong installation;
- the target is not empty → refuse, because it belongs to a running
  installation.

Set `PGDATA_VOLUME` and `OBJECTS_VOLUME` to name them explicitly where the
resolution is genuinely ambiguous.

## Drill it

Restore into a scratch VM twice a year and record the date, the duration, and what
went wrong. A backup nobody has restored is a hypothesis. The verification step exists
because the first drill will fail on something, and it is better for that to be a
Tuesday afternoon.

## What is not backed up

Rotated container logs, support bundles, and the model cache — all reconstructible or
disposable.
