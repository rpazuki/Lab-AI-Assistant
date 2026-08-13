Status: active
Owner: steward + operator
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/releases.py`

# Runbook: release rollback

**Symptom.** A published release is wrong — a bad claim got through, a template defect
is visible in answers, or a retracted paper was included.

## Roll back the alias

```bash
lab release rollback --workspace <slug> --actor <user-id> --alias current
```

The alias repoints to the previous release. The previous release's projections were
never deleted, so this is a pointer change: seconds, not a rebuild. It is audited with
actor and reason.

Answers already given are **not** rewritten. They record the release they came from,
which is what makes them reproducible; a snapshot that changed retroactively would be
worthless.

## Then

1. tell the members. An answer they read this morning may have cited the withdrawn
   release — that is legitimate and should be stated plainly rather than discovered.
2. withdraw the release so it cannot be re-published by accident. Withdrawn is
   terminal, and it refuses while an alias still points at the release — roll the alias
   back first, which is step 1:

```bash
lab release withdraw --workspace <slug> --actor <user-id> --release-id <id>
```
3. fix the cause: the claim, the template version, or the exclusion.
4. build and validate a new release. `publish_blockers` lists everything at once.
5. republish. The alias moves forward, and the withdrawn release keeps its history.

## For a retraction specifically

```bash
lab release retract-work --workspace <slug> --actor <user-id> --work-id <id> \
  --reason publisher_retraction_notice
```

Records the retraction, flags every answer that cited the work — across all members,
not only yours — and excludes it from the next release. It does **not** mutate the
published release.

## Do not

- **Do not delete the bad release's projections.** They are what makes rollback fast,
  and re-publishing to fix a mistake made under pressure is exactly when you want the
  old one intact.
- **Do not fix a claim in place in a published release.** Immutability is the property
  every citation depends on. Correct it, then publish a new release.
- **Do not roll back the application to fix a corpus problem.** They are independent;
  conflating them makes the incident larger.
