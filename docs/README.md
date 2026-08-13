# Documentation

This index is the map. Every page below is reachable from here — an orphan page is
a page nobody maintains, and CI fails on one.

Each non-evergreen page carries a status header (status, owner, last verified) and
names its source of truth. Where a page and the code disagree, the code wins and the
page is a bug.

## By audience

| Audience | Start here |
|---|---|
| Scientist or researcher | [Product overview](product/overview.md) · [Asking and citations](product/asking-and-citations.md) · [Review workflows](product/review-workflows.md) |
| Data steward | [Collections and releases](product/collections-and-releases.md) · [Export guide](product/export-guide.md) · [User roles](product/user-roles.md) |
| Developer | [System context](architecture/system-context.md) · [Modules and dependencies](architecture/modules-and-dependencies.md) · [Task orchestration](architecture/task-orchestration.md) · [Search and RAG](architecture/search-and-rag.md) · [Provenance](architecture/provenance.md) |
| Connector owner | [Connector SDK](connectors/README.md) · [Source matrix](connectors/source-matrix.md) |
| Operator | [Deployment](operations/deployment.md) · [Monitoring](operations/monitoring.md) · [Support bundles](operations/support-bundles.md) · [Runbooks](operations/runbooks/README.md) |
| Security or privacy reviewer | [Threat model](security/threat-model.md) · [Data inventory](security/data-inventory.md) · [Model transfer policy](security/model-transfer.md) |
| Anyone joining mid-migration | [Migration overview](migration/README.md) · [API compatibility map](migration/api-compatibility.md) · [Remaining implementation tasks](migration/remaining-implementation-tasks.md) |

## Data contracts

The vocabulary is precise, and these pages are the definitions:

- [Terminology](data-contracts/terminology.md)
- [Source observations](data-contracts/source-observations.md)
- [Work identity](data-contracts/work-identity.md)
- [Assets and rights](data-contracts/assets-and-rights.md)
- [Renditions and spans](data-contracts/renditions-and-spans.md)
- [Entities, experiments, claims](data-contracts/entities-experiments-claims.md)
- [Corpus release](data-contracts/corpus-release.md)
- [Export package](data-contracts/export-package.md)
- [State and error taxonomy](data-contracts/state-and-error-taxonomy.md)

## Decisions

[Architecture decision records](architecture/adr/README.md) — one per durable
decision. An accepted ADR is immutable except for status and links; a changed
decision is a new superseding ADR.

## Evaluation

- [Methodology](evaluation/methodology.md)
- [Release gates](evaluation/release-gates.md)

## Extraction templates

- [Template documentation rules](extraction-templates/README.md)
- [Yeast lipid datasheet](extraction-templates/yeast-lipid-datasheet.md)

## Contributing

- [Review ownership](contributing/review-ownership.md)
- [Root CONTRIBUTING.md](../CONTRIBUTING.md)
