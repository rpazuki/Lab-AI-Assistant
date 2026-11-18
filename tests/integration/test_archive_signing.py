"""Who approved a snapshot, as distinct from whether its bytes survived.

Every other check in the validator establishes integrity. This is the only one
that establishes authority, and the two are routinely confused: anyone who can
write to the bucket can produce a snapshot whose every digest is correct.

The replay tests are worth reading, and so is what they do *not* prove. A
snapshot's identity and sequence live inside its manifest, so the signing
digest already covers them: lifting a signature onto another library breaks it
through the digest, not through the statement's identity fields. Those fields
are deliberate redundancy against a future version narrowing the digest, and
`test_the_signing_digest_covers_identity` is the test that notices if one does.

That distinction was found by breaking the signing statement down to a bare
digest and watching every replay test still pass. A test that passes for the
wrong reason is worse than no test, because it is evidence.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from lab_assistant.adapters.signing import (
    Ed25519Signer,
    Ed25519Verifier,
    generate_keypair,
)
from lab_assistant.application.archive_catalog import MANIFEST_NAME, ArchiveCatalogBuilder
from lab_assistant.application.archive_validate import validate_snapshot
from lab_assistant.contracts.archive import SnapshotManifest
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.contracts.signing import statement_for
from lab_assistant.domain.signing import SignatureStatus, evaluate_signature
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import publish_archive_fixture, replicating_rights_policy
from lab_assistant.testing import build_harness, make_actor

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
)


@pytest.fixture
async def corpus():
    harness = build_harness(rights_policy=replicating_rights_policy())
    user = await harness.accounts.create_user(
        email="steward@lab.invalid", password="correct horse battery"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
    )
    admin = make_actor(user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN)
    grants = [
        await harness.accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=action,
            effect=Effect.ALLOW,
            policy_basis="signing_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    await publish_archive_fixture(harness, actor)
    return harness, workspace


@pytest.fixture
def publisher_key():
    private, public = generate_keypair()
    return {
        "signer": Ed25519Signer(private, key_identifier="rla-publisher-2026"),
        "verifier": Ed25519Verifier({"rla-publisher-2026": public}),
        "public": public,
    }


async def _package(corpus, tmp_path, *, signer=None, library_id=None, name="snapshot"):
    harness, workspace = corpus
    builder = ArchiveCatalogBuilder(
        harness.context,
        library_id=library_id or uuid4(),
        installation_id=uuid4(),
        signer=signer,
    )
    build = await builder.build(workspace.id, tmp_path / name)
    assert build.ok, build.problems
    return build, tmp_path / name


class TestSigningAndVerifying:
    async def test_a_signed_snapshot_verifies(self, corpus, tmp_path, publisher_key) -> None:
        _build, directory = await _package(corpus, tmp_path, signer=publisher_key["signer"])
        report = validate_snapshot(directory, verifier=publisher_key["verifier"])
        assert report.ok, [f.detail for f in report.blocking]
        assert not [f for f in report.findings if f.code.startswith("signature_")]

    async def test_signing_does_not_change_the_snapshot_identity(
        self, corpus, tmp_path, publisher_key
    ) -> None:
        """The signature is excluded from the digest it covers, so an unsigned
        snapshot and the same snapshot signed are the same snapshot."""
        unsigned, _ = await _package(corpus, tmp_path, name="unsigned")
        library = unsigned.manifest.library_id
        signed, _ = await _package(
            corpus,
            tmp_path,
            signer=publisher_key["signer"],
            library_id=library,
            name="signed",
        )
        # Same closure, same library: the signing digests agree even though one
        # carries a signature and the other does not.
        assert signed.manifest.signature
        assert unsigned.manifest.signature is None
        assert signed.manifest.catalog_shards == unsigned.manifest.catalog_shards

    async def test_an_unsigned_snapshot_is_reported_not_refused(
        self, corpus, tmp_path, publisher_key
    ) -> None:
        """Signing has to be adoptable: refusing everything published before it
        started would make it impossible to turn on."""
        _build, directory = await _package(corpus, tmp_path)
        report = validate_snapshot(directory, verifier=publisher_key["verifier"])
        assert report.ok
        assert any(f.code == "signature_unsigned" for f in report.findings)

    async def test_an_unsigned_snapshot_is_refused_where_required(
        self, corpus, tmp_path, publisher_key
    ) -> None:
        _build, directory = await _package(corpus, tmp_path)
        report = validate_snapshot(
            directory, verifier=publisher_key["verifier"], require_signature=True
        )
        assert not report.ok
        assert any(f.code == "signature_unsigned" for f in report.blocking)

    async def test_a_signature_is_not_checked_without_trusted_keys(
        self, corpus, tmp_path, publisher_key
    ) -> None:
        """ "Nobody told me whose key to expect" is not "this is fine"."""
        _build, directory = await _package(corpus, tmp_path, signer=publisher_key["signer"])
        report = validate_snapshot(directory)
        assert any(f.code == "signature_unchecked" for f in report.findings)


class TestForgeryAndTampering:
    async def test_an_edited_manifest_fails_its_signature(
        self, corpus, tmp_path, publisher_key
    ) -> None:
        """A digest check would also catch this. The point is that the
        signature catches it too, so an attacker who recomputes every digest
        still cannot pass."""
        _build, directory = await _package(corpus, tmp_path, signer=publisher_key["signer"])
        path = directory / MANIFEST_NAME
        manifest = SnapshotManifest.model_validate(json.loads(path.read_text()))
        altered = manifest.model_copy(update={"publication_sequence": 99})
        _reseal(path, altered)

        report = validate_snapshot(directory, verifier=publisher_key["verifier"])
        assert not report.ok
        assert any(f.code == "signature_invalid" for f in report.blocking)

    async def test_another_key_is_not_trusted(self, corpus, tmp_path) -> None:
        """An attacker can always sign with a key of their own. The question is
        whether a reader accepts it."""
        attacker_private, _attacker_public = generate_keypair()
        attacker = Ed25519Signer(attacker_private, key_identifier="rla-publisher-2026")
        _build, directory = await _package(corpus, tmp_path, signer=attacker)

        _private, real_public = generate_keypair()
        verifier = Ed25519Verifier({"rla-publisher-2026": real_public})
        report = validate_snapshot(directory, verifier=verifier)
        assert not report.ok
        assert any(f.code == "signature_invalid" for f in report.blocking)

    async def test_an_unknown_key_identifier_is_not_trusted(self, corpus, tmp_path) -> None:
        """Keys are pinned, never learned. A key named by the manifest is a key
        the manifest's author chose."""
        private, _public = generate_keypair()
        stranger = Ed25519Signer(private, key_identifier="somebody-else")
        _build, directory = await _package(corpus, tmp_path, signer=stranger)

        _p, trusted_public = generate_keypair()
        verifier = Ed25519Verifier({"rla-publisher-2026": trusted_public})
        report = validate_snapshot(directory, verifier=verifier)
        assert any(f.code == "signature_unknown_key" for f in report.findings)


