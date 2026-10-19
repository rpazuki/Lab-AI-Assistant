Status: active
Owner: operator
Last verified: 2026-10-19
Source of truth: `docker-compose.yml`, `infra/compose/docker-compose.prod.yml`

# Fresh installation on the shared Red Hat server

This installs the new Lab AI Assistant at
`https://rlalab-assist.dept.ic.ac.uk:8445/assist`. Host nginx serves TLS; Docker
runs this application's PostgreSQL 16 with pgvector, API, workers, frontend and
ClamAV. Existing PostgreSQL and the applications on 8443/8444 are untouched.
API port 8100 and frontend port 3300 bind only to loopback. Compose 2.24.4 or newer
is required for port overrides. No Node or Python installation on the host is needed.
Local email/password login works in production; there is no default account.
The stopped legacy containers and their database are not automatically imported.

## 1. Check prerequisites and ports

```bash
sudo systemctl is-active docker nginx
docker compose version
sudo docker info --format '{{.ServerVersion}}'
sudo ss -lntp | grep -E ':(3300|8100|8445)\b' || true
df -h /opt /var/lib/docker
free -h
```

3300, 8100 and 8445 must be free. If Docker or Compose is absent, install the
institution-approved Docker Engine and Compose plugin before continuing. Allow
capacity for a PostgreSQL database, ClamAV signature loading, web builds and workers;
the worker resource ceilings alone total 6 GB and are not a whole-stack estimate.
Keep the old `rlalab_db` until its data has been backed up and migration assessed.
Ensure Docker starts at boot with `sudo systemctl enable --now docker`.

## 2. Clone as the application owner

```bash
id labassistant || sudo useradd --system --create-home --shell /bin/bash labassistant
sudo install -d -o labassistant -g labassistant /opt/lab-assistant
sudo -u labassistant git clone https://github.com/rpazuki/Lab-AI-Assistant.git /opt/lab-assistant
cd /opt/lab-assistant
```

For an existing checkout, use `sudo -u labassistant git status --short`, reconcile
local edits, then `sudo -u labassistant git pull --ff-only`. Do not clone over files.
If this repository is private, configure Git access for this account first.
The account owns the checkout; root invokes Docker with sudo, without giving the
account membership in the root-equivalent docker group.

## 3. Configure secrets and external dependencies

**Required before startup:** an approved Anthropic API key and sufficient VM disk
space. This profile stores documents in the persistent `objects` Docker volume;
S3 and its credentials are optional. Server mode still rejects fake/missing model
credentials. Back up both PostgreSQL and the objects volume outside the VM.
Outbound access is needed for image/package downloads, ClamAV updates and enabled
literature/model providers. Configure institutional proxy/CA trust if required.

```bash
sudo -u labassistant cp .env.example .env
sudo chmod 600 .env
openssl rand -hex 32
openssl rand -hex 32
sudoedit /opt/lab-assistant/.env
```

Put the first generated value in `LAB_SECRET_KEY`, the second in
`POSTGRES_PASSWORD`; set `POSTGRES_USER=labassistant` and
`POSTGRES_DB=lab_assistant`. Hex avoids URL-special-character issues in the Compose
DSN. Set these additional values in `.env` (replace placeholders):

```dotenv
LAB_ENV=server
LAB_LLM_PROVIDER=anthropic
LAB_LLM_API_KEY=<approved-provider-key>
LAB_EXTERNAL_MODEL_ENABLED=true
LAB_CONNECTOR_CONTACT=<your-email-address>
```

External model transfer remains subject to application rights/policy checks. Keep
secrets out of Git. Do not paste `.env` or resolved Compose configuration into logs.

```bash
sudoedit /opt/lab-assistant/.env.server
```

Public URL and CORS already target 8445. Local storage is already configured:
`LAB_OBJECT_STORE_KIND=filesystem`, `LAB_ALLOW_FILESYSTEM_IN_PRODUCTION=true`,
and `LAB_OBJECT_STORE_ROOT=/var/lib/lab-assistant/objects`. All API/worker containers
share that persistent volume. No additional storage service is needed.
CORS is the origin without `/assist`; public URL
includes `/assist`. `.env.server` contains addresses, not passwords. Existing
PostgreSQL credentials/ports and the legacy database must not be reused here.

