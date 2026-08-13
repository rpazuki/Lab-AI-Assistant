"""Deterministic fake model provider, embedder, and extractor.

Same rule as the fake connectors: **fixture-keyed, and it raises on a miss.**

The fake generator does not paraphrase. It composes an answer from the evidence
blocks it was actually given and cites their handles, so a citation-validation
test proves something. To exercise the invalid-handle path a test configures
``invent_handle``, which makes the provider return a handle that was never
supplied — the behaviour the plan requires to degrade to a flagged answer rather
than a 500.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.extraction import (
    ClaimProposal,
    ExtractionRequest,
    ExtractionResult,
    ProposalSource,
)
from lab_assistant.contracts.models import (
    EmbeddingProfile,
    GenerationRequest,
    GenerationResult,
    ProviderProperties,
    StructuredRequest,
    StructuredResult,
    VectorIndexKind,
)

__all__ = [
    "FakeExtractor",
    "FakeLLMProvider",
    "HashingEmbeddingProvider",
    "ScriptedExtractor",
]


@dataclass
class FakeLLMProvider:
    """Composes a grounded answer from supplied evidence, deterministically."""

    provider_id: str = "fake"
    model: str = "fake-answerer-1"
    #: When set, the provider cites this handle even though it was never supplied.
    invent_handle: str | None = None
    #: When set, the provider refuses, so the refusal path is testable.
    refuse: bool = False
    truncate_after_chars: int | None = None
    #: Scripted structured outputs, keyed by ``schema_name``.
    structured_fixtures: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def properties(self) -> ProviderProperties:
        return ProviderProperties(
            provider_id=self.provider_id,
            supports_streaming=True,
            supports_structured_output=True,
            supports_batch=False,
            supports_seed=True,
            accepted_sampling_params=frozenset({"temperature"}),
            retains_data=False,
            trains_on_data=False,
            regions=("local",),
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        text = self._compose(request)
        handles = tuple(dict.fromkeys(re.findall(r"\[(E[0-9a-f]{10})\]", text)))
        return GenerationResult(
            text=text,
            cited_handles=handles,
            provider=self.provider_id,
            model=self.model,
            model_revision=self.model,
            input_tokens=sum(len(block.text) for block in request.evidence) // 4,
            output_tokens=len(text) // 4,
            stop_reason="end_turn" if self.truncate_after_chars is None else "max_tokens",
            truncated=self.truncate_after_chars is not None,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        text = self._compose(request)
        # Word-level chunks: enough to exercise the streaming path without
        # pretending to model real token boundaries.
        for index, word in enumerate(text.split(" ")):
            yield word if index == 0 else f" {word}"

    async def structured(self, request: StructuredRequest) -> StructuredResult:
        if self.refuse:
            return StructuredResult(
                data=None,
                refused=True,
                provider=self.provider_id,
                model=self.model,
                model_revision=self.model,
            )
        if request.schema_name not in self.structured_fixtures:
            raise LabAssistantError(
                f"fake provider has no structured fixture for schema "
                f"{request.schema_name!r}; recorded: {sorted(self.structured_fixtures)}"
            ).with_class(ErrorClass.PROVIDER_ERROR)
        data = self.structured_fixtures[request.schema_name]
        raw = json.dumps(data, sort_keys=True)
        truncated = False
        if self.truncate_after_chars is not None and len(raw) > self.truncate_after_chars:
            raw = raw[: self.truncate_after_chars]
            truncated = True
            data = None
        return StructuredResult(
            data=data,
            raw_text=raw,
            truncated=truncated,
            provider=self.provider_id,
            model=self.model,
            model_revision=self.model,
            input_tokens=len(request.user_prompt) // 4,
            output_tokens=len(raw) // 4,
            model_settings=(
                {"temperature": request.temperature} if request.temperature is not None else {}
            ),
        )

    def _compose(self, request: GenerationRequest) -> str:
        if self.refuse:
            return "I cannot answer this from the supplied evidence."
        if not request.evidence and not request.structured_claims:
            return (
                "The supplied evidence does not cover this question, so there is nothing to "
                "report. No source in the current release addresses it."
            )
        sentences: list[str] = []
        for block in request.evidence:
            first = block.text.strip().split(". ")[0].strip().rstrip(".")
            if not first:
                continue
            sentences.append(f"{first} [{block.handle}].")
        for claim in request.structured_claims[:3]:
            handle = claim.get("handle")
            value = (claim.get("value") or {}).get("verbatim")
            if handle and value:
                sentences.append(
                    f"{claim.get('subject') or 'The source'} reports "
                    f"{claim.get('predicate')} of {value} [{handle}]."
                )
        if self.invent_handle:
            sentences.append(f"An additional unsupported statement [{self.invent_handle}].")
        answer = " ".join(sentences)
        if self.truncate_after_chars is not None:
            answer = answer[: self.truncate_after_chars]
        return answer


@dataclass
class HashingEmbeddingProvider:
    """Deterministic hashing embedder.

    Not a semantic model. It exists so the vector path — profiles, per-dimension
    tables, incremental copies, fusion — is exercised offline and identically on
    every machine. A real deployment sets a measured profile instead; see the
    embedding ADR, which requires the choice to be made on measured recall rather
    than inherited from the predecessor.
    """

    dimensions: int = 64
    profile_id: str = "hashing-v1"

    @property
    def profile(self) -> EmbeddingProfile:
        return EmbeddingProfile(
            profile_id=self.profile_id,
            provider="local",
            model="hashing",
            model_revision="1",
            dimensions=self.dimensions,
            normalisation="l2",
            external=False,
            index_kind=VectorIndexKind.NONE,
            index_parameters={},
        )

    async def embed_query(self, text: str) -> Sequence[float]:
        return self._embed(text)

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in "".join(c.lower() if c.isalnum() else " " for c in text).split()
        if len(token) > 1
    ]


@dataclass
class ScriptedExtractor:
    """Extractor returning fixture proposals keyed by rendition digest.

    A digest with no fixture raises. This is what stops an extraction test from
    "passing" against proposals that were meant for a different document.
    """

    extractor_id: str = "scripted"
    extractor_version: str = "1.0.0"
    proposals_by_digest: dict[str, tuple[ClaimProposal, ...]] = field(default_factory=dict)

    async def propose(self, request: ExtractionRequest) -> ExtractionResult:
        digest = request.context.rendition_digest
        if digest not in self.proposals_by_digest:
            raise LabAssistantError(
                f"scripted extractor has no proposals for rendition digest {digest[:12]}...; "
                f"recorded: {[d[:12] for d in sorted(self.proposals_by_digest)]}"
            ).with_class(ErrorClass.PROVIDER_ERROR)
        return ExtractionResult(
            proposals=self.proposals_by_digest[digest],
            provider="fake",
            model="scripted",
            model_revision="1",
            input_tokens=len(request.context.evidence_text) // 4,
            output_tokens=32,
        )


@dataclass
class FakeExtractor:
    """Deterministic rule-based extractor over the supplied evidence text.

    It reads the evidence it was actually given and proposes values it can quote
    verbatim, so span verification is meaningful. Numbers it cannot find are
    abstentions, not guesses.
    """

    extractor_id: str = "regex-demo"
    extractor_version: str = "1.0.0"

    async def propose(self, request: ExtractionRequest) -> ExtractionResult:
        context = request.context
        proposals: list[ClaimProposal] = []
        blocks = _split_blocks(context.evidence_text)
        for target in context.template.targets:
            if target.deterministic_metadata:
                value = context.deterministic_metadata.get(target.predicate)
                if value is not None:
                    proposals.append(
                        ClaimProposal(
                            target_id=target.target_id,
                            predicate=target.predicate,
                            object_kind=target.object_kind,
                            value_verbatim=str(value),
                            proposal_source=ProposalSource.DETERMINISTIC_PARSER,
                        )
                    )
                continue
            match = _find_value(blocks, target)
            if match is None:
                from lab_assistant.contracts.errors import AbstentionReason

                proposals.append(
                    ClaimProposal(
                        target_id=target.target_id,
                        predicate=target.predicate,
                        object_kind=target.object_kind,
                        abstention=(
                            AbstentionReason.NOT_REPORTED_AFTER_COMPLETE_SEARCH
                            if not context.unsearched_nodes
                            else AbstentionReason.NOT_SEARCHED_BUDGET
                        ),
                        proposal_source=ProposalSource.DETERMINISTIC_PARSER,
                    )
                )
                continue
            node_id, quote, value, unit = match
            from lab_assistant.contracts.extraction import EvidenceRef

            proposals.append(
                ClaimProposal(
                    target_id=target.target_id,
                    predicate=target.predicate,
                    object_kind=target.object_kind,
                    value_verbatim=value,
                    unit_verbatim=unit,
                    evidence=(EvidenceRef(node_id=node_id, quote=quote),),
                    proposal_source=ProposalSource.DETERMINISTIC_PARSER,
                )
            )
        return ExtractionResult(
            proposals=tuple(proposals),
            provider="local",
            model="regex-demo",
            model_revision="1",
        )


def _split_blocks(evidence_text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for chunk in evidence_text.split("\n\n"):
        match = re.match(r"^\[([^\]]+)\]\s*(.*)$", chunk, flags=re.DOTALL)
        if match:
            blocks.append((match.group(1), match.group(2).strip()))
    return blocks


def _find_value(
    blocks: list[tuple[str, str]], target: Any
) -> tuple[str, str, str, str | None] | None:
    """Find a quotable sentence containing the target's terms and a number."""
    terms = [target.predicate.replace("_", " "), *target.likely_sections]
    for node_id, text in blocks:
        for sentence in re.split(r"(?<=[.;])\s+", text):
            lowered = sentence.casefold()
            if not any(term.casefold() in lowered for term in terms if term):
                continue
            number = re.search(r"(\d+(?:\.\d+)?)\s*([A-Za-zμ%/·\-]+(?:/[A-Za-z]+)?)?", sentence)
            if number is None:
                continue
            return node_id, sentence.strip(), number.group(1), (number.group(2) or None)
    return None
