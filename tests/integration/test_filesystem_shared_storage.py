"""The VM API and workers must see each other's objects without restarting."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
from lab_assistant.config import LabEnv, Settings
from lab_assistant.contracts.errors import LabAssistantError


def _write(root, workspace, payload):
    async def work():
        store = FilesystemObjectStore(Path(root))
        record = await store.put(payload, workspace_id=workspace, object_class="test")
        return record.id

    return asyncio.run(work())


async def test_live_instances_see_new_records_and_tombstones(tmp_path):
    api = FilesystemObjectStore(tmp_path)
    worker = FilesystemObjectStore(tmp_path)
    workspace = uuid4()
    item = await api.put(b"shared payload", workspace_id=workspace, object_class="test")
    assert await worker.get(item.id) == b"shared payload"
    await worker.tombstone(item.id, reason="test deletion")
    assert (await api.head(item.id)).tombstoned
    with pytest.raises(FileNotFoundError):
        await api.get(item.id)


async def test_concurrent_process_writes_remain_visible_and_deduplicated(tmp_path):
    observer = FilesystemObjectStore(tmp_path)
    workspace = uuid4()
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_write, str(tmp_path), workspace, f"payload {i % 4}".encode())
            for i in range(16)
        ]
        ids = [future.result(timeout=30) for future in futures]
    assert len(set(ids)) == 4
    for i, object_id in enumerate(ids):
        assert await observer.get(object_id) == f"payload {i % 4}".encode()
    assert len((tmp_path / "index.jsonl").read_text().splitlines()) == 4


async def test_corrupt_local_payload_is_not_served(tmp_path):
    store = FilesystemObjectStore(tmp_path)
    item = await store.put(b"original", workspace_id=None, object_class="test")
    (tmp_path / item.key).write_bytes(b"corrupt")
    with pytest.raises(LabAssistantError, match="digest"):
        await store.get(item.id)


def test_filesystem_server_requires_explicit_opt_in():
    settings = Settings(
        env=LabEnv.SERVER,
        secret_key="example-only",
        llm_provider="anthropic",
        llm_api_key="example-only",
        app_public_url="https://lab.invalid",
        cors_origins=("https://lab.invalid",),
    )
    assert any("local directory" in blocker for blocker in settings.production_blockers())
    enabled = settings.model_copy(update={"allow_filesystem_in_production": True})
    assert not enabled.production_blockers()
