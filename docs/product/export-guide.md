Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/export.py`

# Export guide

An export is a **package**, not a file. A spreadsheet without provenance is a
liability: nobody can tell six months later which release, which policy, or which
evidence produced a number.

## What a package contains

| Member | Contents |
|---|---|
| `datasheet.csv` | long form: one row per value, with target, unit, conditions, work, span id, reviewer, confidence |
| `datasheet.parquet` | the same table, typed |
| `manifest.json` | release id and digest, scope version, template versions, policy version, embedding profile, row count, per-file digests |
| `works.csv` | one row per work with identifiers and rights verdict |
| `spans.jsonl` | every cited span with its normalised quote and locator |
| `README.md` | how to cite this package, and what it excludes |
| `ro-crate-metadata.json` | RO-Crate metadata for archival tooling |
| `bagit.txt`, `manifest-sha256.txt` | BagIt fixity |

`validate_package` re-computes every digest and confirms the manifest matches. It runs
before the package is written and can be run again by the recipient.

## Long form, not wide

One row per value, not one column per target. Wide form quietly loses conditions and
becomes ambiguous the moment two measurements of the same target exist for one work.

## Rights are enforced at export

Full text does not leave in a package unless the licence permits redistribution.
Metadata-only material exports its metadata, its citation, and the *locator* of its
evidence — not the passage. The manifest states what was withheld and why, so an
incomplete export is visibly incomplete.

## Spreadsheet safety

Cells beginning `=`, `+`, `-`, `@`, tab, or carriage return are prefixed with an
apostrophe. A CSV that runs a formula when a colleague opens it in Excel is a security
bug, not a formatting quirk.

## Reproducibility

Two exports of the same release with the same template versions produce byte-identical
CSV and Parquet. Timestamps live in the manifest, not in the data.

## Citing an export

Cite the release id and manifest digest. They are stable; "the export from March" is
not.
