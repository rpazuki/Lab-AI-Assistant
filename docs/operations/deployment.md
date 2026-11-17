Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `scripts/`, `infra/compose/docker-compose.prod.yml`

# Deployment

For the shared Red Hat server (8445), follow the complete
[fresh installation and update guide](../fresh_install_linux_redhat.md).
The generic commands below require the same model and scanner prerequisites.

One VM, compose stack, one workspace. Everything below is idempotent.

## First install

```bash
git clone <repository> /opt/lab-assistant
```

```bash
cp .env.example .env
```

Then, in `.env`: a 32-byte hex `SECRET_KEY`, the database password, the provider API
key, and contact emails. **No hostnames, ports, or URLs** — those live in `.env.server`,
which is committed. The reasoning is
[ADR-0012](../architecture/adr/0012-configuration-model.md).

```bash
docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml up -d db
```

```bash
docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml run --rm -w /app/database api alembic upgrade head
```

```bash
docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml up -d
```

Create the first admin through the CLI. There is no self-registration and no default
account. The password comes from `LAB_ADMIN_PASSWORD` or an interactive prompt —
never from the command line, where it would land in `ps` output and shell history:

```bash
lab db create-admin --email <you@example.ac.uk> --workspace <slug>
```

## Routine deploys

```bash
./scripts/deploy.sh --dry-run
```

```bash
./scripts/deploy.sh
```

`deploy.sh` fast-forwards from GitHub, maps changed paths to services and rebuilds only
those, applies migrations, waits on `/health` and `/ready`, and **rolls back to the
previous image and commit if either fails**. It never pushes, never commits, and never
prints a secret.

The readiness gate reads the HTTP status rather than matching a string in the body —
200 proceeds, 503 rolls back — and prints the check that failed, so a rolled-back
deploy says *why* on the way out. `/ready` refuses when the control plane is
unreachable, when the object store does not answer, and when **the database is not at
the migration revision this build expects**: an API container that starts before its
migrations have run used to pass every check and then fail on the first real query.

## Object storage

The shared-VM profile uses a persistent `objects` volume with
`LAB_OBJECT_STORE_KIND=filesystem` and explicit
`LAB_ALLOW_FILESYSTEM_IN_PRODUCTION=true`. API and workers share this volume;
file locks coordinate writes and newly appended records are visible without
restarting readers. Back up the entire volume with PostgreSQL outside the VM.

S3 remains optional: set `LAB_OBJECT_STORE_KIND=s3`, bucket, endpoint and region
in `.env.server`, and credentials in `.env`. Selecting S3 refuses construction
when these are missing. Changing the adapter does not migrate existing files.
See [ADR-0022](../architecture/adr/0022-single-vm-filesystem-storage.md).

## Malware scanning

Uploads are scanned by default. The container wires a real scanner whenever either scan
gate is on, and the parse task is **not queued** for an asset that did not pass: the
upload reports itself blocked, with the reason, instead of leaving a task that can
never run. The production stack starts a ClamAV daemon with signature updates and shares its
Unix socket with the API and compute worker. `clamdscan` streams bytes through
that socket. If the client or daemon is unavailable, the scanner
reports itself unavailable rather than reporting `clean`.

## What refuses to start, and why that is the feature

In `server` mode the API refuses a default `SECRET_KEY`, a `localhost` public URL or
CORS origin, the fake model provider, missing model credentials, or a local-directory
object store without the explicit single-VM opt-in. Each of these has been a production incident somewhere. A stack that
starts and quietly serves with a development secret is worse than one that will not
start.

## Reading the effective configuration

```bash
lab config print
```

`GET /api/v1/config` returns the same thing over HTTP, and requires an authenticated
member with the operations capability. Secrets are digests either way — but redaction
is not a reason to publish deployment topology, provider choices, and which staged
controls are off.

## Migrations

Forward-only. Every migration is tested against a populated database. A destructive
change is two releases: add and backfill, then remove after the previous release is no
longer served. A migration that drops a column the running release still reads causes
an outage at the worst possible moment.

## Model weights

Embedding weights live in the `modelcache` volume. First start downloads them; a
rebuild must not. If the volume is lost, the next start re-downloads — slow, not fatal,
but it means the first deploy after a disk restore is not the moment to be in a hurry.

## Rolling back

Application: `./scripts/deploy.sh --ref <previous-tag>`.
Corpus: repoint the alias — see [runbooks](runbooks/README.md). They are independent,
and conflating them is a common way to make an incident worse.

See also: [monitoring](monitoring.md) · [support bundles](support-bundles.md) ·
[backup and restore](backup-and-restore.md) ·
[containers and deployment](../architecture/containers-and-deployment.md)
