"""Model provider and embedding contracts.

Two provider behaviours are load-bearing and must be contract-tested (spike S7):
structured output that actually adheres to a schema, and citation handles that
the server supplied. A model returning a plausible but unsupplied handle is a
first-class outcome — a flagged answer — not a 500.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "EmbeddingProfile",
    "EmbeddingProvider",
    "EvidenceBlock",
    "GenerationRequest",
    "GenerationResult",
    "LLMProvider",
    "ProviderProperties",
    "StreamUsage",
    "StructuredRequest",
    "StructuredResult",
    "VectorIndexKind",
]


class VectorIndexKind(StrEnum):
    HNSW = "hnsw"
    IVFFLAT = "ivfflat"
    NONE = "none"


class EmbeddingProfile(BaseModel):
    """Named, versioned embedding configuration.

    The profile is part of what makes a release reproducible, so index type and
    parameters live here rather than in a migration (spike S4).
    """

    model_config = {"frozen": True}

    profile_id: str
    provider: str
    model: str
    model_revision: str
    dimensions: int
    normalisation: str = "l2"
    #: True when producing these vectors sends content to an external service,
    #: which is a distinct permitted action from local embedding.
    external: bool = False
    index_kind: VectorIndexKind = VectorIndexKind.HNSW
    index_parameters: dict[str, int] = Field(
        default_factory=lambda: {"m": 16, "ef_construction": 64}
    )
    storage_type: str = "vector"  # vector | halfvec

    @property
    def table_name(self) -> str:
        """Vectors of different dimensions never share a column."""
        return f"chunk_embeddings_{self.dimensions}"


class ProviderProperties(BaseModel):
    """What a provider adapter declares about itself (connectors §9)."""

    model_config = {"frozen": True}

    provider_id: str
    supports_streaming: bool = True
    supports_structured_output: bool = True
    supports_batch: bool = False
    supports_seed: bool = False
    #: Sampling parameters this provider/model accepts. Claude 5 models reject
    #: temperature/top_p/top_k with a 400 rather than ignoring them, so the
    #: adapter decides per target model (legacy mistakes.md, spike S7).
    accepted_sampling_params: frozenset[str] = frozenset()
    retains_data: bool = False
    trains_on_data: bool = False
    regions: tuple[str, ...] = ()
    max_input_tokens: int = 200_000


class EvidenceBlock(BaseModel):
    """One retrieved evidence block handed to the generator.

    ``handle`` is the only citation identifier the model may return. It is a
    server-minted opaque token, not a database id, so a leaked handle discloses
    nothing and an invented handle cannot accidentally resolve.
    """

    model_config = {"frozen": True}

    handle: str
    text: str
    work_title: str | None = None
    work_year: int | None = None
    source_label: str | None = None
    locator: str | None = None
    #: True when policy allows only a bounded excerpt or locator-only display.
    display_restricted: bool = False
    #: True when the block is from the paper's abstract, standing in for a full
    #: text nobody could fetch: an answer resting on it must say so.
    abstract_only: bool = False
    #: What a reader needs to find the paper and the passage. Shown beside the
    #: source, never put in the prompt: the generator sees the fields above.
    journal: str | None = None
    authors: tuple[str, ...] = ()
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    section: str | None = None
    page: int | None = None
    chunk_type: str | None = None


class GenerationRequest(BaseModel):
    model_config = {"frozen": True}

    system_prompt: str
    prompt_bundle_id: str
    prompt_bundle_version: str
    question: str
    conversation: tuple[dict[str, str], ...] = ()
    evidence: tuple[EvidenceBlock, ...] = ()
    structured_claims: tuple[dict[str, Any], ...] = ()
    max_output_tokens: int = 2048
    temperature: float | None = 0.1
    #: Release the answer is anchored to, recorded in answer provenance.
    release_label: str | None = None


class GenerationResult(BaseModel):
    model_config = {"frozen": True}

    text: str
    cited_handles: tuple[str, ...] = ()
    provider: str = ""
    model: str = ""
    model_revision: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    truncated: bool = False


@dataclass(slots=True)
class StreamUsage:
    """Token counts a provider reports once a stream has finished.

    Passed in by the caller and filled by the provider, because a generator's
    return value is not visible to `async for`, and a shared provider instance
    must not hold per-request state. Usage is metered against a member's token
    allowance, so a stream that reported nothing would make every answer free.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None


class StructuredRequest(BaseModel):
    model_config = {"frozen": True}

    system_prompt: str
    user_prompt: str
    json_schema: dict[str, Any]
    schema_name: str
    max_output_tokens: int = 4096
    temperature: float | None = 0.0
    #: Content that may be cached by the provider as a prompt prefix. Only
    #: material the policy engine cleared for external transfer reaches here.
    cacheable_prefix: str | None = None


class StructuredResult(BaseModel):
    model_config = {"frozen": True}

    data: dict[str, Any] | None
    raw_text: str = ""
    refused: bool = False
    truncated: bool = False
    provider: str = ""
    model: str = ""
    model_revision: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model_settings: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    properties: ProviderProperties

    async def generate(self, request: GenerationRequest) -> GenerationResult: ...

    def stream(
        self, request: GenerationRequest, *, usage: StreamUsage | None = None
    ) -> AsyncIterator[str]: ...

    async def structured(self, request: StructuredRequest) -> StructuredResult: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    profile: EmbeddingProfile

    async def embed_query(self, text: str) -> Sequence[float]: ...

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...
