"""Model providers, embedders, and extractors."""

from lab_assistant.adapters.models.anthropic_provider import AnthropicProvider, sampling_params
from lab_assistant.adapters.models.embeddings import SentenceTransformerEmbedder
from lab_assistant.adapters.models.extractor import ModelExtractor
from lab_assistant.adapters.models.fake import (
    FakeExtractor,
    FakeLLMProvider,
    HashingEmbeddingProvider,
    ScriptedExtractor,
)

__all__ = [
    "AnthropicProvider",
    "FakeExtractor",
    "FakeLLMProvider",
    "HashingEmbeddingProvider",
    "ModelExtractor",
    "ScriptedExtractor",
    "SentenceTransformerEmbedder",
    "sampling_params",
]
