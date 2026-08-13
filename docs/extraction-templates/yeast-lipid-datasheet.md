Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `configs/examples/templates/yeast-lipid-datasheet.yaml`

# Yeast lipid datasheet

The worked example: per-experiment lipid yields from engineered yeast, with the culture
conditions they were measured under. Its purpose is strain comparison across the
literature.

`cardinality: per_experiment`. One paper reporting three carbon sources produces three
rows, not one row with an averaged number.

## Targets

| Target | Kind | Unit | Notes |
|---|---|---|---|
| `strain` | entity | – | required |
| `lipid_yield` | quantity | `g/g` | required; **basis matters** |
| `lipid_titre` | quantity | `g/L` | |
| `culture_temperature` | quantity | degrees C | |
| `carbon_source` | text | – | controlled vocabulary |
| `doi` | text | – | `deterministic_metadata: true` |

## The trap this template exists to document

A yield "per gram substrate" and one "per gram dry cell weight" are both `g/g` and are
**not the same measurement**. The reviewer instructions say so explicitly, and the
evidence span must contain the basis. A number whose basis cannot be quoted is an
abstention.

Second trap: values reported only in a figure, with no numeric caption. Excluded by the
template. A model reading an axis label produces a number nobody can check.

## Not for

Pathway-level flux estimates. Figure-only values. Anything requiring a unit conversion
the paper did not itself report.

## Evidence standard

An exact span in focal content under normalisation version 1. Focal content means the
work's own Results, Methods, and tables — not its Introduction recounting another
paper's result, and not its reference list.

## Try it offline

```bash
lab demo
```

The demo publishes a small corpus in process — rendition, release gates, projections,
alias — then asks a question and **opens the citation it returns**, printing the exact
evidence. It exits non-zero if a citation does not resolve. No network, no database, no
model provider.

The full lifecycle, including this template's extraction and review, is exercised by
`tests/e2e/test_acceptance_scenario.py`.
