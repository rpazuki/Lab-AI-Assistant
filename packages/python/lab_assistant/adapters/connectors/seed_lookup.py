"""Organism and product lookup, for writing a scope's source queries.

Ported from the predecessor's datasheet wizard. What discovery needs is not the
name a steward types but the set of names the literature uses: searching for
*Yarrowia lipolytica* alone misses every paper published as *Candida lipolytica*.

* **Organisms** come from NCBI Taxonomy: a typeahead over names and synonyms
  (``esearch`` + ``esummary``), then the full name block of the chosen taxon
  (``efetch``). A synonym resolves to its accepted taxon, so two spellings give one
  seed.
* **Products** come from PubChem: autocomplete, then the compound's synonyms with
  registry codes (CAS numbers, vendor catalogue codes) filtered out, since they add
  noise to a literature query without adding recall.
* **Queries** are then written per source, each in its own dialect, AND-ing the
  organism group with the product group where the source can express it.

The lookup only proposes. The steward chooses which names to keep, edits the
queries if they like, and the scope version records exactly what was searched.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError, ValidationFailure
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.workspace import Capability

__all__ = [
    "OrganismSeed",
    "ProductSeed",
    "SeedLookupService",
    "build_queries",
    "parse_taxon_xml",
]

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUBCHEM_AUTOCOMPLETE = "https://pubchem.ncbi.nlm.nih.gov/rest/autocomplete/compound"

_USABLE_NAME_TAGS = (
    "Synonym",
    "GenbankSynonym",
    "EquivalentName",
    "Anamorph",
    "Teleomorph",
    "Includes",
)
_COMMON_NAME_TAGS = ("CommonName", "GenbankCommonName")
_DIGITS = re.compile(r"^\d+$")
_MAX_SYNONYMS = 40
_REGISTRY_PATTERNS = (
    re.compile(r"^\d{2,7}-\d{2}-\d$"),
    re.compile(r"^(cid|sid)\s*\d+$", re.I),
    re.compile(r"^(dtxsid|dtxcid|chembl|chebi|unii|hms|mfcd|nsc|akos|schembl)", re.I),
    re.compile(r"^[a-z]{1,4}[-_ ]?\d{4,}$", re.I),
    re.compile(r"^[0-9A-Z]{10}$"),
)


class LookupUnavailable(LabAssistantError):
    """The upstream did not answer usefully; the steward can still type names."""

    error_class = ErrorClass.SOURCE_UNAVAILABLE
    http_status = 502


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        cleaned = " ".join(value.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            kept.append(cleaned)
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class OrganismSeed:
    taxid: int
    scientific_name: str
    rank: str | None = None
    synonyms: tuple[str, ...] = ()
    common_names: tuple[str, ...] = ()
    lineage: tuple[str, ...] = ()

    @property
    def search_terms(self) -> tuple[str, ...]:
        """Accepted name first, then synonyms, then common names."""
        return _dedupe([self.scientific_name, *self.synonyms, *self.common_names])

    def as_dict(self) -> dict[str, Any]:
        return {
            "taxid": self.taxid,
            "scientific_name": self.scientific_name,
            "rank": self.rank,
            "synonyms": list(self.synonyms),
            "common_names": list(self.common_names),
            "lineage": list(self.lineage),
            "search_terms": list(self.search_terms),
        }


@dataclass(frozen=True, slots=True)
class ProductSeed:
    cid: int
    preferred_name: str
    synonyms: tuple[str, ...] = ()
    molecular_formula: str | None = None
    inchikey: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def search_terms(self) -> tuple[str, ...]:
        return _dedupe([self.preferred_name, *self.synonyms])

    def as_dict(self) -> dict[str, Any]:
        return {
            "cid": self.cid,
            "preferred_name": self.preferred_name,
            "synonyms": list(self.synonyms),
            "molecular_formula": self.molecular_formula,
            "inchikey": self.inchikey,
            "search_terms": list(self.search_terms),
        }


def parse_taxon_xml(xml_text: str) -> OrganismSeed | None:
    """One ``efetch`` TaxaSet into a seed; ``None`` for an empty set.

    NCBI answers an unknown taxid with a valid, empty document rather than an error.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise LookupUnavailable(f"NCBI Taxonomy returned unreadable XML: {exc}") from exc
    taxon = root.find("Taxon")
    if taxon is None:
        return None
    raw_taxid = (taxon.findtext("TaxId") or "").strip()
    name = (taxon.findtext("ScientificName") or "").strip()
    if not _DIGITS.match(raw_taxid) or not name:
        return None
    synonyms: list[str] = []
    common: list[str] = []
    other = taxon.find("OtherNames")
    if other is not None:
        for tag in _USABLE_NAME_TAGS:
            synonyms.extend((element.text or "").strip() for element in other.findall(tag))
        for tag in _COMMON_NAME_TAGS:
            common.extend((element.text or "").strip() for element in other.findall(tag))
    return OrganismSeed(
        taxid=int(raw_taxid),
        scientific_name=name,
        rank=(taxon.findtext("Rank") or "").strip() or None,
        synonyms=_dedupe([value for value in synonyms if value != name]),
        common_names=_dedupe(common),
        lineage=tuple(
            part.strip() for part in (taxon.findtext("Lineage") or "").split(";") if part.strip()
        ),
    )


