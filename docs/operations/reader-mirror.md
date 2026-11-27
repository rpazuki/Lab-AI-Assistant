Status: active
Owner: operator
Last verified: 2026-11-27
Source of truth: `scripts/archive.sh`, `infra/compose/docker-compose.reader.yml`, `.env.site.example`

# A reader mirror on another host

How to run a second installation outside Imperial that serves the same library, and
keep it current. Design: [ADR-0026](../architecture/adr/0026-content-replication.md)
(what may travel) and [ADR-0029](../architecture/adr/0029-reader-import.md) (how a
reader holds it).

## How it fits together

The Imperial VM is the **publisher**. It curates the corpus and, on a timer, publishes a
signed snapshot of it to a bucket. The mirror is a **reader**. It can only read the
bucket, and it imports each snapshot into its own database. Members, chats and
feedback on the mirror belong to the mirror.

Each machine keeps its own private object store on its own disk. Only the
`lab archive` commands use the bucket, through the `LAB_ARCHIVE_STORE_*` settings, so
neither application's normal running depends on the bucket being reachable.

```
publisher (Imperial)                      bucket                    reader (mirror)
  scripts/archive.sh publish  ── write ─▶  archive/libraries/<id>/  ◀─ read ── scripts/archive.sh sync
```

## Before you start

- **Text travels only where the data steward allows it.** A paper's text, chunks and
  vectors are copied only when its rights decision grants `replicate_content`. With
  `LAB_RIGHTS_REPLICATE_OPEN_CONTENT=true` on the publisher, decisions made from then on
  grant it for open licences. Decisions made before that keep the actions they had, and
  no command re-decides them yet. Until then, the mirror gets those papers' records and
  reviewed claims, but not their text.
