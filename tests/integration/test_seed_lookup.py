"""Organism and product lookup: NCBI Taxonomy and PubChem, through a mock transport.

The payloads are trimmed copies of the upstream shapes. No test here reaches the
network: a lookup that needed it would make the suite depend on NCBI's uptime.
"""

from __future__ import annotations

import httpx
import pytest

from lab_assistant.adapters.connectors.seed_lookup import (
    LookupUnavailable,
    SeedLookupService,
    build_queries,
    parse_taxon_xml,
)
from lab_assistant.contracts.errors import PolicyDenied, ValidationFailure
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor

TAXON_XML = """<?xml version="1.0"?>
<TaxaSet><Taxon>
  <TaxId>4952</TaxId>
  <ScientificName>Yarrowia lipolytica</ScientificName>
  <OtherNames>
    <Synonym>Candida lipolytica</Synonym>
    <Synonym>Endomycopsis lipolytica</Synonym>
    <Anamorph>Candida lipolytica</Anamorph>
    <Authority>Yarrowia lipolytica (Wick. et al.) van der Walt</Authority>
    <GenbankCommonName>oleaginous yeast</GenbankCommonName>
  </OtherNames>
  <Rank>species</Rank>
  <Lineage>cellular organisms; Eukaryota; Fungi; Yarrowia</Lineage>
</Taxon></TaxaSet>"""


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "esearch.fcgi" in url:
        return httpx.Response(200, json={"esearchresult": {"idlist": ["4952"]}})
    if "esummary.fcgi" in url:
        return httpx.Response(
            200,
            json={"result": {"4952": {"scientificname": "Yarrowia lipolytica", "rank": "species"}}},
        )
    if "efetch.fcgi" in url:
        return httpx.Response(200, text=TAXON_XML)
    if "/autocomplete/compound/" in url:
        return httpx.Response(
            200, json={"dictionary_terms": {"compound": ["oleic acid", "oleate"]}}
        )
    if "/compound/name/" in url:
        return httpx.Response(200, json={"IdentifierList": {"CID": [445639]}})
    if "/property/" in url:
        return httpx.Response(
            200,
            json={
                "PropertyTable": {
                    "Properties": [
                        {"MolecularFormula": "C18H34O2", "InChIKey": "ZQPPMHVWECSIRJ-KTKRTIGZSA-N"}
                    ]
                }
            },
        )
    if "/synonyms/" in url:
        return httpx.Response(
            200,
            json={
                "InformationList": {
                    "Information": [
                        {
                            "Synonym": [
                                "oleic acid",
                                "112-80-1",
                                "cis-9-Octadecenoic acid",
                                "Oleate",
                                "CHEBI:16196",
                                "DTXSID1025809",
                                "AB1234567",
                            ]
                        }
                    ]
                }
            },
        )
    return httpx.Response(404)


@pytest.fixture
async def lookup():
    harness = build_harness()
    user = await harness.accounts.create_user(email="s@lab.invalid", password="a-long-passphrase")
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
    )
    actor = make_actor(user_id=user.id, workspace_id=workspace.id, role=Role.DATA_STEWARD)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    yield {"service": SeedLookupService(harness.context, client=client), "actor": actor}
    await client.aclose()


def test_a_taxon_keeps_synonyms_and_drops_authorities() -> None:
    seed = parse_taxon_xml(TAXON_XML)
    assert seed is not None
    assert seed.search_terms == (
        "Yarrowia lipolytica",
        "Candida lipolytica",
        "Endomycopsis lipolytica",
        "oleaginous yeast",
    )
    assert seed.lineage[-1] == "Yarrowia"


def test_an_empty_taxa_set_is_no_seed() -> None:
    assert parse_taxon_xml("<TaxaSet/>") is None


class TestLookups:
    async def test_organism_typeahead_and_resolution(self, lookup) -> None:
        service, actor = lookup["service"], lookup["actor"]
        [suggestion] = await service.suggest_organisms(actor, "Yarrowia lipo")
        assert suggestion["taxid"] == 4952
        seed = await service.resolve_organism(actor, query="candida lipolytica")
        assert seed is not None and seed.scientific_name == "Yarrowia lipolytica"

    async def test_a_product_keeps_names_and_drops_registry_codes(self, lookup) -> None:
        service, actor = lookup["service"], lookup["actor"]
        assert [item["name"] for item in await service.suggest_products(actor, "olei")] == [
            "oleic acid",
            "oleate",
        ]
        seed = await service.resolve_product(actor, query="oleic acid")
        assert seed is not None and seed.cid == 445639
        assert seed.search_terms == ("oleic acid", "cis-9-Octadecenoic acid", "Oleate")

    async def test_an_upstream_failure_is_reported_not_raised_raw(self, lookup) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
        service = SeedLookupService(lookup["service"]._context, client=client)
        with pytest.raises(LookupUnavailable, match="503"):
            await service.suggest_organisms(lookup["actor"], "Yarrowia")
        await client.aclose()

    async def test_a_researcher_cannot_look_up(self, lookup) -> None:
        researcher = make_actor(
            user_id=lookup["actor"].user_id,
            workspace_id=lookup["actor"].workspace_id,
            role=Role.RESEARCHER,
        )
        with pytest.raises(PolicyDenied):
            await lookup["service"].suggest_organisms(researcher, "Yarrowia")


class TestQueries:
    def test_each_source_gets_its_own_dialect(self) -> None:
        queries = build_queries(
            ["Yarrowia lipolytica", "Candida lipolytica"], ["oleic acid", "Oleate"]
        )
        assert queries["pubmed"] == (
            '("Yarrowia lipolytica"[Title/Abstract] OR "Candida lipolytica"[Title/Abstract])'
            ' AND ("oleic acid"[Title/Abstract] OR "Oleate"[Title/Abstract])'
        )
        assert queries["europepmc"].startswith('(TITLE_ABS:"Yarrowia lipolytica" OR ')
        assert queries["openalex"] == (
            '("Yarrowia lipolytica" OR "Candida lipolytica") AND ("oleic acid" OR "Oleate")'
        )
        assert queries["crossref"] == "Yarrowia lipolytica oleic acid"

    def test_an_organism_alone_is_enough(self) -> None:
        assert build_queries(["Yarrowia lipolytica"], [])["pubmed"] == (
            '("Yarrowia lipolytica"[Title/Abstract])'
        )

    def test_no_names_is_refused(self) -> None:
        with pytest.raises(ValidationFailure):
            build_queries([" "], [])