class TestReplay:
    """The reason the statement carries identity and not only a digest."""

    async def test_a_signature_does_not_transfer_to_another_library(
        self, corpus, tmp_path, publisher_key
    ) -> None:
        """Lifting a signature onto a snapshot it was not made for fails.

        It fails through the digest, because the library id is inside the
        manifest the digest covers. That is the guarantee an importer relies
        on, so it is worth a test even though it is not the statement's
        identity fields doing the work.
        """
        signed, _ = await _package(
            corpus, tmp_path, signer=publisher_key["signer"], name="original"
        )
        other, other_dir = await _package(corpus, tmp_path, name="other-library")

        # Lift the signature across.
        forged = other.manifest.model_copy(
            update={
                "signature": signed.manifest.signature,
                "key_identifier": signed.manifest.key_identifier,
            }
        )
        _reseal(other_dir / MANIFEST_NAME, forged)

        report = validate_snapshot(other_dir, verifier=publisher_key["verifier"])
        assert not report.ok
        assert any(f.code == "signature_invalid" for f in report.blocking)

    def test_changing_any_identity_field_breaks_the_signature(self) -> None:
        """Change one and the signature must fail — today via the digest."""
        private, public = generate_keypair()
        signer = Ed25519Signer(private, key_identifier="k")
        verifier = Ed25519Verifier({"k": public})

        manifest = SnapshotManifest(
            library_id=uuid4(),
            source_workspace_id=uuid4(),
            snapshot_id=uuid4(),
            origin_installation_id=uuid4(),
            writer_epoch=2,
            publication_sequence=5,
        )
        signature = signer.sign(statement_for(manifest).to_bytes())

        for field, value in (
            ("library_id", uuid4()),
            ("snapshot_id", uuid4()),
            ("origin_installation_id", uuid4()),
            ("writer_epoch", 3),
            ("publication_sequence", 6),
        ):
            moved = manifest.model_copy(update={field: value})
            assert not verifier.verify(
                statement_for(moved).to_bytes(), signature, key_identifier="k"
            ), f"the signature survived a change to {field}"

    def test_the_signing_digest_covers_identity(self) -> None:
        """The invariant the statement's redundancy is insurance for.

        Every identity and sequence field must change the signing digest. If a
        later version narrows the digest — over the catalog shards rather than
        the whole manifest, which is a plausible optimisation — this fails, and
        whoever narrowed it finds out here rather than when a snapshot is
        replayed under somebody else's library with a valid signature.
        """
        manifest = SnapshotManifest(
            library_id=uuid4(),
            source_workspace_id=uuid4(),
            snapshot_id=uuid4(),
            origin_installation_id=uuid4(),
            writer_epoch=1,
            publication_sequence=1,
        )
        baseline = manifest.signing_digest()
        for field, value in (
            ("library_id", uuid4()),
            ("snapshot_id", uuid4()),
            ("origin_installation_id", uuid4()),
            ("writer_epoch", 7),
            ("publication_sequence", 7),
        ):
            moved = manifest.model_copy(update={field: value})
            assert moved.signing_digest() != baseline, (
                f"the signing digest does not cover {field}, so a signature would "
                "transfer to a snapshot with a different one"
            )

    def test_the_signing_digest_ignores_the_signature(self) -> None:
        """Otherwise signing would change the thing being signed."""
        manifest = SnapshotManifest(
            library_id=uuid4(),
            source_workspace_id=uuid4(),
            snapshot_id=uuid4(),
            origin_installation_id=uuid4(),
            writer_epoch=1,
            publication_sequence=1,
        )
        signed = manifest.model_copy(update={"signature": "whatever", "key_identifier": "k"})
        assert signed.signing_digest() == manifest.signing_digest()

    def test_a_signature_made_before_a_fencing_does_not_survive_it(self) -> None:
        """The epoch is in the statement, so a signature produced by the old
        publisher cannot vouch for a publication after the handover."""
        private, public = generate_keypair()
        signer = Ed25519Signer(private, key_identifier="k")
        verifier = Ed25519Verifier({"k": public})

        before = SnapshotManifest(
            library_id=uuid4(),
            source_workspace_id=uuid4(),
            snapshot_id=uuid4(),
            origin_installation_id=uuid4(),
            writer_epoch=1,
            publication_sequence=9,
        )
        signature = signer.sign(statement_for(before).to_bytes())
        after = before.model_copy(update={"writer_epoch": 2})
        assert not verifier.verify(statement_for(after).to_bytes(), signature, key_identifier="k")


