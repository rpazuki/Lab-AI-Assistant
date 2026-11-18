"""Ed25519 signing and verification, over the maintained library.

The arithmetic only. Which keys to trust, and what an unsigned snapshot means,
are decisions and live in `domain/signing.py`.

Keys are held as raw 32-byte values, base64 in configuration and on disk. Not
PEM, not a certificate: there is no chain to validate here and no authority to
delegate to. A publisher's key is trusted because an operator put it in this
deployment's configuration, and a format that *looked* like it carried
authority would invite somebody to believe the file instead of the operator.

The private key never leaves the publisher. It is loaded from a path, so it can
come from a secret manager's mounted file rather than from an environment
variable that ends up in a process listing and a support bundle.
"""

from __future__ import annotations

import base64
from pathlib import Path

from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = ["Ed25519Signer", "Ed25519Verifier", "generate_keypair"]


def _decode(value: str, *, what: str) -> bytes:
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except Exception as exc:
        raise LabAssistantError(
            f"{what} is not valid base64",
            context={"hint": "keys are raw 32-byte Ed25519 values, base64 encoded"},
        ).with_class(ErrorClass.INVALID_CONTENT) from exc
    if len(raw) != 32:
        raise LabAssistantError(
            f"{what} is {len(raw)} bytes; an Ed25519 key is 32",
        ).with_class(ErrorClass.INVALID_CONTENT)
    return raw


class Ed25519Signer:
    """Signs a publisher's statements. Publisher-side only."""

    def __init__(self, private_key_base64: str, *, key_identifier: str) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._key = Ed25519PrivateKey.from_private_bytes(
            _decode(private_key_base64, what="the archive signing key")
        )
        self.key_identifier = key_identifier

    @classmethod
    def from_path(cls, path: Path, *, key_identifier: str) -> Ed25519Signer:
        """Load from a file, which is how a secret manager delivers one.

        Deliberately not from an environment variable: those reach process
        listings, crash dumps and support bundles, and a signing key that has
        been in one is a key to rotate.
        """
        path = Path(path)
        if not path.is_file():
            raise LabAssistantError(
                "the archive signing key file does not exist",
                context={"path": str(path)},
            ).with_class(ErrorClass.NOT_FOUND)
        return cls(path.read_text(encoding="utf-8"), key_identifier=key_identifier)

    def public_key_base64(self) -> str:
        from cryptography.hazmat.primitives import serialization

        return base64.b64encode(
            self._key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode("ascii")

    def sign(self, payload: bytes) -> str:
        return base64.b64encode(self._key.sign(payload)).decode("ascii")


class Ed25519Verifier:
    """Checks signatures against the keys this deployment was told to trust."""

    def __init__(self, public_keys: dict[str, str]) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        self._keys = {
            identifier: Ed25519PublicKey.from_public_bytes(
                _decode(value, what=f"trusted key {identifier!r}")
            )
            for identifier, value in public_keys.items()
        }

    @property
    def trusted_key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def verify(self, payload: bytes, signature: str, *, key_identifier: str) -> bool:
        """True when the signature checks out. Never raises for a bad one.

        A verification failure is an ordinary answer here — the caller turns it
        into a finding with the right severity — so this returns a boolean and
        keeps exceptions for "I could not even try".
        """
        from cryptography.exceptions import InvalidSignature

        key = self._keys.get(key_identifier)
        if key is None:
            return False
        try:
            key.verify(base64.b64decode(signature, validate=True), payload)
        except (InvalidSignature, ValueError):
            return False
        return True


def generate_keypair() -> tuple[str, str]:
    """A fresh (private, public) pair, base64. For setup and for tests.

    Here rather than in a script so the one way to make a key in this system is
    the one the adapter can read back.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return (
        base64.b64encode(private_bytes).decode("ascii"),
        base64.b64encode(public_bytes).decode("ascii"),
    )