def _clean_product_synonyms(raw: Sequence[str], *, preferred: str) -> tuple[str, ...]:
    seen = {preferred.casefold()}
    kept: list[str] = []
    for value in raw:
        cleaned = " ".join(value.split())
        if not cleaned or len(cleaned) > 120 or cleaned.casefold() in seen:
            continue
        if any(pattern.match(cleaned) for pattern in _REGISTRY_PATTERNS):
            continue
        seen.add(cleaned.casefold())
        kept.append(cleaned)
        if len(kept) >= _MAX_SYNONYMS:
            break
    return tuple(kept)


# ── per-source queries ──────────────────────────────────────────────────────
def _phrase(term: str) -> str:
    return '"' + term.replace('"', " ").strip() + '"'


def build_queries(organism_terms: Sequence[str], product_terms: Sequence[str]) -> dict[str, str]:
    """One query per registered source, each in that source's own dialect.

    Organism and product groups are AND-ed where the source has real boolean
    search. Crossref has none: its bibliographic search is fuzzy and ranked, so it
    gets the accepted organism and product names as plain words, and discovery's
    relevance step separates the matches.
    """
    groups = [list(_dedupe(terms)) for terms in (organism_terms, product_terms)]
    groups = [terms for terms in groups if terms]
    if not groups:
        raise ValidationFailure("choose at least one organism or product name")

    def joined(render: Any) -> str:
        clauses = ["(" + " OR ".join(render(term) for term in terms) + ")" for terms in groups]
        return " AND ".join(clauses)

    return {
        "pubmed": joined(lambda term: f"{_phrase(term)}[Title/Abstract]"),
        "europepmc": joined(lambda term: f"TITLE_ABS:{_phrase(term)}"),
        "openalex": joined(_phrase),
        "datacite": joined(_phrase),
        "crossref": " ".join(terms[0] for terms in groups),
    }


