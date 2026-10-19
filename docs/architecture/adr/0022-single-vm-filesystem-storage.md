Status: accepted
Owner: technical owner + operator
Last verified: 2026-10-19
Applies to: single-VM object storage

# ADR-0022: Permit persistent filesystem storage on one VM

## Context

The experimental VM deployment has no institutional S3 service available. An
external object service is not necessary to preserve content addressing and
workspace isolation. However, the old filesystem adapter cached its registry at
startup, so separate API/worker processes could not see newly registered objects.

## Decision

Amend the production storage policy described in [ADR-0017](0017-object-storage-adapter.md):
keep S3 available, and permit filesystem storage with explicit
`allow_filesystem_in_production`. The shared-VM profile enables it and mounts the
same persistent `objects` volume in API and workers. Coordinate registry access
with POSIX file locks and incremental registry refresh, atomically publish content
files, flush registry writes, and verify digests on reads. Existing JSONL registries
remain readable. All containers use UID 10001 for shared files.

## Consequences

No S3 endpoint or credentials are required. VM/disk failure can lose documents
and database together. Consistent backups must include PostgreSQL and the entire
objects volume and be copied off the VM. Container recreation is not disk failure;
the named volume survives it. Do not delete volumes during updates.
This is a one-VM POSIX filesystem profile, not a distributed filesystem protocol.
Switching adapters does not migrate existing objects.

## Alternatives considered

Mandatory S3 prevents the intended installation. Removing the storage layer would
lose the immutable document/citation contract. Running another object server on
the same VM adds operations without removing the VM's failure domain.

## Validation / revisit trigger

Tests exercise two live instances, concurrent process writes/deduplication,
tombstone visibility, digest corruption and explicit server opt-in. Revisit if
multi-host operation, registry size, or recovery objectives exceed this profile.
