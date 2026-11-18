Status: active
Owner: operator
Last verified: 2026-11-18
Source of truth: `packages/python/lab_assistant/application/archive_fetch.py`

# Runbook: fetching a snapshot onto a reader

**When.** Setting up a reader installation, or refreshing one, from a library a
publisher has published. Design:
[ADR-0026](../../architecture/adr/0026-content-replication.md). Importing the fetched
snapshot into the reader's database is the next phase-3 step and is not a command yet.

## Fetch

```bash
lab archive fetch /srv/lab/snapshots/next --library-id <library-uuid>
```

Needs the archive address and GET on the library's keys, and nothing else: no
database, no write permission, no permission to list the bucket. It reads the head,
the manifest the head committed to, every shard and payload the manifest names, and
the current policy checkpoint, verifying each against its declared digest before
writing it. It finishes by validating the directory with its payloads, and exits
non-zero unless the result is complete and valid.

Set `LAB_ARCHIVE_TRUSTED_KEYS` first, or the signature is reported and not checked.

## When it fails

| Message | Meaning | Do |
|---|---|---|
| `published nothing` | the library has no head yet | wait for the publisher's first `lab archive publish --apply` |
| `not the one the head committed to` | the manifest object was replaced after publication | stop; treat it as a tampered archive and tell the publisher |
| `absent from the archive` | a payload or shard the snapshot names is missing | tell the publisher: the archive is incomplete |
| `digesting to …` | an object's bytes are wrong; they were not kept | tell the publisher; do not copy the object in by hand |
| a `storage_denied` error | this reader's credential may not read a key | fix the credential; the evidence is not missing |
| `already holds a different snapshot` | the directory is from an earlier fetch | fetch into a new directory |

An interrupted fetch — a dropped connection, a timeout — is resumed by running the same
command again on the same directory. Objects already verified are not fetched again.

## Withheld content

`"withheld"` counts payloads the publisher's rights decisions did not permit to leave
(`replicate_content`). That is not a fault. The reader gets those papers' records and
reviewed claims and cannot serve their text; whether a licence permits replication is
the data steward's decision, made on the publisher.

## Do not

- **Do not copy objects into the directory by hand** to get past a digest failure. The
  next run trusts what it finds when the digest matches, and nothing else.
- **Do not give a reader a write or list credential** to make a failure go away. A
  reader that can write can rewrite the library it serves.
- **Do not read a denial as missing data.** A `storage_denied` error is a credential
  problem; acquiring the paper again would be the wrong fix.
