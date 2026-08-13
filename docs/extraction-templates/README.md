Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `configs/examples/templates/`, `packages/python/lab_assistant/contracts/extraction.py`

# Extraction templates

A template is **workspace data**, with a stable id and a semantic version. No domain
template is compiled into core code as an unexplained default — the predecessor's
yeast-lipid schema lived in the extractor, so changing a target meant a code release,
and two different template revisions shared a cache key.

## Required parts

| Part | Why it is mandatory |
|---|---|
| `template_id`, `semantic_version` | the fingerprint includes both. `1` alone is not a version. |
| `title`, `scientific_purpose` | what question the sheet answers |
| `exclusions` | what it is **not** for. Templates get misused when this is left blank. |
| `cardinality` | `per_work`, `per_experiment`, or `per_entity` — the row grain |
| `evidence_standard` | what counts as evidence; normally an exact span under normalisation v1 |
| `reviewer_instructions` | what a reviewer should check beyond the number |
| `targets` | the fields |

## Target fields

`target_id`, `predicate`, `object_kind` (`quantity`, `text`, `entity`, `boolean`,
`categorical`), `description`, `unit_expectation`, `controlled_vocabulary`,
`likely_sections`, `required`, `deterministic_metadata`.

**`deterministic_metadata: true` means a model may not propose it.** A DOI comes from
resolved source assertions. A model proposing a DOI is rejected at validation, because a
plausible invented identifier is worse than a missing one.

`likely_sections` steers evidence selection, so a titre is sought in Results and tables
rather than in a Discussion sentence recalling someone else's paper.

## Versioning

A new target or a changed unit expectation is a new semantic version. The version is in
the extraction fingerprint, so previous results are neither reused nor silently
invalidated — they remain attributable to the version that produced them, and a release
records which versions it used.

## Wide sheets

The wide, one-column-per-target sheet is generated **from a release** and is documented
as a derived view. Long form is the contract; wide form is a convenience that loses
conditions the moment two measurements exist for one work.

## Writing one

Start from [the yeast lipid datasheet](yeast-lipid-datasheet.md), then:

```bash
python tools/cli/validate_examples.py
```

Validation checks the schema, that every target has a description, that quantity
targets declare a unit expectation, and that controlled vocabularies are non-empty.