class SeedLookupService:
    """Network lookups for the scope form. Only curators reach the internet."""

    def __init__(self, context: ServiceContext, *, client: httpx.AsyncClient | None = None) -> None:
        self._context = context
        self._client = client

    def _require(self, actor: ActorContext) -> None:
        self._context.require_installation(InstallationCapability.ACQUIRE_SOURCES)
        actor.require(Capability.MANAGE_SCOPE)

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response | None:
        settings = self._context.settings
        headers = {"User-Agent": settings.connector_request_identity}
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.get(
                url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise LookupUnavailable(f"{url.split('/')[2]} did not answer: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise LookupUnavailable(
                f"{url.split('/')[2]} answered {response.status_code}; try again shortly"
            )
        return response

    def _ncbi(self) -> dict[str, Any]:
        settings = self._context.settings
        return {"email": settings.connector_contact, "api_key": settings.ncbi_api_key}

    # ── organisms ───────────────────────────────────────────────────────────
    async def _taxids(self, term: str, *, limit: int) -> list[int]:
        response = await self._get(
            f"{EUTILS_BASE}/esearch.fcgi",
            {"db": "taxonomy", "term": term, "retmode": "json", "retmax": limit, **self._ncbi()},
        )
        result = (response.json() if response else {}).get("esearchresult") or {}
        return [int(value) for value in result.get("idlist", []) if _DIGITS.match(str(value))]

    async def suggest_organisms(
        self, actor: ActorContext, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        self._require(actor)
        cleaned = query.strip()
        if len(cleaned) < 2:
            return []
        # NCBI matches whole names: the wildcard is what lets "Yarrowia lipo" match.
        taxids = await self._taxids(cleaned if "*" in cleaned else f"{cleaned}*", limit=limit)
        if not taxids:
            return []
        response = await self._get(
            f"{EUTILS_BASE}/esummary.fcgi",
            {"db": "taxonomy", "id": ",".join(map(str, taxids)), "retmode": "json", **self._ncbi()},
        )
        result = (response.json() if response else {}).get("result") or {}
        suggestions = []
        for taxid in taxids:  # keep esearch's ranking
            record = result.get(str(taxid))
            if isinstance(record, dict) and (record.get("scientificname") or "").strip():
                suggestions.append(
                    {
                        "taxid": taxid,
                        "scientific_name": record["scientificname"].strip(),
                        "rank": (record.get("rank") or "").strip() or None,
                        "common_name": (record.get("commonname") or "").strip() or None,
                    }
                )
        return suggestions

    async def resolve_organism(
        self, actor: ActorContext, *, query: str | None = None, taxid: int | None = None
    ) -> OrganismSeed | None:
        self._require(actor)
        if taxid is None:
            cleaned = (query or "").strip()
            if not cleaned:
                return None
            if _DIGITS.match(cleaned):
                taxid = int(cleaned)
            else:
                found = await self._taxids(cleaned, limit=1) or await self._taxids(
                    f"{cleaned}*", limit=1
                )
                if not found:
                    return None
                taxid = found[0]
        response = await self._get(
            f"{EUTILS_BASE}/efetch.fcgi",
            {"db": "taxonomy", "id": taxid, "retmode": "xml", **self._ncbi()},
        )
        return parse_taxon_xml(response.text) if response else None

    # ── products ────────────────────────────────────────────────────────────
    async def suggest_products(
        self, actor: ActorContext, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        self._require(actor)
        cleaned = query.strip()
        if len(cleaned) < 2:
            return []
        response = await self._get(
            f"{PUBCHEM_AUTOCOMPLETE}/{quote(cleaned, safe='')}/json", {"limit": limit}
        )
        terms = ((response.json() if response else {}).get("dictionary_terms") or {}).get(
            "compound"
        ) or []
        return [{"name": str(term), "cid": None} for term in terms[:limit]]

    async def resolve_product(
        self, actor: ActorContext, *, query: str | None = None, cid: int | None = None
    ) -> ProductSeed | None:
        self._require(actor)
        typed = (query or "").strip()
        if cid is None:
            if not typed:
                return None
            if _DIGITS.match(typed):
                cid = int(typed)
            else:
                response = await self._get(
                    f"{PUBCHEM_BASE}/compound/name/{quote(typed, safe='')}/cids/JSON"
                )
                cids = ((response.json() if response else {}).get("IdentifierList") or {}).get(
                    "CID"
                ) or []
                if not cids:
                    return None
                cid = int(cids[0])
        properties = await self._get(
            f"{PUBCHEM_BASE}/compound/cid/{cid}/property/MolecularFormula,InChIKey/JSON"
        )
        records = ((properties.json() if properties else {}).get("PropertyTable") or {}).get(
            "Properties"
        ) or []
        if not records:
            return None
        synonyms_response = await self._get(f"{PUBCHEM_BASE}/compound/cid/{cid}/synonyms/JSON")
        information = (
            (synonyms_response.json() if synonyms_response else {}).get("InformationList") or {}
        ).get("Information") or []
        raw = [str(value) for value in (information[0].get("Synonym") if information else []) or []]
        # PubChem's first synonym is its preferred name; the typed one is a fallback.
        preferred = (raw[0] if raw else None) or typed or f"CID {cid}"
        return ProductSeed(
            cid=cid,
            preferred_name=preferred,
            synonyms=_clean_product_synonyms(raw, preferred=preferred),
            molecular_formula=records[0].get("MolecularFormula"),
            inchikey=records[0].get("InChIKey"),
        )
