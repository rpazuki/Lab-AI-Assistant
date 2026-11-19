Status: active
Owner: operator + technical owner
Last verified: 2026-08-12
Source of truth: `docker-compose.yml`, `infra/compose/docker-compose.prod.yml`

# Containers and deployment

## Supported profile

One VM running the compose stack: PostgreSQL 16 with pgvector, the API, two worker
deployments, and the web application. One workspace. No GPU.

```bash
docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml up -d
```

## What the production overlay changes

- **the database port is not published** — nothing outside the compose network reaches
  PostgreSQL;
- read-only root filesystems with explicit `tmpfs` where a process must write;
- `no-new-privileges` on every service;
- resource limits, so one runaway parse cannot take the VM;
- **compute coordinator connectivity** — PostgreSQL and S3 access remain available;
  per-document network isolation is not implemented (see
  [ADR-0021](adr/0021-worker-network-and-shared-host.md));
- log rotation, 20 MB × 5 per service. Docker's default `json-file` driver never
  rotates, and an unbounded backend log fills the disk and takes PostgreSQL with it.

The API image also carries the malware scanner. An assisted upload is scanned on the
request path — before any parse task is queued — so the scanner has to be where the
upload arrives, not only in the parsing worker. When it is absent the scanner reports
itself *unavailable* rather than *clean*, and the upload is visibly blocked.

## Volumes

| Volume | Holds | Why it must persist |
|---|---|---|
| `pgdata` | the control plane | obvious |
| `objects` | immutable assets, renditions, packages | citation targets live here |
| `modelcache` | embedding weights | a rebuild must not re-download them |
| `clamav-signatures` | updated virus definitions | avoid a full download on restart |
| `clamav-socket` | daemon socket | shared runtime connection, not backup data |

## Health and readiness

`/health` answers if the process is up. `/ready` checks the control plane and the
object store and reports each one, so a degraded dependency is visible rather than
inferred. The updater (`scripts/deploy/update.sh`) waits on both and stops with the
failing check named if they do not come up; it does not roll back.

## Refusing to start

In `server` mode the API refuses to start with development defaults still in place: a
default `SECRET_KEY`, a `localhost` public URL or CORS origin, the fake model
provider, missing model credentials, or filesystem storage without explicit single-VM opt-in. The VM profile uses
a persistent shared volume and requires off-VM backups; it shares the database's
failure domain. See [ADR-0022](adr/0022-single-vm-filesystem-storage.md).

## Backup

See [backup and restore](../operations/backup-and-restore.md). The short version:
PostgreSQL with point-in-time recovery, versioned S3 or an off-VM filesystem backup,
and a restore that
someone has actually performed. A backup nobody has restored is not a backup.