class TestTheTrustPolicy:
    def test_an_unsupported_algorithm_is_refused_not_ignored(self) -> None:
        """ "I cannot check this" must not read as "this is fine"."""
        verdict = evaluate_signature(
            signature="abc",
            key_identifier="k",
            algorithm="rsa-pkcs1-md5",
            trusted_key_ids=frozenset({"k"}),
            verify=lambda: True,
        )
        assert verdict.status is SignatureStatus.UNSUPPORTED_ALGORITHM
        assert verdict.blocks(require_signature=False)

    def test_a_bad_signature_always_blocks(self) -> None:
        """Regardless of whether signatures are required: a signature that does
        not verify is an active claim that failed."""
        verdict = evaluate_signature(
            signature="abc",
            key_identifier="k",
            trusted_key_ids=frozenset({"k"}),
            verify=lambda: False,
        )
        assert verdict.status is SignatureStatus.INVALID
        assert verdict.blocks(require_signature=False)

    def test_unsigned_blocks_only_where_required(self) -> None:
        verdict = evaluate_signature(
            signature=None,
            key_identifier=None,
            trusted_key_ids=frozenset(),
            verify=lambda: True,
        )
        assert verdict.status is SignatureStatus.UNSIGNED
        assert not verdict.blocks(require_signature=False)
        assert verdict.blocks(require_signature=True)

    def test_rotation_is_adding_a_key_not_trusting_the_archive(self) -> None:
        """Two keys trusted at once is how a rotation happens without an
        outage; the old one is removed when the operator decides, not when a
        snapshot says so."""
        old_private, old_public = generate_keypair()
        new_private, new_public = generate_keypair()
        during = Ed25519Verifier({"k-2026": old_public, "k-2027": new_public})
        assert during.trusted_key_ids == frozenset({"k-2026", "k-2027"})

        payload = b"a statement"
        for identifier, private in (("k-2026", old_private), ("k-2027", new_private)):
            signature = Ed25519Signer(private, key_identifier=identifier).sign(payload)
            assert during.verify(payload, signature, key_identifier=identifier)

        after = Ed25519Verifier({"k-2027": new_public})
        retired = Ed25519Signer(old_private, key_identifier="k-2026").sign(payload)
        assert not after.verify(payload, retired, key_identifier="k-2026")


def _reseal(path, manifest: SnapshotManifest) -> None:
    """Write an altered manifest and its digest file.

    So a tampering test fails on the signature rather than on the digest beside
    it, which would pass for the wrong reason.
    """
    payload = canonical_json(manifest.model_dump(mode="python"))
    path.write_bytes(payload)
    (path.parent / "MANIFEST.sha256").write_text(
        f"{sha256_bytes(payload)}  {MANIFEST_NAME}\n", encoding="utf-8"
    )
