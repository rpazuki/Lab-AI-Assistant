"""Sentence-transformer embedding adapter.

The predecessor's default encoder was ``BiomedNLP-PubMedBERT-...``, a masked
language model rather than a model trained with a retrieval objective. Mean-pooled
MLM checkpoints often lose to much smaller purpose-trained bi-encoders on
retrieval despite better domain vocabulary. So the model is *configuration*, the
profile records exactly which checkpoint and revision produced a projection, and
the choice is expected to be made on measured recall (ADR-0013).

Weights are cached in a persistent directory so a container restart does not
re-download them — a rule the predecessor's deployment learned and this adapter
keeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.models import EmbeddingProfile, VectorIndexKind

__all__ = ["SentenceTransformerEmbedder"]


@dataclass
class SentenceTransformerEmbedder:
    """Local embedding provider. CPU by default; GPU when one is present."""

    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    profile_id: str = "minilm-l6-v2"
    model_revision: str = "unknown"
    dimensions: int = 384
    cache_folder: Path | None = None
    batch_size: int = 32
    index_kind: VectorIndexKind = VectorIndexKind.HNSW
    index_parameters: dict[str, int] = field(
        default_factory=lambda: {"m": 16, "ef_construction": 64}
    )
    _model: Any | None = None

    @property
    def profile(self) -> EmbeddingProfile:
        return EmbeddingProfile(
            profile_id=self.profile_id,
            provider="sentence-transformers",
            model=self.model_name,
            model_revision=self.model_revision,
            dimensions=self.dimensions,
            normalisation="l2",
            external=False,
            index_kind=self.index_kind,
            index_parameters=self.index_parameters,
            storage_type="vector" if self.dimensions <= 2000 else "halfvec",
        )

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
            raise LabAssistantError(
                "sentence-transformers is not installed; install the 'embeddings' extra "
                "or configure the hashing profile"
            ).with_class(ErrorClass.PROVIDER_ERROR) from exc
        self._model = SentenceTransformer(
            self.model_name,
            cache_folder=str(self.cache_folder) if self.cache_folder else None,
        )
        actual = int(self._model.get_sentence_embedding_dimension())
        if actual != self.dimensions:
            raise LabAssistantError(
                f"model {self.model_name} produces {actual} dimensions but the profile "
                f"declares {self.dimensions}; vectors of different dimensions must never "
                "share a projection table"
            ).with_class(ErrorClass.SCHEMA_VIOLATION)
        return self._model

    async def embed_query(self, text: str) -> Sequence[float]:
        vectors = await self.embed_documents([text])
        return vectors[0]

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return []
        # Encoding is CPU-bound; a thread keeps the event loop responsive.
        return await asyncio.to_thread(self._encode, list(texts))

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [[float(value) for value in row] for row in vectors]
