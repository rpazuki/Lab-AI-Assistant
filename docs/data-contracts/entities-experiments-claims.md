Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/claims.py`

# Entities, experiments, and claims

## Claims

A claim states: **this work reported this value for this target, under these
conditions, and here is the span that says so.**

| Field | Notes |
|---|---|
| `work_id`, `release_scoped` | what it is about |
| `target_id` | which template target it answers |
| `value` | a `NumericValue`, text, boolean, or categorical |
| `unit` | canonical unit; the source's own string is kept too |
| `entity_ids`, `experiment_id` | strain, construct, substrate; conditions |
| `evidence_span_ids` | at least one. A claim with no span cannot be created. |
| `abstention_reason` | why there is no value, when there is none |
| `confidence`, `extractor_fingerprint` | how it was produced |
| `review_state`, `revisions` | how it came to be trusted |

## Numbers

`parse_numeric` handles what papers actually print: `12.3 ± 0.4`, `1.2e3`, ranges
(`10–15`), inequalities (`<0.05`), percentages, and thousands separators. It produces a
value with optional uncertainty, bounds, and a comparison operator — and **fails
loudly** on something it does not understand, rather than returning the first float it
can find. `0.05` from `p<0.05` is not the same fact.

## Comparability

```python
left.comparable_with(right)  # same dimension, convertible units, compatible operators
```

Two claims conflict only when they are comparable *and* disagree. `1.2 g/L` and
`1200 mg/L` agree; `12 g/L` on glucose and `4 g/L` on glycerol do not conflict at all —
they are different experiments. Comparability is a domain rule so that neither the
model nor each individual reviewer has to re-derive it.

## Abstention

A missing value has a reason, and only two reasons support a statement about what the
source says:

- `reported` — there is a value;
- `not_reported_after_complete_search` — the source genuinely does not report it.

Everything else (`not_searched_budget`, `not_searched_policy`, `source_unavailable`,
`parse_quality_insufficient`, `ambiguous_multiple_values`, `conflicting_values`,
`extractor_abstained`, `not_applicable`, `policy_redacted`) means **we did not
establish it** — and must never be exported or displayed as "the paper does not report
this". That conflation is the single most damaging error a datasheet can contain.

## Entities and experiments

Entities are strains, constructs, organisms, substrates, and compounds, with
identifiers where a registry exists and free text where none does. Experiments carry
the conditions: substrate, medium, temperature, duration, scale, mode.

Both are shared across claims, so "every lipid titre measured on glycerol" is a query
rather than a text search.

## Release eligibility

`claim_release_blockers` names every reason a claim cannot enter a release: unreviewed,
unverified span, rendition below quality threshold, missing rights decision,
unrecognised unit, unresolved conflict. All of them at once.
