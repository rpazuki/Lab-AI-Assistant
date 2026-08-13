"""Canonicalisation and reproducibility fingerprints.

Every "same input, same output" claim in the system reduces to these functions,
including the extraction cache key whose predecessor let two different templates
at version 1 collide.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from lab_assistant.contracts.canonical import (
    CanonicalisationError,
    canonical_json,
    fingerprint,
    sha256_text,
    stable_number,
)
from lab_assistant.contracts.extraction import (
    ExtractionContext,
    TemplateTarget,
    TemplateVersionSpec,
)
from lab_assistant.contracts.models import EmbeddingProfile, VectorIndexKind
from lab_assistant.contracts.stage import StageCommand, StageName
from lab_assistant.provenance.fingerprints import (
    extraction_item_fingerprint,
    projection_fingerprint,
    source_request_fingerprint,
    task_fingerprint,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE = UUID("00000000-0000-4000-8000-000000000002")


class TestCanonicalJson:
    def test_key_order_never_changes_the_bytes(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_sequence_order_does_change_the_bytes(self) -> None:
        # Evidence selection order is meaningful, so lists are not sorted.
        assert canonical_json([1, 2]) != canonical_json([2, 1])

    def test_unicode_forms_agree(self) -> None:
        assert sha256_text("é") == sha256_text("é")

    def test_datetimes_are_utc_and_naive_ones_are_refused(self) -> None:
        stamp = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
        assert b"2026-08-12T09:00:00Z" in canonical_json({"at": stamp})
        with pytest.raises(CanonicalisationError, match="naive datetime"):
            canonical_json({"at": datetime(2026, 8, 12, 9, 0)})

    def test_payload_serialisation_accepts_floats(self) -> None:
        # A rendition carries bounding boxes and confidences; refusing them would
        # mean inventing a parallel encoding for real measurements.
        assert canonical_json({"confidence": 0.87}) == b'{"confidence":0.87}'

    def test_decimals_are_stable(self) -> None:
        assert canonical_json({"v": Decimal("1.10")}) == canonical_json({"v": Decimal("1.1")})

    def test_fingerprints_refuse_floats(self) -> None:
        # A digest claims two runs had the same input; an accumulated float is not
        # the number anyone configured.
        with pytest.raises(CanonicalisationError, match="not fingerprintable"):
            fingerprint(kind="test", parts={"threshold": 0.1})
        assert fingerprint(kind="test", parts={"threshold": stable_number(0.1)})

    def test_bytes_are_hashed_not_embedded(self) -> None:
        assert canonical_json({"b": b"abc"}) == canonical_json({"b": sha256_text("abc")})


class TestFingerprintNamespacing:
    def test_kind_is_part_of_the_digest(self) -> None:
        parts = {"a": 1}
        assert fingerprint(kind="one", parts=parts) != fingerprint(kind="two", parts=parts)

    def test_prefix_is_readable(self) -> None:
        assert fingerprint(kind="chunk", parts={}).startswith("chunk:")


def _template(version: str, *, target_description: str = "yield") -> TemplateVersionSpec:
    return TemplateVersionSpec(
        template_id="yeast-datasheet",
        semantic_version=version,
        title="Yeast datasheet",
        scientific_purpose="lipid yields",
        targets=(
            TemplateTarget(
                target_id="lipid_yield",
                predicate="lipid_yield",
                object_kind="quantity",
                description=target_description,
                unit_expectation="g/g",
            ),
        ),
    )


def _context(
    template: TemplateVersionSpec,
    *,
    workspace: UUID = WORKSPACE,
    nodes: tuple[str, ...] = ("p-1",),
    digest: str = "abc123",
) -> ExtractionContext:
    return ExtractionContext(
        workspace_id=workspace,
        work_id=UUID("00000000-0000-4000-8000-000000000010"),
        rendition_id=UUID("00000000-0000-4000-8000-000000000011"),
        rendition_digest=digest,
        template=template,
        selected_nodes=nodes,
        evidence_text="[p-1] yield 0.42 g/g",
        prompt_bundle_id="extraction-default",
        prompt_bundle_version="1",
    )


def _fingerprint(context: ExtractionContext, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "selector_version": "sel/1",
        "prompt_builder_version": "pb/1",
        "validator_version": "val/1",
        "parser_version": "1.0",
        "provider": "fake",
        "model": "fake-1",
        "model_revision": "fake-1",
        "model_settings": {"temperature": 0.0},
        "output_mode": "structured_json",
        "policy_version_id": None,
    }
    kwargs.update(overrides)
    return extraction_item_fingerprint(context, **kwargs)  # type: ignore[arg-type]


class TestExtractionFingerprint:
    def test_same_inputs_give_the_same_key(self) -> None:
        template = _template("1.0.0")
        assert _fingerprint(_context(template)) == _fingerprint(_context(template))

    def test_two_templates_at_version_one_do_not_collide(self) -> None:
        """The exact legacy defect: template identity, not just version number."""
        left = _template("1.0.0")
        right = _template("1.0.0").model_copy(update={"template_id": "other-datasheet"})
        assert _fingerprint(_context(left)) != _fingerprint(_context(right))

    def test_a_changed_target_description_invalidates_the_cache(self) -> None:
        assert _fingerprint(_context(_template("1.0.0"))) != _fingerprint(
            _context(_template("1.0.0", target_description="yield after 72 h"))
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"selector_version": "sel/2"},
            {"prompt_builder_version": "pb/2"},
            {"validator_version": "val/2"},
            {"parser_version": "2.0"},
            {"model_revision": "fake-2"},
            {"model_settings": {"temperature": 0.7}},
            {"output_mode": "text"},
            {"policy_version_id": UUID("00000000-0000-4000-8000-0000000000ff")},
        ],
    )
    def test_every_declared_input_changes_the_key(self, overrides: dict[str, object]) -> None:
        template = _template("1.0.0")
        baseline = _fingerprint(_context(template))
        assert _fingerprint(_context(template), **overrides) != baseline

    def test_workspace_and_selection_are_part_of_the_key(self) -> None:
        template = _template("1.0.0")
        baseline = _fingerprint(_context(template))
        assert _fingerprint(_context(template, workspace=OTHER_WORKSPACE)) != baseline
        assert _fingerprint(_context(template, nodes=("p-1", "p-2"))) != baseline
        assert _fingerprint(_context(template, digest="def456")) != baseline


class TestOtherFingerprints:
    def test_cursor_semantics_version_starts_a_new_lineage(self) -> None:
        base = {
            "connector_id": "pubmed",
            "connector_version": "1.0.0",
            "method": "GET",
            "canonical_url": "https://example.invalid/search",
            "parameters": {"term": "yeast"},
            "cursor_position": "0",
        }
        assert source_request_fingerprint(
            **base, cursor_semantics_version="1"
        ) != source_request_fingerprint(**base, cursor_semantics_version="2")

    def test_embedding_profile_parameters_change_the_projection_key(self) -> None:
        release = UUID("00000000-0000-4000-8000-000000000020")
        profile = EmbeddingProfile(
            profile_id="p1",
            provider="local",
            model="m",
            model_revision="1",
            dimensions=64,
            index_kind=VectorIndexKind.HNSW,
            index_parameters={"m": 16},
        )
        other = profile.model_copy(update={"index_parameters": {"m": 32}})
        assert projection_fingerprint(
            release_id=release, chunker_key="structural-v1@1", embedding_profile=profile
        ) != projection_fingerprint(
            release_id=release, chunker_key="structural-v1@1", embedding_profile=other
        )

    def test_task_fingerprint_is_stable_for_a_duplicate_delivery(self) -> None:
        command = StageCommand(
            stage=StageName.PARSING,
            workspace_id=WORKSPACE,
            actor_id=WORKSPACE,
            inputs={"asset_id": "abc"},
        )
        assert task_fingerprint(command, implementation_version="parsing/1") == task_fingerprint(
            command, implementation_version="parsing/1"
        )
        assert task_fingerprint(command, implementation_version="parsing/2") != task_fingerprint(
            command, implementation_version="parsing/1"
        )
