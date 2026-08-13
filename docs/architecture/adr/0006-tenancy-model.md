Status: accepted
Owner: product owner + security owner
Last verified: 2026-08-12
Applies to: every domain table and every repository signature

# ADR-0006: Tenancy-ready, single-tenant by default

## Context

"Build shared-safe" sounds free. It is not, and the owner was being asked to decide
without the cost. Shared multi-workspace operation means:

- a required `workspace_id` on roughly every table, index, and constraint;
- workspace scope in every cache key, idempotency key, and object-encryption scope,
  which forbids cross-workspace asset deduplication and therefore re-acquires and
  re-parses the same public paper per workspace;
- workspace scope in search projections, so embedding and index storage multiply;
- a two-workspace negative suite across API, search, counts, errors, timing, export,
  and object ids — maintained forever;
- RLS policy authorship and its migration burden;
- one more dimension in every metric, log, and dashboard.

Plausibly 10–20 percent of total build effort, and a permanent multiplier on storage
and compute for shared public content.

## Decision

**Tenancy-ready, single-tenant.** Carry `workspace_id` on domain tables and keep
every repository signature workspace-scoped from the start. Seed one workspace.
Defer PostgreSQL row-level security (behind `LAB_ROW_LEVEL_SECURITY_ENABLED`),
per-workspace encryption scoping, and the no-shared-cache rule until a second lab
actually exists.

The irreversible part — the column and the call signature — is bought now.
The expensive part is bought when it is needed.

## Consequences

- Retrofitting tenancy onto populated tables is avoided, which is the one thing that
  would be a rewrite rather than a migration.
- The two-workspace negative suite exists *now* even though only one workspace is
  deployed, because it is what makes the deferral safe. It runs on every commit.
- RLS is a flag rather than a policy set. The flag's dependencies are exercised by
  the enabled path's tests, so it cannot rot unnoticed.

## Alternatives considered

- **Full shared-safe from day one**, including RLS and per-workspace encryption.
  Deferred: real cost, no named second lab.
- **Genuinely single-tenant**, no workspace column. Rejected: this is the choice
  that becomes a rewrite.

## Validation / revisit trigger

Enable RLS and the no-shared-cache rule when a second workspace holds real data.
Until then, `tests/policy/test_workspace_isolation.py` is the guarantee.

## References

- Scale, sizing, and proportionality §3.3
- `packages/python/lab_assistant/application/ports.py`