- **The policy checkpoint expires.** It is valid for `LAB_ARCHIVE_POLICY_VALIDITY_HOURS`
  (24 by default). After that, a reader stops serving restricted material. The
  publisher therefore renews it every 6 hours ([§4](#4-timers)).

## 1. The bucket and its two identities

Run this in Cloud Shell. The publisher's role is Object User, not Object Creator,
because moving the library head overwrites an object. The reader can read objects and
cannot list them, which is all `lab archive fetch` needs.

```bash
PROJECT=your-project; BUCKET=your-bucket
gcloud storage buckets update gs://$BUCKET --uniform-bucket-level-access --public-access-prevention
gcloud iam service-accounts create lab-archive-publisher --project=$PROJECT
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:lab-archive-publisher@$PROJECT.iam.gserviceaccount.com --role=roles/storage.objectUser
gcloud iam service-accounts create lab-archive-reader --project=$PROJECT
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:lab-archive-reader@$PROJECT.iam.gserviceaccount.com --role=roles/storage.legacyObjectReader
```

**The publisher is not on Google, so it needs a key file:**

```bash
gcloud iam service-accounts keys create gcs-publisher.json \
  --iam-account=lab-archive-publisher@$PROJECT.iam.gserviceaccount.com
```

Copy the file to `/etc/lab-assistant/` on the publisher. Then delete every other copy:
it is a credential that never expires.

**A reader on a Google VM needs no key.** Attach the reader account to the VM, which
must be stopped first:

```bash
gcloud compute instances set-service-account MIRROR_VM --zone=ZONE \
  --service-account=lab-archive-reader@$PROJECT.iam.gserviceaccount.com --scopes=storage-ro
```

`MIRROR_VM` and `ZONE` are the name and zone shown by `gcloud compute instances list`.

## 2. The publisher

Files on the host. The container user is uid 10001.

```bash
sudo install -d -m 700 -o 10001 -g 10001 /etc/lab-assistant
sudo chown 10001:10001 /etc/lab-assistant/gcs-publisher.json
sudo chmod 600 /etc/lab-assistant/gcs-publisher.json
```

**Make the signing key once.** It stays on this host. Give the printed public key to
every reader.

```bash
cd /opt/lab-assistant
sudo docker compose -p lab-assistant -f docker-compose.yml -f infra/compose/docker-compose.prod.yml \
  run --rm --no-deps -v /etc/lab-assistant:/keys api python -c "from pathlib import Path; from lab_assistant.adapters.signing import generate_keypair as g; k,p=g(); f=Path('/keys/archive-signing.key'); f.write_text(k); f.chmod(0o600); print('public key:', p)"
```

**In `.env`:**

```
LAB_ARCHIVE_SIGNING_KEY_PATH=/etc/lab-assistant/archive-signing.key
LAB_ARCHIVE_SIGNING_KEY_ID=rlalab-2026
LAB_ARCHIVE_TRUSTED_KEYS=rlalab-2026=<the public key>
LAB_RIGHTS_REPLICATE_OPEN_CONTENT=true
```

**In `.env.site`.** The Imperial VM's addresses stay in `.env.server`; this file only
says where the archive is.

```
LAB_ARCHIVE_STORE_KIND=gcs
LAB_ARCHIVE_STORE_BUCKET=your-bucket
LAB_ARCHIVE_STORE_PROJECT=your-project
LAB_ARCHIVE_STORE_PREFIX=archive
LAB_ARCHIVE_STORE_CREDENTIALS_PATH=/etc/lab-assistant/gcs-publisher.json
```

**Publish.** The last line of the output names the library id that readers need.

```bash
sudo bash scripts/archive.sh publish
```

## 3. The reader

Clone the repository, then write `.env` (secrets: `POSTGRES_PASSWORD`, `LAB_SECRET_KEY`,
the LLM key) and `.env.site` (start from `.env.site.example`):

```
LAB_CORS_ORIGINS=["https://mirror.example.org"]
LAB_APP_PUBLIC_URL=https://mirror.example.org/assist
LAB_INSTALLATION_ROLE=reader
LAB_ARCHIVE_STORE_KIND=gcs
LAB_ARCHIVE_STORE_BUCKET=your-bucket
LAB_ARCHIVE_STORE_PROJECT=your-project
LAB_ARCHIVE_STORE_PREFIX=archive
LAB_ARCHIVE_LIBRARY_ID=<from the publisher>
```

In `.env`, add the publisher's public key, and use the same embedding model as the
publisher. If the models differ, imported vectors are skipped and search falls back to
keywords only.

```
LAB_ARCHIVE_TRUSTED_KEYS=rlalab-2026=<the public key>
LAB_ARCHIVE_REQUIRE_SIGNATURE=true
LAB_EMBEDDING_MODEL=hashing-v1
```

`.env.site` exists because `.env.server` is committed with the Imperial VM's addresses
and is read after `.env`. It is read last and is not committed, so the deploy script's
"no local edits" check still passes.

**Deploy, create the administrator and import:**

```bash
sudo bash scripts/deploy.sh --first-install
sudo docker compose -p lab-assistant -f docker-compose.yml -f infra/compose/docker-compose.prod.yml \
  -f infra/compose/docker-compose.reader.yml exec api lab db create-admin --email YOU@example.org --workspace rlalab
sudo bash scripts/archive.sh sync
```

With `LAB_INSTALLATION_ROLE=reader`, the deploy script:

- runs no workers and no scanner (`infra/compose/docker-compose.reader.yml`). A
  reader's workers refuse to start, and a reader accepts no uploads;
- refuses to run without `.env.site`;
- accepts an API whose only failing readiness check is `corpus`, the expected state
  before the first import.

Put a TLS reverse proxy in front of `127.0.0.1:3300`. `infra/compose/nginx-lab-assistant.conf.example`
is a starting point.

## 4. Timers

Run this on each machine, the publisher and the reader. It installs systemd timers for
the machine's role:

```bash
sudo bash scripts/archive.sh schedule
```

| Role | Runs (UTC) |
|---|---|
| publisher | `publish` daily at 02:15; `policy`, which renews the checkpoint only, at 08:15, 14:15 and 20:15 |
| reader | `sync` daily at 03:15 |

A daily publish alone is not enough. The checkpoint lasts 24 hours, so one late or failed
run lets it lapse. Renewing it every 6 hours leaves three failed runs before that
happens. Runs wait for each other, because a renewal and a publish both move the policy
head. A timer missed while the machine was off runs when it starts again.

Options given to `schedule`, such as `--workspace` or `--keep`, are used by every run. To
change them, run `schedule` again. Use a timer instead of a crontab line. `schedule`
warns if root's crontab still runs `archive.sh`.

```bash
systemctl list-timers 'lab-archive-*'
```

```bash
journalctl -u lab-archive-publish -n 50
```

To try a run now, use `sudo systemctl start lab-archive-publish.service`, or
`lab-archive-sync.service` on the reader. `sudo bash scripts/archive.sh unschedule`
removes the timers.

Each run writes a new directory under `/srv/lab-archive` and keeps the newest three
(`--keep`).

## 5. What a mirror keeps, and serving less

**From the publisher:** everything from scopes to published releases. Papers, texts,
claims, reviews, scopes and releases arrive in the snapshot, and an import only adds
to them. A snapshot also records which scopes found each paper, so a mirror knows
what each scope covers without the publisher's discovery runs.

**The mirror's own:** members, chat sessions, questions and answers, feedback and
logins. No snapshot carries them and no import touches them, so each mirror's Ask
history is separate from the publisher's and from every other mirror's. The
publisher's scope access rules do not travel either. On a mirror, every member sees
everything the mirror serves.

**Serving less.** A mirror's admin can stop serving some scopes or releases, for
everyone on that mirror, admins included. The list belongs to the mirror and imports
never change it.

- **A removed scope:** a paper is withheld when every scope that found it is
  removed. It is left out of Ask, Search, Analytics and exports. Citations to it in
  earlier answers no longer open.
- **A removed release:** it is never served. The mirror follows the publisher's
  current release while that release is not removed, and otherwise serves the newest
  published release that is. If the mirror removes the publisher's choice, a release
  the publisher publishes later is still served, because it is not on the list.
  Putting a release back restores the publisher's choice at once.

An admin does this on **Administration › Mirror access**, which appears only on a
mirror. Each scope and release has a **Serve** box, and **Save** records the whole
choice, with who made it and when. Behind the page is
`GET`/`PUT /api/v1/mirror-access`, with
`{"withheld_scope_ids": [...], "withheld_release_ids": [...]}`. On the publisher the
page says to use scope access rules and the Releases page instead.

Papers imported before this version carry no scopes until the next import, which
needs a publish from a publisher running this version or later. When a run fails, the [archive fetch runbook](runbooks/archive-fetch.md)
and the [policy checkpoint runbook](runbooks/policy-checkpoints.md) say what each
message means.
