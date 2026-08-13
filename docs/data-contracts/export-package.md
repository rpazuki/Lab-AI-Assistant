Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/export.py`

# Export package

The wire format behind the [export guide](../product/export-guide.md).

## Layout

```text
<package>/
  bagit.txt
  manifest-sha256.txt
  ro-crate-metadata.json
  data/
    manifest.json
    datasheet.csv
    datasheet.parquet
    works.csv
    spans.jsonl
    README.md
```

BagIt for fixity, RO-Crate for archival tooling. Both are permissively licensed,
widely readable, and require no software from this project to interpret.

## `datasheet.csv` columns

Long form, one row per value:

```text
work_id, work_title, doi, pmid, year, target_id, target_label,
value, value_text, unit, uncertainty, operator,
entity_labels, experiment_conditions,
abstention_reason, confidence,
span_id, span_locator, span_quote,
review_state, reviewed_by, reviewed_at,
release_id, template_id, template_version
```

`abstention_reason` is present on every row, including reported ones. A consumer must
be able to distinguish "not reported" from "not searched" **without reading this
document**, because they will not read this document.

## `manifest.json`

The release manifest, plus: the export's own id and timestamp, the actor, the policy
version applied at export, the per-file sha256 digests, the row count, and
`withheld` — what was excluded and under which rule. An export that silently omits
restricted rows looks complete and is not.

## Validation

```python
validate_package(path)   # recomputes every digest, checks the manifest, raises on drift
```

Runs before the package is written, and can be re-run by the recipient with nothing
from this repository beyond the function — or by hand with `sha256sum -c`.

## Determinism

Rows are sorted by `(work_id, target_id, span_id)`. Floats are formatted through the
canonical number rule. Two exports of the same release with the same templates produce
identical bytes for `datasheet.csv` and `datasheet.parquet`; only `manifest.json`
differs, in its timestamp and export id.

## Injection safety

Every cell whose first character is `=`, `+`, `-`, `@`, tab, or carriage return is
prefixed with an apostrophe. A CSV that executes a formula in a colleague's
spreadsheet is a security bug.
