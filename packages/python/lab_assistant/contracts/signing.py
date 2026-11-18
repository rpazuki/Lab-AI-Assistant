"""Who approved a snapshot, as distinct from whether its bytes survived.

A digest proves the bytes did not rot. It says nothing about who produced them.
Anyone who can write to the bucket can write a manifest with a perfectly
correct digest, so bucket administration must never be read as evidence that a
steward reviewed a claim — and in a shared-archive deployment the set of people
who can write to a bucket is not the set of people entitled to publish science.

So the manifest carries a signature, and this module defines exactly what that
signature covers.

**It covers the digest and restates the identity.** The identity and sequence
fields are *already* inside the manifest, so `signing_digest()` covers them and
a replay to another library or sequence breaks the signature through the digest
alone. Restating them in the statement is therefore redundant today, and it is
kept deliberately for one reason: the digest's coverage is a property of the
manifest's shape, and shapes change. If a later version ever narrows the digest
— over the catalog shards rather than the whole manifest, say, which is a
plausible optimisation — the signature would silently stop binding the library
and the sequence, and a snapshot could be replayed under another identity with
a perfectly valid signature. The statement makes that binding explicit rather
than inherited, so narrowing the digest cannot quietly remove it.

`test_the_signing_digest_covers_identity` is the test that fails if somebody
narrows it. The redundancy is the belt; that test is the braces.

**Ed25519**, for one property above the others: it is deterministic. There is
no per-signature nonce, so there is no way to leak a private key by signing
twice with bad randomness — a failure mode that has taken out real systems and
that nothing in this codebase would otherwise notice.

**The signature is not inside what it signs.** `signature` and
`key_identifier` are excluded from the digest, or signing would change the
thing being signed. `SnapshotManifest.signing_digest()` is the authority on
that exclusion, and the head records *that* digest rather than the file's, so
the identity of a snapshot does not change when somebody signs it.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from lab_assistant.contracts.canonical import canonical_json

__all__ = [
    "POLICY_STATEMENT_TYPE",
    "SIGNING_ALGORITHM",
    "SIGNING_ENVELOPE_VERSION",
    "PolicySigningStatement",
    "SigningStatement",
    "policy_statement_for",
    "statement_for",
]

#: The only algorithm this version produces or accepts. Named in the envelope
#: so a later version can add one without a reader having to guess which it is
#: looking at — and so an unknown algorithm is a refusal rather than a
#: best-effort verification that silently passes.
SIGNING_ALGORITHM = "ed25519"

SIGNING_ENVELOPE_VERSION = "1.0"


class SigningStatement(BaseModel):
    """Exactly what a publisher's key attests to.

    Every field is here because leaving it out makes a signature replayable
    somewhere it should not be:

    - without ``manifest_digest`` the statement says nothing about content;
    - without ``library_id`` a signature for one library vouches for another;
    - without ``snapshot_id`` it vouches for any snapshot with the same bytes;
    - without ``writer_epoch`` a signature made before a fenced handover is
      still good afterwards, which is the one thing fencing exists to stop;
    - without ``publication_sequence`` an old signed snapshot can be replayed
      as a newer publication.
    """

    model_config = {"frozen": True}

    envelope_version: str = SIGNING_ENVELOPE_VERSION
    algorithm: str = SIGNING_ALGORITHM
    manifest_digest: str
    library_id: UUID
    snapshot_id: UUID
    origin_installation_id: UUID
    writer_epoch: int
    publication_sequence: int

    def to_bytes(self) -> bytes:
        """The canonical bytes that are signed and verified.

        Canonical because a signature over "whatever this parser produced
        today" is a signature that stops verifying when a library changes how
        it orders keys.
        """
        return canonical_json(self.model_dump(mode="python"))


def statement_for(manifest) -> SigningStatement:
    """The statement for a manifest, derived rather than supplied.

    Taking these from the manifest itself means a signer cannot be handed a
    statement that disagrees with the thing it is signing.
    """
    return SigningStatement(
        manifest_digest=manifest.signing_digest(),
        library_id=manifest.library_id,
        snapshot_id=manifest.snapshot_id,
        origin_installation_id=manifest.origin_installation_id,
        writer_epoch=manifest.writer_epoch,
        publication_sequence=manifest.publication_sequence,
    )


#: Names what a policy statement is about. A snapshot statement and a policy
#: statement are different claims made with the same key, and a signature over
#: one must never verify as the other.
POLICY_STATEMENT_TYPE = "policy_checkpoint"


class PolicySigningStatement(BaseModel):
    """Exactly what a publisher's key attests to about a policy checkpoint.

    **Domain separation.** The publisher signs snapshots and checkpoints with
    one key, so the two statements must not be confusable. This one carries
    ``statement_type`` and field names the snapshot statement does not, so
    their canonical bytes can never coincide — a signature lifted from a
    snapshot onto a checkpoint, or back, fails rather than vouching for
    something its signer never saw.

    The rest follows `SigningStatement`: the digest is what binds, because every
    field here is already inside the checkpoint the digest covers, and the
    restatement is insurance against a later version narrowing that digest.
    The validity bounds are restated for the same reason. They are the fields
    a replayed checkpoint would most want to change: an old checkpoint
    re-dated to look fresh is how a revoked licence comes back.
    """

    model_config = {"frozen": True}

    envelope_version: str = SIGNING_ENVELOPE_VERSION
    algorithm: str = SIGNING_ALGORITHM
    statement_type: str = POLICY_STATEMENT_TYPE
    checkpoint_digest: str
    library_id: UUID
    checkpoint_id: UUID
    origin_installation_id: UUID
    writer_epoch: int
    policy_sequence: int
    valid_until: datetime
    public_valid_until: datetime

    def to_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="python"))


def policy_statement_for(checkpoint) -> PolicySigningStatement:
    """The statement for a checkpoint, derived from it rather than supplied."""
    return PolicySigningStatement(
        checkpoint_digest=checkpoint.signing_digest(),
        library_id=checkpoint.library_id,
        checkpoint_id=checkpoint.checkpoint_id,
        origin_installation_id=checkpoint.origin_installation_id,
        writer_epoch=checkpoint.writer_epoch,
        policy_sequence=checkpoint.policy_sequence,
        valid_until=checkpoint.valid_until,
        public_valid_until=checkpoint.public_valid_until,
    )
