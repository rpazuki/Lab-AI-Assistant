Status: active
Owner: operator
Last verified: 2026-08-12
Source of truth: `scripts/status.sh`, `docker-compose.yml`

# Runbook: disk pressure

**Symptom.** Under 15% free. PostgreSQL and the object store share the disk, so this
becomes data loss faster than any other failure in this system.

```bash
./scripts/status.sh
```

## Find it, in this order

1. **Container logs.** Rotation is configured (20 MB × 5 per service) by the
   `x-logging` anchor. If logs are large anyway, the anchor was removed from a service —
   put it back. Docker's default driver never rotates.
2. **WAL.** If archiving is failing, WAL accumulates and will fill the disk completely.
   Fix archiving; do not delete WAL.
3. **The object store.** Renditions and model envelopes for a large corpus. Real growth.
4. **Old export packages.** Deletable, by a steward, once collaborators have them.
5. **Superseded releases' projections.** Deletable for releases below the retained
   window — but keep at least the previous published release, because that is what
   rollback needs.

## Reclaim safely

```bash
docker system prune -f --filter "until=168h"
```

Removes unused images and build cache. It does not touch named volumes.

## Do not

- **Do not delete WAL files.** Point-in-time recovery is gone, and if the disk fills
  during the deletion, so is the database.
- **Do not `docker volume prune`.** `pgdata` and `objects` are named volumes, and one
  mistyped filter is the whole system.
- **Do not delete objects a published release references.** Every citation into that
  release breaks, and `verify-restore` will fail afterwards. Check first:

```bash
lab release references --workspace <slug> --digest <sha256>
```

  It reports every release citing the digest, whether any alias still serves one, and
  exits non-zero when deletion is unsafe — so a cleanup script can gate on it.
- **Do not truncate a live log file** an active process holds — the space is not
  returned and the file descriptor keeps writing at the old offset.