## 4. Build, start dependencies and migrate

Run the following from `/opt/lab-assistant` in Bash. The explicit project name keeps
containers and volumes separate from the old `rlalab` stack.

```bash
cd /opt/lab-assistant
export COMPOSE_PROJECT_NAME=lab-assistant
DC=(sudo docker compose -p lab-assistant -f docker-compose.yml -f infra/compose/docker-compose.prod.yml)
"${DC[@]}" config --quiet
"${DC[@]}" build api web worker-network worker-compute scanner
"${DC[@]}" up -d --wait --wait-timeout 900 db scanner
"${DC[@]}" run --rm --no-deps --user 0 --entrypoint sh api -c 'chown -R 10001:10001 /var/lib/lab-assistant/objects'
"${DC[@]}" run --rm --no-deps -w /app/database api alembic upgrade head
"${DC[@]}" run --rm --no-deps api lab config print
"${DC[@]}" up -d --wait --wait-timeout 900
sudo ./scripts/check-web.sh
```

ClamAV's first signature download may take several minutes. If it fails, inspect
`"${DC[@]}" logs --tail=100 scanner`; do not disable scanning to hide the failure.
Image builds include frontend CSS and public assets; no manual `.next` copying is needed.

## 5. Create the first administrator and check the application

```bash
"${DC[@]}" exec api lab db create-admin --email YOUR_EMAIL --workspace rlalab
curl --noproxy '*' --fail --retry 20 --retry-connrefused --retry-delay 2 http://127.0.0.1:8100/health
curl --noproxy '*' --fail http://127.0.0.1:8100/ready
curl --noproxy '*' --fail -I http://127.0.0.1:3300/assist/login
"${DC[@]}" exec -T api sh -c 'printf "clean scanner check" | clamdscan --stream --no-summary -'
```

The administrator command prompts for a password of at least 12 characters. Run it
once; it is not a password-reset command. Read `/ready`: both database and object
store must be true and status must be `ok` (a degraded result still uses HTTP 200).
The scanner check must exit successfully; it confirms the client can reach clamd.

## 6. Configure host nginx

Find the existing matching certificate key path without displaying its contents:

```bash
sudo grep -n ssl_certificate_key /etc/nginx/conf.d/biopipeline2.conf
sudo install -m 644 infra/compose/nginx-lab-assistant.conf.example /etc/nginx/conf.d/lab-assistant.conf
sudoedit /etc/nginx/conf.d/lab-assistant.conf
```

Replace `REPLACE_WITH_EXISTING_KEY.key` with that path. The existing hostname
certificate works on 8445 too. Do not edit the two other applications' server blocks
or add a second `conf.d` include. These are nginx configuration directives, not shell
commands. The example forwards paths unchanged, including `/assist` and static assets.

```bash
getenforce
sudo setsebool -P httpd_can_network_connect 1
sudo semanage port -l | grep http_port_t
```

If SELinux is enforcing and 8445 is not already an HTTP port, run
`sudo semanage port -a -t http_port_t -p tcp 8445`. If assigned to another type,
inspect that assignment before changing it. `semanage` is supplied by
`policycoreutils-python-utils` on Red Hat. Do not disable SELinux.

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo ss -lntp | grep -E ':(8443|8444|8445)\b'
sudo firewall-cmd --state
```

If firewalld is running, identify the active zone with
`sudo firewall-cmd --get-active-zones`, then allow `8445/tcp` in the zone containing
the server interface, both permanently and at runtime. For example, for `public`:

```bash
sudo firewall-cmd --zone=public --permanent --add-port=8445/tcp
sudo firewall-cmd --zone=public --add-port=8445/tcp
```

ICT/Zscaler access policy must also allow 8445 for intended clients.

## 7. Browser check and diagnostics

Open `https://rlalab-assist.dept.ic.ac.uk:8445/assist/login` from the college-managed
machine that already trusts the Imperial certificate. A personal machine's trust
warning is independent of application readiness; server certificate installation
cannot establish client trust.

