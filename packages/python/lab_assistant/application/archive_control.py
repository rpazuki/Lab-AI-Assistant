"""The two control records a library has, read the same way.

A library has a catalog head (`contracts/publication.py`) and a policy head
(`contracts/archive_policy.py`). They are moved by one writer, fenced by one
rule (`domain/publication.authority_refusal`), and read here so that both fail
identically: an absent record is ``None``, and a denial, a timeout or a record
that will not parse raises. A writer that read "no head" from a permissions
error would conclude the library was empty and create one over the top of a
library it simply could not see.
"""

from __future__ import annotations

import json
from uuid import UUID

from lab_assistant.application.ports import ArchiveControlStore
from lab_assistant.contracts.archive_policy import POLICY_HEAD_LOCATION, PolicyHeadDescriptor
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.publication import HEAD_LOCATION, HeadDescriptor

__all__ = [
    "library_prefix",
    "read_catalog_head",
    "read_control_record",
    "read_policy_head",
]


def library_prefix(library_id: UUID) -> str:
    """Where a library's objects live. One namespace per library, because
    deduplication across libraries would couple their rights and retention."""
    return f"libraries/{library_id}/"


async def read_control_record(control: ArchiveControlStore, key: str, model):
    """A control record and its version token, or ``(None, None)`` when absent."""
    payload = await control.read(key)
    if payload is None:
        return None, None
    try:
        record = model.model_validate(json.loads(payload.data))
    except Exception as exc:
        raise LabAssistantError(
            "a library control record does not parse",
            context={"key": key, "detail": str(exc)},
        ).with_class(ErrorClass.INVALID_CONTENT) from exc
    return record, payload.version


async def read_catalog_head(
    control: ArchiveControlStore, library_id: UUID
) -> tuple[HeadDescriptor | None, str | None]:
    return await read_control_record(
        control, f"{library_prefix(library_id)}{HEAD_LOCATION}", HeadDescriptor
    )


async def read_policy_head(
    control: ArchiveControlStore, library_id: UUID
) -> tuple[PolicyHeadDescriptor | None, str | None]:
    return await read_control_record(
        control, f"{library_prefix(library_id)}{POLICY_HEAD_LOCATION}", PolicyHeadDescriptor
    )
