"""Token minting and validation.

Short-lived HS256 access tokens carrying issuer, audience, subject, auth epoch,
and token id. The web tier keeps the token in an ``httpOnly`` cookie and proxies
API calls, so the browser never handles it directly — the pattern the predecessor
arrived at, kept deliberately.

``auth_epoch`` is checked on every request: bumping a user's epoch invalidates
every token already issued to them, which is what a credential incident needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt

from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = ["AccessToken", "TokenError", "decode_token", "mint_token"]

ISSUER = "lab-ai-assistant"
AUDIENCE = "lab-ai-assistant-api"
ALGORITHM = "HS256"


class TokenError(LabAssistantError):
    error_class = ErrorClass.AUTHENTICATION_REQUIRED
    http_status = 401


@dataclass(frozen=True, slots=True)
class AccessToken:
    user_id: UUID
    auth_epoch: int
    token_id: str
    expires_at: datetime
    workspace_id: UUID | None = None


def mint_token(
    *,
    secret: str,
    user_id: UUID,
    auth_epoch: int,
    ttl_minutes: int,
    now: datetime | None = None,
    workspace_id: UUID | None = None,
) -> tuple[str, AccessToken]:
    issued_at = now or datetime.now(tz=UTC)
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    token_id = uuid4().hex
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "jti": token_id,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "epoch": auth_epoch,
    }
    if workspace_id is not None:
        payload["ws"] = str(workspace_id)
    encoded = jwt.encode(payload, secret, algorithm=ALGORITHM)
    return encoded, AccessToken(
        user_id=user_id,
        auth_epoch=auth_epoch,
        token_id=token_id,
        expires_at=expires_at,
        workspace_id=workspace_id,
    )


def decode_token(token: str, *, secret: str) -> AccessToken:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "sub", "jti", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("session has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Deliberately uninformative: the reason belongs in the audit log.
        raise TokenError("invalid session") from exc
    workspace = payload.get("ws")
    return AccessToken(
        user_id=UUID(payload["sub"]),
        auth_epoch=int(payload.get("epoch", 1)),
        token_id=str(payload["jti"]),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        workspace_id=UUID(workspace) if workspace else None,
    )
