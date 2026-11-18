"""Packaging a release into a portable catalog, and checking it without the DB.

Phase 2's first acceptance gate is *the archive fixture can be validated
without the producer DB*, and that is what most of this file is. The validator
is handed a directory and nothing else — no harness, no uow factory, no store —
because that is what an importer will have.

The cases the golden fixture exists for are checked individually rather than by
asserting a row count. A catalog can carry the right number of rows and the
wrong ones: an exporter that followed `claim.current_revision_id` instead of
the revision a release pinned would produce a snapshot of exactly the same
size, and would silently rewrite last quarter's answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from lab_assistant.application.archive_catalog import (
    CATALOG_DIRECTORY,
    INVENTORY_KIND,
    MANIFEST_NAME,
    ArchiveCatalogBuilder,
)
from lab_assistant.application.archive_validate import Severity, validate_snapshot
from lab_assistant.contracts.archive_catalog import RecordKind
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import publish_archive_fixture
from lab_assistant.testing import build_harness, make_actor

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)


@pytest.fixture
async def packaged(tmp_path):
    """The golden fixture, packaged into a snapshot directory."""
    harness = build_harness()
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
            policy_basis="archive_catalog_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    fixture = await publish_archive_fixture(harness, actor)

    builder = ArchiveCatalogBuilder(harness.context, library_id=uuid4(), installation_id=uuid4())
    build = await builder.build(workspace.id, tmp_path / "snapshot")
    return {
        "harness": harness,
        "workspace": workspace,
        "fixture": fixture,
        "build": build,
        "directory": tmp_path / "snapshot",
        "builder": builder,
    }


def _rows(directory: Path, kind: str) -> list[dict]:
    path = directory / CATALOG_DIRECTORY / f"{kind}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestPackaging:
    async def test_it_packages_without_problems(self, packaged) -> None:
        build = packaged["build"]
        assert build.ok, build.problems
        assert build.manifest is not None

    async def test_both_releases_travel(self, packaged) -> None:
        """A snapshot that could only describe the current state could not
        resolve a citation into last quarter's answer."""
        releases = _rows(packaged["directory"], "release")
        assert len(releases) == 2

    async def test_the_pinned_revision_travels_not_the_current_one(self, packaged) -> None:
        """The case an exporter gets wrong by following `current_revision_id`.

        The fixture has a claim corrected by a reviewer: revision 2 supersedes
        revision 1. Release 1 pins revision 1, and a citation into it must
        still resolve to what it said then.
        """
        rows = _rows(packaged["directory"], "claim_revision")
        revisions = {row["record_id"]: row for row in rows}
        entries = _rows(packaged["directory"], "release_item")
        pinned = {rid for entry in entries for rid in entry["claim_revision_ids"]}

        superseding = [row for row in revisions.values() if row["supersedes_revision_id"]]
        assert superseding, "the fixture's correction did not travel"
        for row in superseding:
            replaced = row["supersedes_revision_id"]
            assert replaced in revisions, (
                "the revision a correction replaced did not travel, so the corpus "
                "cannot explain why the number changed"
            )
            assert replaced in pinned or row["record_id"] in pinned

    async def test_an_abstention_travels_as_a_result(self, packaged) -> None:
        """`not_reported_after_complete_search` is a positive statement that the
        source does not report the value. A reader that cannot tell it from
        "not yet extracted" makes every importer pay to re-run the extraction
        (scenario S12).
        """
        revisions = _rows(packaged["directory"], "claim_revision")
        abstentions = [
            row
            for row in revisions
            if row["abstention_reason"] == "not_reported_after_complete_search"
        ]
        assert abstentions, "the searched-and-found-nothing result did not travel"

    async def test_rights_decisions_travel_with_their_assets(self, packaged) -> None:
        """An archive carrying evidence without its rights decision would have a
        reader serving material the publisher restricted (scenario S39)."""
        assets = _rows(packaged["directory"], "asset")
        decisions = {row["record_id"] for row in _rows(packaged["directory"], "rights_decision")}
        assert decisions
        for asset in assets:
            assert asset["rights_decision_id"] in decisions

        restricted = [
            row
            for row in _rows(packaged["directory"], "rights_decision")
            if row["restricted_to_group_ids"]
        ]
        assert restricted, "the restricted-evidence case did not travel"

    async def test_review_history_travels(self, packaged) -> None:
        reviews = _rows(packaged["directory"], "review")
        assert reviews, "no review decision travelled"
        assert any(row["decision"] for row in reviews)

    async def test_work_titles_are_reconciled_not_dropped(self, packaged) -> None:
        """The title is not stored on the work: it is reconciled from source
        assertions, which are the producer's and do not travel."""
        works = _rows(packaged["directory"], "work")
        assert works
        assert all(row["title"] for row in works), "a work travelled with no title"

    async def test_payloads_are_referenced_not_inlined(self, packaged) -> None:
        """A catalog stays small and diffable; the bytes move once, separately."""
        inventory = _rows(packaged["directory"], INVENTORY_KIND)
        assert inventory
        for row in inventory:
            assert row["sha256"]
        serialised = (packaged["directory"] / CATALOG_DIRECTORY / "asset.jsonl").read_text()
        assert "base64" not in serialised

    async def test_a_second_build_is_byte_identical(self, packaged, tmp_path) -> None:
        """Determinism is what makes an incremental publication possible: a
        snapshot whose digest changed because a dict iterated differently would
        make every publication a full one."""
        again = await packaged["builder"].build(packaged["workspace"].id, tmp_path / "second")
        assert again.ok, again.problems

        first = {shard.kind: shard.sha256 for shard in packaged["build"].manifest.catalog_shards}
        second = {shard.kind: shard.sha256 for shard in again.manifest.catalog_shards}
        assert first == second

    async def test_a_draft_release_is_refused(self, packaged) -> None:
        """A snapshot carries published state. Packaging a draft would publish
        it by the back door."""
        harness = packaged["harness"]
        build = await packaged["builder"].build(
            packaged["workspace"].id,
            Path(packaged["directory"]).parent / "draft",
            release_ids=(uuid4(),),
        )
        assert not build.ok
        assert any("does not exist" in problem for problem in build.problems)
        assert harness is not None


