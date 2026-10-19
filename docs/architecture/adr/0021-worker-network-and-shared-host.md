Status: accepted
Owner: technical owner
Last verified: 2026-10-19
Applies to: production deployment and worker networking

# ADR-0021: Shared-host deployment and worker coordinator connectivity

## Context

The compute worker claims PostgreSQL queue tasks, reads and writes S3 objects,
and performs evaluation. `network_mode: none` disconnects these dependencies and
prevents work from completing. The current parser implementation is in-process;
there is no separate isolated per-document parser process. Shared hosts also need
ports that do not collide with other applications.

## Decision

Keep the compute coordinator on the Compose network. Publish only loopback ports
8100 (API) and 3300 (web), with host nginx terminating TLS on 8445. PostgreSQL and
ClamAV have no host port. Run ClamAV as a separate daemon with updated signatures
and a shared Unix socket, and stream document bytes to it.

This corrects an unimplemented isolation claim: the worker has outbound network
access. Non-root users, resource limits, upload scanning and application rights
checks remain, but are not a per-document network sandbox.

## Consequences

Production instructions must describe the actual isolation. The old application
and database remain separate; no legacy database migration is automatic. An S3
bucket and approved model credentials must be supplied before startup.

## Alternatives considered

Keep networking disabled: rejected because the queue and storage become unreachable.
A separate sandbox executor: deferred until implemented and verified against the
parser and object-transfer contracts.

## Validation / revisit trigger

Compose regression checks assert loopback-only bindings, no published database,
scanner wiring, and compute connectivity. Revisit when a per-document sandbox is
implemented; do not disable coordinator networking to simulate it.
