Status: accepted
Owner: technical owner + operator
Last verified: 2026-08-12
Applies to: every component's configuration

# ADR-0012: Configuration model carried forward from the predecessor

## Context

The predecessor documented a hard-won configuration model: one variable names the
scenario, the secrets file holds no hostnames or ports, address files are per-scenario
and committed, each address has exactly one owner, and no password is duplicated into
a `DATABASE_URL`. Those rules exist because breaking them produced a specific
recurring failure — a host process handed a Compose service name, giving
`getaddrinfo ENOTFOUND backend`.

The migration plan proposed a layered loading order and never referenced that lesson.
Discarding a learned correction because the surrounding architecture changed is exactly
the pattern the plan criticises elsewhere.

## Decision

Carry the lesson forward *and* reconcile it with the layered order.

One variable names the scenario:

```text
LAB_ENV = local | compose | server
```

Loading order, lowest precedence first:

1. compiled defaults — and **the defaults serve `local`**, because compose always
   injects addresses explicitly while a bare shell has no injection mechanism. This is
   why `database_url` defaults to `localhost:5433` and never to `db:5432`;
2. the versioned environment file `infra/environments/<LAB_ENV>.yaml` — behaviour
   only, and a secret key in it is a validation error;
3. workspace config records in the database — scientific settings, not deployment
   ones;
4. environment variables — endpoints and secrets;
5. explicit CLI flags — one invocation.

Rules that do not bend:

- `.env` holds secrets and behaviour, **no hostnames, ports, or URLs**, so
  `source .env` is safe in any shell;
- addresses live in `.env.compose` and `.env.server`, committed because they hold no
  secrets;
- **each address has exactly one owner**. The database URL is owned by `Settings` and
  by the `environment:` block each compose file builds from `POSTGRES_*`; the frontend
  owns its backend URL; evaluation owns its API URL. Nothing restates another
  component's address;
- **no password is duplicated into a `DATABASE_URL` in an address file**. A second copy
  drifts the moment anyone rotates the password, and the whole stack then sits in a
  restart loop;
- `lab config print` shows every key with its source and secrets reduced to a digest.

The backend is environment-variable driven and has no YAML config file of its own
beyond the committed behaviour file.

## Consequences

- The `ENOTFOUND` class of bug cannot recur: a host process reading `.env` finds no
  address to be misled by.
- Rotating a password touches one place.
- Two deployments can be compared by digest without disclosing a secret.
- `POSTGRES_PASSWORD` is an initdb variable: changing it never changes an existing
  database. That surprise is documented in the deployment guide.

## Alternatives considered

- **The plan's layered order alone.** Rejected: it permits an address in the secrets
  file, which is the specific mistake.
- **One `.env` for everything.** Rejected: it is how the original bug happened.

## Validation / revisit trigger

`lab config print` on each scenario, and the production blocker check that refuses to
start with development defaults. Revisit if a secret manager replaces environment
injection — the owner rules stay, the mechanism changes.

## References

- Predecessor `CLAUDE.md` §14 and `mistakes.md`
- `packages/python/lab_assistant/config.py`