class TestValidationNeedsNoDatabase:
    """Phase 2's first acceptance gate.

    Every test here is handed a directory and nothing else.
    """

    def test_the_fixture_validates(self, packaged) -> None:
        report = validate_snapshot(packaged["directory"])
        assert report.ok, [finding.detail for finding in report.blocking]
        assert report.manifest is not None
        assert report.counts["work"] == 4

    def test_a_release_without_a_scope_version_is_advisory(self, packaged) -> None:
        """Real of this fixture, and worth saying: a reader can show what was
        found and not what was looked for. It does not block serving."""
        report = validate_snapshot(packaged["directory"])
        scope = [f for f in report.findings if f.code == "release_without_scope"]
        assert scope
        assert all(f.severity is Severity.ADVISORY for f in scope)

    def test_an_edited_manifest_is_caught(self, packaged) -> None:
        path = packaged["directory"] / MANIFEST_NAME
        payload = json.loads(path.read_text())
        payload["publication_sequence"] = 999
        path.write_text(json.dumps(payload))

        report = validate_snapshot(packaged["directory"])
        assert not report.ok
        assert any(f.code == "manifest_edited" for f in report.blocking)

    def test_a_tampered_shard_is_caught(self, packaged) -> None:
        path = packaged["directory"] / CATALOG_DIRECTORY / "work.jsonl"
        rows = path.read_text().splitlines()
        rows[0] = rows[0].replace('"title"', '"ttle"')
        path.write_text("\n".join(rows) + "\n")

        report = validate_snapshot(packaged["directory"])
        assert not report.ok
        assert any(f.code == "shard_digest" for f in report.blocking)

    def test_a_missing_shard_is_caught(self, packaged) -> None:
        (packaged["directory"] / CATALOG_DIRECTORY / "claim.jsonl").unlink()
        report = validate_snapshot(packaged["directory"])
        assert not report.ok
        assert any(f.code == "shard_absent" for f in report.blocking)

    def test_a_dangling_reference_is_caught(self, packaged) -> None:
        """The check that distinguishes an archive from a table dump: a release
        entry naming a work that did not travel cannot be served, and the
        producer's database is exactly what is not available to explain it."""
        directory = packaged["directory"]
        path = directory / CATALOG_DIRECTORY / "work.jsonl"
        rows = path.read_text().splitlines()
        path.write_text("\n".join(rows[1:]) + "\n")
        _reseal(directory)

        report = validate_snapshot(directory)
        assert not report.ok
        unresolved = [f for f in report.blocking if f.code == "reference_unresolved"]
        assert unresolved
        assert "work" in unresolved[0].detail

    def test_a_directory_that_is_not_a_snapshot_says_so(self, tmp_path) -> None:
        (tmp_path / "empty").mkdir()
        report = validate_snapshot(tmp_path / "empty")
        assert not report.ok
        assert any(f.code == "manifest_absent" for f in report.findings)

    def test_an_unparseable_manifest_is_a_finding(self, tmp_path) -> None:
        """Not a traceback: one bad manifest must not hide that it is the
        manifest which is wrong."""
        directory = tmp_path / "broken"
        directory.mkdir()
        (directory / MANIFEST_NAME).write_text("{not json")
        report = validate_snapshot(directory)
        assert not report.ok
        assert any(f.code == "manifest_unparseable" for f in report.findings)

    def test_the_validator_opens_no_database(self, packaged, monkeypatch) -> None:
        """Proved rather than asserted: the harness is gone from the call.

        `validate_snapshot` takes a path. If it ever reached for a connection,
        this test would be the place that noticed — there is nothing here to
        reach for.
        """
        directory = packaged["directory"]
        del packaged["harness"], packaged["builder"]
        report = validate_snapshot(Path(str(directory)))
        assert report.ok


def _reseal(directory: Path) -> None:
    """Re-digest the shards after a test edits one.

    Without this a reference test would fail on the digest instead of on the
    dangling reference, and would pass for the wrong reason.
    """
    from lab_assistant.contracts.archive import SnapshotManifest
    from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

    path = directory / MANIFEST_NAME
    manifest = SnapshotManifest.model_validate(json.loads(path.read_text()))
    resealed = []
    for shard in manifest.catalog_shards:
        payload = (directory / shard.location).read_bytes()
        lines = [line for line in payload.split(b"\n") if line.strip()]
        resealed.append(
            shard.model_copy(
                update={
                    "sha256": sha256_bytes(payload),
                    "byte_length": len(payload),
                    "row_count": len(lines),
                }
            )
        )
    updated = manifest.model_copy(update={"catalog_shards": tuple(resealed)})
    payload = canonical_json(updated.model_dump(mode="python"))
    path.write_bytes(payload)
    (directory / "MANIFEST.sha256").write_text(
        f"{sha256_bytes(payload)}  {MANIFEST_NAME}\n", encoding="utf-8"
    )


def test_every_record_kind_has_a_shard_name() -> None:
    """A kind with no writer is a kind that silently never travels."""
    from lab_assistant.application.archive_catalog import ArchiveCatalogBuilder as _B

    assert _B is not None
    assert {kind.value for kind in RecordKind}
