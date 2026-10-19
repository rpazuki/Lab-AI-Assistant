# Operations scripts

Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: the scripts themselves

Ported from the predecessor because they encode real operational knowledge. Each
one is safe to run twice.

| Script | What it does |
|---|---|
| `deploy.sh` | fast-forward from GitHub, rebuild only changed services, migrate, verify, roll back on failure |
| `restart.sh` | bounce or recreate services without touching git |
| `status.sh` | deployed commit, container states, health, queue depth, disk |
| `logs.sh` | live unified log view, redacted |
| `collect-logs.sh` | redacted support bundle, with a verification pass |

## Recipes

```bash
./scripts/deploy.sh --dry-run              # what would rebuild, and why
./scripts/deploy.sh                        # deploy main
./scripts/deploy.sh --ref release/2026-08  # deploy a specific ref
./scripts/deploy.sh --all                  # rebuild everything
./scripts/status.sh                        # is it healthy, and how deep is the queue
./scripts/logs.sh api --since 30m --grep error
./scripts/restart.sh worker-compute --recreate
./scripts/collect-logs.sh --since 2h --out ~/reports
```

## Why deploy rebuilds selectively

`lib/common.sh` maps changed paths to services. A frontend-only change does not
rebuild three Python images, and a `packages/python` change rebuilds the API *and*
both workers because they share that package. Rebuilding everything every time
costs minutes the VM does not have; rebuilding too little ships a stale worker.

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
- treat a passing health check as proof the release is correct — that is what the
  release gates and the evaluation run are for.

`sudo ./scripts/check-web.sh` checks the deployed login route and CSS without
credentials. It catches a healthy Node process serving broken prefixed routes.
