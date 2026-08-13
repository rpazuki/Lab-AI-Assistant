"""Reproducibility fingerprints.

The legacy extraction cache key used selected-text hash, an integer template
version, and a model name. It therefore let two different templates at version
``1`` share a cache entry, and let a prompt change silently reuse stale output.

Each function here names every input that can change the result. A cache hit is
only valid when *all* of them match, and never across a workspace or policy
boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from lab_assistant.contracts.canonical import fingerprint, stable_number
from lab_assistant.contracts.extraction import ExtractionContext
from lab_assistant.contracts.models import EmbeddingProfile
from lab_assistant.contracts.stage import StageCommand

__all__ = [
    "acquisition_attempt_fingerprint",
    "extraction_item_fingerprint",
    "projection_fingerprint",
    "source_request_fingerprint",
    "task_fingerprint",
]


def source_request_fingerprint(
    *,
    connector_id: str,
    connector_version: str,
    method: str,
    canonical_url: str,
    parameters: dict[str, Any],
    cursor_position: str | None,
    cursor_semantics_version: str,
) -> str:
    """Identity of one upstream request.

    Includes the cursor semantics version: a source that changes what its cursor
    means starts a new lineage rather than silently resuming.
    """
    return fingerprint(
        kind="source_request",
        parts={
            "connector": f"{connector_id}@{connector_version}",
            "method": method,
            "url": canonical_url,
            "parameters": parameters,
            "cursor": cursor_position,
            "cursor_semantics_version": cursor_semantics_version,
        },
    )


def acquisition_attempt_fingerprint(
    *,
    manifestation_id: UUID | None,
    route: str,
    target_url: str | None,
    rights_policy_version_id: UUID | None,
) -> str:
    return fingerprint(
        kind="acquisition_attempt",
        parts={
            "manifestation_id": manifestation_id,
            "route": route,
            "target_url": target_url,
            "rights_policy_version_id": rights_policy_version_id,
        },
    )


def extraction_item_fingerprint(
    context: ExtractionContext,
    *,
    selector_version: str,
    prompt_builder_version: str,
    validator_version: str,
    parser_version: str,
    provider: str,
    model: str,
    model_revision: str,
    model_settings: dict[str, Any],
    output_mode: str,
    policy_version_id: UUID | None,
    span_digests: Sequence[str] = (),
) -> str:
    """The complete per-item extraction fingerprint (data model §11.2).

    Every element listed in the plan is present, including the workspace and
    policy scope — a cache entry must never cross either boundary.
    """
    return fingerprint(
        kind="extraction_item",
        parts={
            "workspace_id": context.workspace_id,
            "policy_version_id": policy_version_id,
            "allow_external_model": context.allow_external_model,
            "rendition_digest": context.rendition_digest,
            "selected_nodes": list(context.selected_nodes),
            "span_digests": list(span_digests),
            "deterministic_metadata": context.deterministic_metadata,
            "template": {
                "logical_key": context.template.logical_key,
                "schema_version": context.template.schema_version,
                "cardinality": context.template.cardinality,
                "targets": [t.model_dump(mode="python") for t in context.template.targets],
                "evidence_standard": context.template.evidence_standard,
            },
            "prompt_bundle": {
                "id": context.prompt_bundle_id,
                "version": context.prompt_bundle_version,
            },
            "versions": {
                "selector": selector_version,
                "prompt_builder": prompt_builder_version,
                "validator": validator_version,
                "parser": parser_version,
            },
            "provider": {
                "provider": provider,
                "model": model,
                "model_revision": model_revision,
                # Sampling values are configured floats: converted to one stable
                # textual form so the key is reproducible across platforms.
                "settings": _stable_settings(model_settings),
                "output_mode": output_mode,
            },
        },
    )


def _stable_settings(settings: dict[str, Any]) -> dict[str, Any]:
    def stabilise(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        return stable_number(value)

    return {key: stabilise(value) for key, value in sorted(settings.items())}


def projection_fingerprint(
    *,
    release_id: UUID,
    chunker_key: str,
    embedding_profile: EmbeddingProfile | None,
) -> str:
    """Identity of one search projection build."""
    return fingerprint(
        kind="projection",
        parts={
            "release_id": release_id,
            "chunker": chunker_key,
            "embedding_profile": (
                None
                if embedding_profile is None
                else {
                    "profile_id": embedding_profile.profile_id,
                    "model": embedding_profile.model,
                    "model_revision": embedding_profile.model_revision,
                    "dimensions": embedding_profile.dimensions,
                    "normalisation": embedding_profile.normalisation,
                    "index_kind": embedding_profile.index_kind,
                    "index_parameters": embedding_profile.index_parameters,
                }
            ),
        },
    )


def task_fingerprint(command: StageCommand, *, implementation_version: str) -> str:
    """Idempotency key for a task.

    Two deliveries of the same command have the same fingerprint, so the second
    finds the first's result and does nothing.

    ``actor_id`` is deliberately **excluded**. Two stewards asking for the same
    discovery run over the same scope want one run, not two: the work is a pure
    function of the command, and who asked belongs in the audit trail rather than in
    the identity of the work. Including it would multiply every task by the number
    of people who requested it and quietly double every source's request volume.
    """
    return fingerprint(
        kind="task",
        parts={
            "stage": command.stage,
            "workspace_id": command.workspace_id,
            "inputs": command.inputs,
            "config_version_ids": {
                k: str(v) for k, v in sorted(command.config_version_ids.items())
            },
            "policy_version_id": command.policy_version_id,
            "implementation_version": implementation_version,
            "explicit_key": command.idempotency_key,
        },
    )
