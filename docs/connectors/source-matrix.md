Status: active
Owner: platform
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/adapters/connectors/scholarly.py`

# Source matrix

Rate limits and terms change. Each row links the source's own current guidance rather
than restating a number that will silently go stale.

| Connector | Operations | Asserts | Content | Terms |
|---|---|---|---|---|
| `pubmed` | discover, resolve | PMID, PMCID, DOI | metadata, abstract | [E-utilities policy](https://www.ncbi.nlm.nih.gov/books/NBK25497/) |
| `europepmc` | discover, resolve, acquire | PMID, PMCID, DOI | metadata, OA full text (JATS) | [Europe PMC](https://europepmc.org/RestfulWebService) |
| `crossref` | discover, resolve | DOI | metadata, licence statements | [Crossref REST](https://api.crossref.org/) |
| `openalex` | discover, resolve, delta | OpenAlex id, DOI, PMID | metadata, OA location | [OpenAlex](https://docs.openalex.org/) |
| `datacite` | discover, resolve | DataCite DOI | dataset metadata | [DataCite REST](https://support.datacite.org/docs/api) |
| `unpaywall` | resolve | DOI → OA location | OA route evidence | [Unpaywall](https://unpaywall.org/products/api) |
| `internal_upload` | acquire | content digest | assisted upload | workspace policy |

## Why these

**Crossref and OpenAlex are the licence-and-route authorities.** Unpaywall and Europe
PMC say *where* a permitted copy is. PubMed is the domain's discovery index. DataCite
covers deposited datasets, which a bioengineering lab cites as often as papers.

Their assertions **routinely disagree** — on year, on licence, on which DOI is
canonical. That is why observations are stored per source and canonical values record
which source won.

## Deliberately absent

**No connector scrapes a publisher page, evades a paywall, or ignores
`robots.txt`.** `robots_blocked` and `licence_not_permitted` are permanent outcomes.
When no permitted route exists, the item enters assisted acquisition and a human
supplies a copy they are entitled to. That is the whole design, not a limitation
awaiting a workaround.

## API keys

`NCBI_API_KEY` raises PubMed's rate ceiling; Unpaywall and Crossref want a contact
email; OpenAlex wants one in the user agent. `OPENALEX_API_KEY` is optional only for
searches of a single page: OpenAlex refuses cursor paging without a key, so a run
without one stops after the first page with `authentication_required`. Each is named
in `required_secret_names` when configured and supplied by environment variable
(`LAB_NCBI_API_KEY`, `LAB_OPENALEX_API_KEY`, `LAB_CONNECTOR_CONTACT`). A connector's
capabilities change with the credential present — a declared property, not hidden
behaviour.
