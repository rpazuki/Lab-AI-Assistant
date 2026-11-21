"""How a deployment names the keys it trusts and the archive it uses.

Both are read by `lab archive` commands on a server, through `load_settings`
rather than `Settings()`. That difference is the point of the first group of
tests: `load_settings` passes the environment value through as a raw string,
while the settings library parses the same variable on its own, and the two once
disagreed — `id=base64` crashed the settings library's JSON decoder, and a JSON
object survived it only to be split on `=` into a key named `{"id":"key`. A test
that built `Settings()` directly saw neither.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lab_assistant.adapters.signing import Ed25519Verifier, generate_keypair
from lab_assistant.config import Settings, load_settings


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Trusted keys as a server reads them: the variable in the real process
    environment, where both `load_settings` and the settings library see it."""

    def read(value: str) -> dict[str, str]:
        monkeypatch.setenv("LAB_ARCHIVE_TRUSTED_KEYS", value)
        settings, _ = load_settings(root=tmp_path)
        return settings.archive_trusted_keys

    return read


@pytest.fixture
def public_key() -> str:
    # A real key, so a padding `=` is present and the verifier is the judge.
    return generate_keypair()[1]


class TestTrustedKeys:
    def test_the_documented_spelling_works_through_load_settings(
        self, keys, public_key: str
    ) -> None:
        found = keys(f"rlalab-2026={public_key}")
        assert found == {"rlalab-2026": public_key}
        Ed25519Verifier(found)

    def test_json_works_through_load_settings(self, keys, public_key: str) -> None:
        found = keys(f'{{"rlalab-2026": "{public_key}"}}')
        assert found == {"rlalab-2026": public_key}
        Ed25519Verifier(found)

    def test_several_keys_in_either_spelling(self, keys) -> None:
        first, second = generate_keypair()[1], generate_keypair()[1]
        listed = keys(f"a={first}, b={second}")
        as_json = keys(f'{{"a":"{first}","b":"{second}"}}')
        assert listed == as_json == {"a": first, "b": second}

    def test_both_spellings_work_from_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch, public_key: str
    ) -> None:
        # The settings library's own environment source, which `load_settings`
        # does not bypass: it reads the variable too, and must not decode it.
        for value in (f"k={public_key}", f'{{"k":"{public_key}"}}'):
            monkeypatch.setenv("LAB_ARCHIVE_TRUSTED_KEYS", value)
            assert Settings().archive_trusted_keys == {"k": public_key}

    def test_the_lower_case_workaround_keeps_working(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, public_key: str
    ) -> None:
        # What operators were told to write while the bug stood. It must not
        # break when the fix is deployed under it.
        monkeypatch.setenv("lab_archive_trusted_keys", f'{{"k":"{public_key}"}}')
        monkeypatch.delenv("LAB_ARCHIVE_TRUSTED_KEYS", raising=False)
        settings, _ = load_settings(root=tmp_path)
        assert settings.archive_trusted_keys == {"k": public_key}

    @pytest.mark.parametrize(
        "value",
        ['{"k": ', '["k", "v"]', '{"k": 3}', "no-separator"],
    )
    def test_a_malformed_value_is_refused(self, keys, value: str) -> None:
        with pytest.raises(ValidationError):
            keys(value)


class TestArchiveStore:
    def test_unset_means_the_archive_is_the_object_store(self, tmp_path: Path) -> None:
        settings, _ = load_settings(root=tmp_path, environ={})
        assert settings.archive_store_settings() is settings

    def test_set_it_replaces_only_the_store_location(self, tmp_path: Path) -> None:
        settings, _ = load_settings(
            root=tmp_path,
            environ={
                "LAB_OBJECT_STORE_KIND": "s3",
                "LAB_OBJECT_STORE_BUCKET": "private-evidence",
                "LAB_OBJECT_STORE_ACCESS_KEY": "private-access",
                "LAB_OBJECT_STORE_SECRET_KEY": "private-secret",
                "LAB_ARCHIVE_STORE_KIND": "GCS",
                "LAB_ARCHIVE_STORE_BUCKET": "shared-archive",
                "LAB_ARCHIVE_STORE_PROJECT": "lab-project",
                "LAB_ARCHIVE_STORE_PREFIX": "archive",
                "LAB_ARCHIVE_STORE_CREDENTIALS_PATH": "/etc/lab-assistant/gcs.json",
            },
        )
        archive = settings.archive_store_settings()
        assert archive.object_store_kind == "gcs"
        assert archive.object_store_bucket == "shared-archive"
        assert archive.object_store_project == "lab-project"
        assert archive.object_store_prefix == "archive"
        assert archive.object_store_credentials_path == "/etc/lab-assistant/gcs.json"
        # The private store's keys never travel to the archive.
        assert archive.object_store_access_key is None
        assert archive.object_store_secret_key is None
        # And the installation's own store is untouched.
        assert settings.object_store_kind == "s3"
        assert settings.object_store_bucket == "private-evidence"

    def test_an_unknown_kind_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            load_settings(root=tmp_path, environ={"LAB_ARCHIVE_STORE_KIND": "azure"})

    def test_the_archive_commands_use_the_archive_store(self, tmp_path: Path) -> None:
        pytest.importorskip("google.cloud.storage")
        from lab_assistant.adapters.object_store.control import (
            FilesystemControlStore,
            GcsControlStore,
        )
        from lab_assistant.cli.main import _archive_control

        environ = {
            "LAB_OBJECT_STORE_KIND": "filesystem",
            "LAB_OBJECT_STORE_ROOT": str(tmp_path / "objects"),
        }
        private, _ = load_settings(root=tmp_path, environ=environ)
        assert isinstance(_archive_control(private), FilesystemControlStore)

        shared, _ = load_settings(
            root=tmp_path,
            environ={
                **environ,
                "LAB_ARCHIVE_STORE_KIND": "gcs",
                "LAB_ARCHIVE_STORE_BUCKET": "shared-archive",
                "LAB_ARCHIVE_STORE_PREFIX": "archive",
                # An emulator endpoint: anonymous, so no credential is resolved.
                "LAB_ARCHIVE_STORE_ENDPOINT": "http://127.0.0.1:9",
            },
        )
        control = _archive_control(shared)
        assert isinstance(control, GcsControlStore)
        assert control._config.bucket == "shared-archive"
        assert control._config.prefix == "archive/"
