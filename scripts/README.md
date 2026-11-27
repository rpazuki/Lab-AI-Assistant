# Operations scripts

Status: active
Owner: operator
Last verified: 2026-11-19
Source of truth: the scripts themselves

Ported from the predecessor because they encode real operational knowledge. Each
one is safe to run twice.

| Script | What it does |
|---|---|
| `deploy/update.sh` | the updater: lock, fast-forward pull, build while serving, migrate, verify over loopback — never rolls back |
| `deploy.sh` | entry point that hands to `deploy/update.sh` (`--dry-run` means `--check`) |
| `restart.sh` | bounce or recreate services without touching git |
| `status.sh` | deployed commit, container states, health, queue depth, disk |
| `logs.sh` | live unified log view, redacted |
| `collect-logs.sh` | redacted support bundle, with a verification pass |
| `archive.sh` | `publish` the library to the shared archive, renew its `policy` checkpoint, or `sync` a reader from it; `schedule` installs the systemd timers that run these ([reader mirror](../docs/operations/reader-mirror.md)) |

Every script reads the installation role from `.env`, `.env.server` and `.env.site`
(last wins, as Compose reads them). On a reader they add
`infra/compose/docker-compose.reader.yml` and leave out the workers and the scanner.

## Recipes

```bash
sudo bash scripts/deploy/update.sh --check          # preflight; what would deploy
sudo bash scripts/deploy/update.sh                  # deploy the checked-out branch
sudo bash scripts/deploy/update.sh --first-install  # first deployment of a checkout
sudo bash scripts/deploy/update.sh --cpu-percent 70 # all containers together: at most 70% of the CPUs (default 80)
./scripts/status.sh                        # is it healthy, and how deep is the queue
./scripts/logs.sh api --since 30m --grep error
./scripts/restart.sh worker-compute --recreate
./scripts/collect-logs.sh --since 2h --out ~/reports
sudo bash scripts/archive.sh publish                # publisher: policy, package, publish
sudo bash scripts/archive.sh policy                 # publisher: renew the policy checkpoint only
sudo bash scripts/archive.sh sync                   # reader: fetch and import
sudo bash scripts/archive.sh schedule               # either: install this role's timers
systemctl list-timers 'lab-archive-*'               # when they run next
```

## Why the updater never rolls back

The previous `deploy.sh` reset git to the old commit when a health check failed. After
a migration that runs old code against a schema it has never seen — the defect the code
audit records as F14. The updater follows the Protocols Analysis updater on the same
server instead: build everything while the old version serves (Docker's layer cache
keeps an unchanged image cheap), migrate explicitly, and on failure stop and name what
to inspect. Recovery is a backup restore to a matching commit, done deliberately.

## Why the bundle verifies its own redaction

`collect-logs.sh` redacts, then greps for anything still secret-shaped and deletes
the bundle if it finds something. A support bundle is handed to someone outside the
deployment, so "we redacted it" has to be checked rather than asserted. Redaction
patterns live in `lib/redact.sh`; add one there and it applies to the live log view
too, because an operator sharing a screenshot is the most common way a token escapes.

## What these scripts never do

- print a secret, even when a command would be more convenient with one;
- push to git, or create a commit;
- run a migration without the deploy path having verified health afterwards;
- roll the code back over a migrated schema;
- touch nginx, SELinux, the firewall or another application on the server;
- treat a passing health check as proof the release is correct — that is what the
  release gates and the evaluation run are for.

`sudo ./scripts/check-web.sh` checks the deployed login route and CSS without
credentials. It catches a healthy Node process serving broken prefixed routes.