```bash
curl --noproxy '*' --resolve rlalab-assist.dept.ic.ac.uk:8445:127.0.0.1 --cacert /etc/pki/tls/certs/ic-ca-root-icca4-2045.crt --fail -I https://rlalab-assist.dept.ic.ac.uk:8445/assist/login
"${DC[@]}" ps
"${DC[@]}" logs --tail=100 api web worker-network worker-compute scanner
sudo journalctl -u nginx -n 50 --no-pager
```

Log in, select the workspace, and check the collection/review pages. This is a fresh
empty database; old reports, accounts and documents are not imported automatically.
The compute coordinator has network access to its dependencies; a per-document
network sandbox is not implemented, as recorded in
[ADR-0021](architecture/adr/0021-worker-network-and-shared-host.md).

## 8. Update from Git

Back up the database and objects volume together first; see
[backup and restore](operations/backup-and-restore.md). Then, in Bash:

```bash
cd /opt/lab-assistant
DC=(sudo docker compose -p lab-assistant -f docker-compose.yml -f infra/compose/docker-compose.prod.yml)
sudo -u labassistant git status --short
```

If `.env.server` has local address changes, preserve and reapply them when merging;
resolve conflicts before continuing. Never reset local settings or volumes blindly.

```bash
sudo -u labassistant git pull --ff-only
"${DC[@]}" build api web worker-network worker-compute scanner
"${DC[@]}" stop web api worker-network worker-compute
"${DC[@]}" up -d --wait --wait-timeout 900 db scanner
"${DC[@]}" run --rm --no-deps --user 0 --entrypoint sh api -c 'chown -R 10001:10001 /var/lib/lab-assistant/objects'
"${DC[@]}" run --rm --no-deps -w /app/database api alembic upgrade head
"${DC[@]}" up -d --wait --wait-timeout 900
sudo ./scripts/check-web.sh
curl --noproxy '*' --fail http://127.0.0.1:8100/ready
```

Apply migrations on every update; `upgrade head` is safe when already current.
If migration fails, stop and inspect before restarting application services. Do not
assume an old image can read a migrated schema. Do not use `down -v`: it deletes data.

## Verification scope

Image builds, fresh PostgreSQL/pgvector migrations, administrator creation,
production password login, Secure cookies, prefixed routes/CSS, worker startup,
and clean/EICAR scanner checks were exercised locally. Real provider credentials, optional S3 bucket permissions, Red Hat SELinux
and Zscaler reachability must be verified
on the target server using the checks above.

## Local storage and backups

The `lab-assistant_objects` volume survives rebuilds, restarts and ordinary
`docker compose down`. Never run `down -v`. A VM/disk loss can destroy both the
database and documents, so a second copy must be held outside this VM. A snapshot
on the same disk is not that second copy. This profile supports one POSIX VM,
not a shared volume across several hosts.

For a consistent backup, stop all application writers, dump PostgreSQL, archive
the objects volume, then restart. Run in the Bash session with `DC` defined above:

```bash
sudo install -d -m 700 /var/backups/lab-assistant
set -euo pipefail
BACKUP_TAG=$(date -u +%Y%m%dT%H%M%SZ)
"${DC[@]}" stop web api worker-network worker-compute
"${DC[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' | sudo tee "/var/backups/lab-assistant/$BACKUP_TAG.database.dump" >/dev/null
"${DC[@]}" run --rm --no-deps --user 0 --entrypoint tar api -C /var/lib/lab-assistant/objects -czf - . | sudo tee "/var/backups/lab-assistant/$BACKUP_TAG.objects.tar.gz" >/dev/null
"${DC[@]}" start api worker-network worker-compute web
```

If a backup command fails, resolve the error and restart the application with
the final `start` command; do not treat a failed archive as a usable backup.
Check both commands succeeded and copy both matching files to approved storage
outside the VM. Protect `.env` separately. Restore the matching database and full
objects archive with writers stopped, correct ownership to UID 10001, and run
`lab db verify-restore --workspace rlalab` before serving. See
[backup and restore](operations/backup-and-restore.md).

If an installation already uses S3, keep its settings: switching to filesystem
does not copy existing objects. If upgrading a checkout with old S3 defaults but
no stored data, ensure `.env.server` contains the three filesystem settings above.
