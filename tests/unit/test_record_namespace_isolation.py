"""Sandbox paths must tolerate the *other* records in the shared store.

``RedisRecordStore`` keys everything as ``e2b:record:<id>`` and the sandbox
registry and the volume registry share one namespace (both are built with
``namespace="e2b"``), so ``SandboxRegistry``'s enumeration paths see volume
records too.

Measured on the deployed stack (2026-09-12): with a single volume record
present, ``GET /sandboxes`` / ``GET /v2/sandboxes`` and the tenant-usage
endpoint returned 500 and every TTL sweep raised ``KeyError: 'template_id'``
(``manager.py::from_storage_dict``), so **expired sandboxes were never
reaped**. The store is shared by design, so the fix belongs on the read side:
filter foreign records by type and never let one record's shape take out a
listing or the sweeper.
"""

from __future__ import annotations

import asyncio
import datetime
import json

import httpx
import pytest

fakeredis = pytest.importorskip("fakeredis")

import control_plane.registry.redis_backend as redis_backend
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings
from control_plane.registry.manager import (
    SandboxRegistry,
    UnknownSandboxError,
)
from control_plane.registry.ttl import TTLSweeper
from control_plane.registry.volumes import VolumeRegistry
from envd_service.runtime.registry import RuntimeRegistry

NAMESPACE = "e2b"


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry, *, sandbox_id, tenant_id=None, timeout=300):
    return registry.create(
        template_id="base",
        sandbox_id=sandbox_id,
        timeout=timeout,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
        tenant_id=tenant_id,
    )


def _volume_registry(client, base):
    return VolumeRegistry(base, redis_client=client, namespace=NAMESPACE)


def _strip_kind(client) -> None:
    """Rewrite the stored payloads in the pre-``kind`` (legacy) shape."""
    for key in client.keys(f"{NAMESPACE}:record:*"):
        raw = client.get(key)
        if raw is None:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            payload.pop("kind", None)
            client.set(key, json.dumps(payload, separators=(",", ":")))


#: The four record-set shapes every sandbox path has to survive.
CASES = ["volume-only", "sandbox-only", "both", "legacy-payloads"]


@pytest.fixture()
def store(tmp_path):
    """A fake-Redis server shared by both registries, plus the case builder."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)

    def build(case: str):
        registry = SandboxRegistry(_settings(), redis_client=client)
        volumes = _volume_registry(client, tmp_path / "_volumes")
        sandbox_ids: list[str] = []
        volume_ids: list[str] = []
        if case in ("sandbox-only", "both", "legacy-payloads"):
            sandbox_ids.append(
                _create(registry, sandbox_id="sbx_probe", tenant_id="acme").sandbox_id
            )
        if case in ("volume-only", "both", "legacy-payloads"):
            volume_ids.append(volumes.create("probe-vol").volume_id)
        if case == "legacy-payloads":
            _strip_kind(client)
        return registry, client, sandbox_ids, volume_ids

    return build


@pytest.mark.parametrize("case", CASES)
def test_list_returns_only_sandbox_records(store, case):
    registry, _client, sandbox_ids, _volume_ids = store(case)

    listed = registry.list(limit=None)

    assert [record.sandbox_id for record in listed] == sandbox_ids


@pytest.mark.parametrize("case", CASES)
def test_tenant_usage_counts_only_sandbox_records(store, case):
    registry, _client, sandbox_ids, _volume_ids = store(case)

    usage = registry.tenant_usage()

    if sandbox_ids:
        assert usage == {
            "acme": {
                "sandboxes": 1,
                "memoryMB": 512,
                "cpuPercent": 100,
                "diskMB": 1024,
                "processes": 64,
            }
        }
    else:
        assert usage == {}


@pytest.mark.parametrize("case", CASES)
def test_remove_expired_reaps_the_expired_sandbox(store, case):
    registry, _client, sandbox_ids, _volume_ids = store(case)
    if sandbox_ids:
        record = registry.get(sandbox_ids[0])
        record.end_at = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(seconds=1)
        # The sweeper reads the shared store, so the expired deadline has to be
        # written through the same path the API uses.
        _persist_deadline(registry, record)

    expired = registry.remove_expired()  # must not raise with foreign records

    assert [r.sandbox_id for r in expired] == sandbox_ids


def _persist_deadline(registry, record) -> None:
    """Write ``record`` back to the shared store (no public API needed)."""
    store = registry._record_store
    assert store is not None, "this fixture always wires Redis"
    store.put(record.sandbox_id, record.to_storage_dict(), ttl=None)


@pytest.mark.parametrize("case", ["volume-only", "both", "legacy-payloads"])
def test_volume_ids_are_not_sandbox_ids(store, case):
    registry, _client, _sandbox_ids, volume_ids = store(case)

    with pytest.raises(UnknownSandboxError):
        registry.get(volume_ids[0])


@pytest.mark.asyncio
async def test_ttl_sweeper_reaps_while_a_volume_record_exists(store):
    registry, _client, sandbox_ids, _volume_ids = store("both")
    record = registry.get(sandbox_ids[0])
    record.end_at = datetime.datetime.now(
        datetime.timezone.utc
    ) - datetime.timedelta(seconds=1)
    _persist_deadline(registry, record)

    reaped = []
    sweeper = TTLSweeper(interval_seconds=0.05, on_expired=reaped.append)
    sweeper.start(registry)
    try:
        for _ in range(40):
            if reaped:
                break
            await asyncio.sleep(0.05)
    finally:
        await sweeper.stop()

    assert [r.sandbox_id for r in reaped] == sandbox_ids


@pytest.mark.asyncio
async def test_sandbox_list_endpoints_survive_volume_records(monkeypatch, tmp_path):
    """The user-visible half: ``/sandboxes`` must not 500 because of a volume."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    monkeypatch.setattr(
        redis_backend, "create_redis_client", lambda url: client, raising=True
    )

    workspace = tmp_path / "ws"
    runtime_registry = RuntimeRegistry(workspace)
    settings = _settings(redis_url="redis://fake:6379/0")
    app = create_control_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    app.state.volumes.create("probe-vol")  # writes a volume record
    record = _create(app.state.registry, sandbox_id="sbx_probe", tenant_id="acme")
    _persist_deadline(app.state.registry, record)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http:
        legacy = await http.get("/sandboxes", headers={"X-API-Key": "local-key"})
        v2 = await http.get("/v2/sandboxes", headers={"X-API-Key": "local-key"})

    assert legacy.status_code == 200, legacy.text
    assert [entry["sandboxID"] for entry in legacy.json()] == ["sbx_probe"]
    assert v2.status_code == 200, v2.text
    assert [entry["sandboxID"] for entry in v2.json()] == ["sbx_probe"]
