"""Whose signature counts, and what an unsigned snapshot means.

Verifying a signature is arithmetic. Deciding whose key to verify it *against*
is the security property, and it is the half that gets skipped.

**Keys are pinned, never learned.** A verifier trusts the keys its operator
configured and nothing else. The tempting alternative — read the key
identifier from the manifest, fetch that key from the archive, verify — is
circular: an attacker who can write the manifest can write the key beside it,
and every signature then verifies perfectly. Trust has to enter from outside
the artifact being trusted.

**Rotation is an operator action, not an event in the data.** A new key
becomes trusted when somebody adds it to configuration, which is an
authenticated transition because changing a deployment's configuration is. A
snapshot cannot vouch for the key that signed it, and a key cannot vouch for
its own successor.

**Unsigned is a finding, not a failure — unless signing is required.** A
library may be published by an installation that has no key yet; refusing to
read it would make signing impossible to adopt incrementally. So an unsigned
snapshot is reported, and a deployment that has decided signatures are
mandatory says so in configuration and gets a refusal instead. What is never
allowed is treating a *bad* signature as merely unsigned: a signature that
does not verify is an active claim that failed, and is always blocking.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lab_assistant.contracts.signing import SIGNING_ALGORITHM

__all__ = [
    "SignatureStatus",
    "SignatureVerdict",
    "evaluate_signature",
]


class SignatureStatus(StrEnum):
    #: Verified against a key this deployment was configured to trust.
    TRUSTED = "trusted"
    #: No signature at all.
    UNSIGNED = "unsigned"
    #: A signature naming a key nobody told this deployment to trust. Not the
    #: same as a forgery — it may be a legitimate publisher whose key has not
    #: been distributed — and not the same as trusted either.
    UNKNOWN_KEY = "unknown_key"
    #: The signature exists, the key is known, and the arithmetic failed. This
    #: is the one that is never benign.
    INVALID = "invalid"
    #: Signed with an algorithm this version does not implement. Refused rather
    #: than ignored: "I cannot check this" must not read as "this is fine".
    UNSUPPORTED_ALGORITHM = "unsupported_algorithm"


@dataclass(frozen=True, slots=True)
class SignatureVerdict:
    status: SignatureStatus
    detail: str
    key_identifier: str | None = None

    @property
    def verified(self) -> bool:
        return self.status is SignatureStatus.TRUSTED

    def blocks(self, *, require_signature: bool) -> bool:
        """Does this stop an importer using the snapshot?

        A failed signature always blocks. A missing or unrecognised one blocks
        only where the operator has decided signatures are mandatory — which is
        how a deployment adopts signing without being unable to read anything
        published before it started.
        """
        if self.status is SignatureStatus.TRUSTED:
            return False
        if self.status in (SignatureStatus.INVALID, SignatureStatus.UNSUPPORTED_ALGORITHM):
            return True
        return require_signature

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "detail": self.detail,
            "key_identifier": self.key_identifier,
        }


def evaluate_signature(
    *,
    signature: str | None,
    key_identifier: str | None,
    algorithm: str = SIGNING_ALGORITHM,
    trusted_key_ids: frozenset[str],
    verify,
) -> SignatureVerdict:
    """Decide what a manifest's signature is worth.

    ``verify`` is a callable returning whether the signature checks out against
    the named key. It is injected because the arithmetic belongs to an adapter
    and the *policy* — which keys, what an absence means — belongs here.
    """
    if not signature or not key_identifier:
        return SignatureVerdict(
            status=SignatureStatus.UNSIGNED,
            detail=(
                "this snapshot carries no signature, so its bytes are intact and "
                "nothing establishes who approved them"
            ),
        )

    if algorithm != SIGNING_ALGORITHM:
        return SignatureVerdict(
            status=SignatureStatus.UNSUPPORTED_ALGORITHM,
            detail=(
                f"signed with {algorithm!r}, which this version cannot verify; "
                "refusing rather than reporting an unchecked signature as acceptable"
            ),
            key_identifier=key_identifier,
        )

    if key_identifier not in trusted_key_ids:
        # Never resolved from the archive. A key fetched from beside the
        # manifest is a key the manifest's author chose.
        return SignatureVerdict(
            status=SignatureStatus.UNKNOWN_KEY,
            detail=(
                f"signed by {key_identifier!r}, which this deployment was not "
                "configured to trust. Add the key deliberately if the publisher is "
                "who you expect; a key taken from the archive proves nothing"
            ),
            key_identifier=key_identifier,
        )

    if not verify():
        return SignatureVerdict(
            status=SignatureStatus.INVALID,
            detail=(
                f"the signature does not verify against {key_identifier!r}: the "
                "manifest was altered after signing, or it was not signed by that key"
            ),
            key_identifier=key_identifier,
        )

    return SignatureVerdict(
        status=SignatureStatus.TRUSTED,
        detail=f"signed by {key_identifier!r}, which this deployment trusts",
        key_identifier=key_identifier,
    )
