Status: active
Owner: technical owner
Last verified: 2026-08-12
Source of truth: `tests/contract/test_module_dependencies.py`

# Modules and dependencies

```text
contracts            schemas, taxonomies, canonicalisation, runtime seams
   ^
domain               entities, invariants, state machines — no I/O of any kind
   ^
policy, provenance   grants and rights; fingerprints, audit chain, lineage
   ^
application          lifecycle use cases, transaction boundaries, ports
   ^
adapters             SQL, object store, connectors, parsers, providers, search
```

The direction is enforced by a test that reads the AST of every module. So are the
specific prohibitions: domain importing SQLAlchemy, FastAPI, an SDK, `os`, or
`pathlib`; anything in domain reading the clock or opening a file; workers importing
each other; anyone importing PyMuPDF.

## Why the seams are where they are

**`contracts` may not import the project.** It holds the vocabulary every layer
shares. If it could import domain, "the contract" would depend on an implementation.

**`domain` has no I/O.** Every invariant is a pure function of values, so the test for
"a title-only merge is impossible" needs no database and cannot be fooled by one.

**`application` owns transactions.** A service opens a unit of work, uses
repositories, and commits. No route, worker, or CLI command manages a transaction, so
the same operation behaves identically however it was invoked.

**`adapters` are replaceable, and two exist for the important ones.** In-memory and
SQL repositories, in-memory and filesystem object stores, fake and real connectors and
providers. The offline suite runs the first of each; the SQL adapter also runs against
SQLite in CI and PostgreSQL when a URL is provided. Interchangeability is tested, not
asserted.

## Ports

`application/ports.py` defines every repository protocol. **Every method takes
`workspace_id` as a required argument, never an optional filter.** That signature is
the irreversible half of tenancy-readiness (ADR-0006): adding the column later is a
migration, retrofitting the call sites is a rewrite.

## Registries, not switches

A connector or parser is selected by declared capability. `registry.get("unknown")`
raises; it never falls back to something similar. A permissive registry is the same
failure as a permissive test fake — everything appears to work and nothing is proved.
