"""The wording of the email an admin sends with an invitation, for this workspace.

There is no mailer: the Members page drafts the email and the admin sends it from
their own address. What it says starts from the product's wording, and an admin
can replace that wording for the workspace, one version for invitations by link
and one for invitations by temporary password.

Stored as a ``workspace_profile`` config version, ``invitation-email``: each save
is a new version, with who saved it and when, like every recorded setting. A kind
saved as ``None`` falls back to the product's wording, which lives with the page
that fills it in (``apps/web/src/lib/invitationEmail.ts``).

The one rule kept here is that a version must still carry what the email exists
to send: ``{link}`` for a link, ``{password}`` for a temporary password. Anything
else about the wording is the admin's choice.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.domain.scope import ApprovalState, ConfigKind, ConfigVersion
from lab_assistant.domain.workspace import Capability, InvitationKind

__all__ = ["EMAIL_ID", "REQUIRED_PLACEHOLDER", "InvitationEmailService"]

EMAIL_ID = "invitation-email"

#: What each kind of email must contain: the thing it is sent to deliver.
REQUIRED_PLACEHOLDER: dict[InvitationKind, str] = {
    InvitationKind.LINK: "{link}",
    InvitationKind.TEMPORARY_PASSWORD: "{password}",
}

SUBJECT_LIMIT = 200
BODY_LIMIT = 5000


async def _setting(uow: Any, workspace_id: UUID) -> ConfigVersion | None:
    versions = [
        config
        for config in await uow.configs.list_config_versions(
            workspace_id, kind=ConfigKind.WORKSPACE_PROFILE.value
        )
        if config.logical_id == EMAIL_ID
    ]
    if not versions:
        return None
    return max(versions, key=lambda c: (c.created_at is not None, c.created_at, c.id))


def _checked(kind: InvitationKind, template: dict[str, Any] | None) -> dict[str, str] | None:
    """One kind's wording, trimmed and checked; ``None`` means the product's wording."""
    if template is None:
        return None
    subject = str(template.get("subject") or "").strip()
    body = str(template.get("body") or "").replace("\r\n", "\n").strip()
    label = "link" if kind is InvitationKind.LINK else "temporary password"
    if not subject or not body:
        raise ValidationFailure(f"the {label} email needs a subject and a message")
    if len(subject) > SUBJECT_LIMIT or len(body) > BODY_LIMIT:
        raise ValidationFailure(
            f"the {label} email is too long: at most {SUBJECT_LIMIT} characters of subject "
            f"and {BODY_LIMIT} of message"
        )
    required = REQUIRED_PLACEHOLDER[kind]
    if required not in body:
        raise ValidationFailure(
            f"the {label} email must contain {required} in its message, "
            f"or the person receives no {label}",
            context={"kind": kind.value, "missing": required},
        )
    return {"subject": subject, "body": body}


class InvitationEmailService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def view(self, actor: ActorContext) -> dict[str, Any]:
        """The workspace's wording of each kind, ``None`` where it uses the product's."""
        actor.require(Capability.MANAGE_MEMBERS)
        async with self._context.uow_factory() as uow:
            setting = await _setting(uow, actor.workspace_id)
        templates = setting.payload if setting else {}
        return {
            "templates": {kind.value: templates.get(kind.value) for kind in InvitationKind},
            "set_at": setting.created_at.isoformat() if setting and setting.created_at else None,
            "set_by": str(setting.created_by) if setting and setting.created_by else None,
        }

    async def save(
        self, actor: ActorContext, templates: dict[str, dict[str, Any] | None]
    ) -> dict[str, Any]:
        """Record new wording for both kinds; ``None`` for a kind restores the product's."""
        actor.require(Capability.MANAGE_MEMBERS)
        payload = {kind.value: _checked(kind, templates.get(kind.value)) for kind in InvitationKind}
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            previous = await _setting(uow, ws)
            number = int(previous.semantic_version.split(".")[0]) + 1 if previous else 1
            config = ConfigVersion(
                id=context.new_id(),
                workspace_id=ws,
                kind=ConfigKind.WORKSPACE_PROFILE,
                logical_id=EMAIL_ID,
                semantic_version=f"{number}.0.0",
                payload=payload,
                status=ApprovalState.APPROVED,
                created_at=context.now(),
                created_by=actor.user_id,
            )
            await uow.configs.add_config_version(config)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ADMIN_ACTION,
                    subject_type="config_version",
                    subject_id=config.id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="invitation_email_changed",
                    metadata={
                        "custom": sorted(kind for kind, value in payload.items() if value),
                    },
                )
            )
            await uow.commit()
        return await self.view(actor)
