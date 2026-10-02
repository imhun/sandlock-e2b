"""E2.5 review C1/I2: migration destroy paths must not delete shared
per-sandbox volume slices.

``keep_files`` used to double as "keep the workspace" and "keep the volume
slices".  Migration stop/rollback needs the former but never the latter:
with a shared volume root the target node re-provisions the *same* slice,
so destroying the source (or a failed target) with ``keep_files=False``
must still preserve the slice.  Only a real sandbox deletion
(``kill_sandbox`` / TTL expiry) may run ``cleanup_volume_projects``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import envd_service.agent as agent_mod
import control_plane.api.sandboxes as sandboxes
import envd_service.volumes as volumes
from control_plane.api.errors import OfficialError
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from envd_service.runtime.registry import RuntimeRegistry


def _record(sandbox_id: str = "sbx_mig") -> SimpleNamespace:
    return SimpleNamespace(sandbox_id=sandbox_id)


def _state_with_runtime(tmp_path, sandbox_id: str, *, with_slice: bool):
    """A local-node state carrying one runtime with a volume slice."""
    workspace_base = tmp_path / "ws"
    workspace_base.mkdir()
    ws_dir = workspace_base / sandbox_id
    ws_dir.mkdir()
    (ws_dir / "sandbox.json").write_text("{}", encoding="utf-8")
    volume_path = tmp_path / "vol_1"
    slice_dir = volume_path / sandbox_id
    if with_slice:
        slice_dir.mkdir(parents=True)
        (slice_dir / "payload.bin").write_bytes(b"keep-me")
    registry = RuntimeRegistry(workspace_base)
    registry.register(
        sandbox_id=sandbox_id,
        access_token="tok",
        workspace_dir=str(ws_dir),
        volume_projects=(
            [
                {
                    "volume_id": "vol_1",
                    "sandbox_id": sandbox_id,
                    "mount_path": "mnt/data",
                    "sandbox_dir": str(slice_dir),
                    "projid": 4242,
                }
            ]
            if with_slice
            else []
        ),
    )
    return SimpleNamespace(
        runtime_registry=registry,
        workspace_base=workspace_base,
        # N27: ``app.state`` carries the platform's own base beside the tree
        # base; the teardown takes the record's directory from it.
        state_base=workspace_base,
    ), ws_dir, slice_dir


def test_destroy_local_real_delete_cleans_volume_slices(tmp_path, monkeypatch):
    """A real sandbox deletion still runs volume slice cleanup (regression)."""
    state, ws_dir, slice_dir = _state_with_runtime(
        tmp_path, "sbx_mig", with_slice=True
    )
    calls = []
    monkeypatch.setattr(volumes, "release_project", lambda **kw: calls.append(kw))
    # A slice's project id is read from the disk now (review W7 / C2: the
    # record inside the sandbox-owned tree is input the sandbox can rewrite).
    # This host has no XFS to answer it, so the one read is supplied here; the
    # real parser and the real wiring still run.
    monkeypatch.setattr(
        agent_mod,
        "directory_project_id",
        lambda path: 4242 if Path(path) == slice_dir else None,
    )
    sandboxes._destroy_local(state, _record())
    assert len(calls) == 1
    assert calls[0]["projid"] == 4242
    assert not slice_dir.exists()
    assert not ws_dir.exists()


def test_destroy_local_keep_volume_slices_preserves_shared_slices(
    tmp_path, monkeypatch
):
    """Migration destroy removes the workspace but keeps the shared slice."""
    state, ws_dir, slice_dir = _state_with_runtime(
        tmp_path, "sbx_mig", with_slice=True
    )
    calls = []
    monkeypatch.setattr(
        volumes, "cleanup_volume_projects", lambda **kw: calls.append(kw)
    )
    sandboxes._destroy_local(
        state, _record(), keep_files=False, keep_volume_slices=True
    )
    assert calls == []
    # The target sandbox still uses this slice: data must survive.
    assert slice_dir.is_dir()
    assert (slice_dir / "payload.bin").read_bytes() == b"keep-me"
    # The migrated workspace itself is released.
    assert not ws_dir.exists()


def test_destroy_remote_forwards_keep_volume_slices(tmp_path, monkeypatch):
    """The remote agent receives keepVolumeSlices=true on migration cleanup."""
    sent = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def delete(self, url, headers):
            sent["url"] = url
            sent["headers"] = headers
            # The real agent answers 204 on an acknowledged teardown; the
            # caller now reads that answer (review W7 / C1-1).
            return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(internal_api_key="internal-key")
            )
        )
    )

    import asyncio

    asyncio.run(
        sandboxes._destroy_remote(
            request,
            _record(),
            SimpleNamespace(address="http://127.0.0.1:9"),
            keep_files=False,
            keep_volume_slices=True,
        )
    )
    assert sent["url"].endswith(
        "/agent/sandboxes/sbx_mig?keepVolumeSlices=true"
    )
    assert sent["headers"] == {"X-Internal-Key": "internal-key"}

    # A real deletion sends no keepVolumeSlices parameter.
    asyncio.run(
        sandboxes._destroy_remote(
            request,
            _record(),
            SimpleNamespace(address="http://127.0.0.1:9"),
        )
    )
    assert sent["url"].endswith("/agent/sandboxes/sbx_mig")


async def _create_quota_volume_and_sandbox(client) -> tuple[str, str]:
    created = await client.post(
        "/volumes",
        headers={"X-API-Key": "local-key"},
        json={"name": "mig-vol", "perSandboxQuotaMb": 512},
    )
    assert created.status_code == 201
    volume_id = created.json()["volumeID"]
    assert created.json()["perSandboxQuotaMb"] == 512
    sandbox = await client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={
            "templateID": "base",
            "volumeMounts": [{"name": volume_id, "path": "mnt/data"}],
        },
    )
    assert sandbox.status_code == 201
    return volume_id, sandbox.json()["sandboxID"]


def _control_app_with_shared_volume(workspace):
    """Control app whose volume root lives under the shared volume root."""
    return create_control_app(
        settings=ControlSettings(
            api_keys=("local-key",), shared_volume_root=workspace
        ),
        runtime_registry=RuntimeRegistry(workspace),
        workspace_base=workspace,
    )


async def test_migrate_success_preserves_volume_slices(
    workspace, monkeypatch, tmp_path
):
    """C1: successful migration destroys the source with keep_volume_slices."""
    control = _control_app_with_shared_volume(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        _volume_id, sandbox_id = await _create_quota_volume_and_sandbox(client)
        # The remote node appears only after the sandbox exists, so creation
        # stays on the (healthy) local node; migration then moves it away.
        control.state.nodes.register(
            node_id="node_remote",
            address="http://127.0.0.1:9",
            total_memory_mb=4096,
            total_cpu_percent=400,
            total_disk_mb=8192,
            total_processes=256,
        )
        tar_path = tmp_path / "export.tar.gz"
        tar_path.write_bytes(b"")

        async def _stop(request, record, node):
            return True

        async def _export(request, record, node):
            return tar_path

        async def _import(request, record, node, tar_path):
            return None

        async def _provision(request, record, node, settings, snapshot,
                             volume_mounts, snapshot_id=None):
            return None

        destroyed = []

        async def _destroy(request, record, node, keep_files=False,
                           keep_volume_slices=False):
            destroyed.append((node.node_id, keep_files, keep_volume_slices))
            # The real hop's answer; Task 3's source release reads it.
            return sandboxes._TeardownOutcome(acknowledged=True)

        monkeypatch.setattr(sandboxes, "_stop_source_runtime", _stop)
        monkeypatch.setattr(sandboxes, "_export_sandbox_archive", _export)
        monkeypatch.setattr(sandboxes, "_import_sandbox_archive", _import)
        monkeypatch.setattr(sandboxes, "_provision_remote", _provision)
        monkeypatch.setattr(sandboxes, "_destroy_on_node", _destroy)

        migrated = await client.post(
            f"/sandboxes/{sandbox_id}/migrate",
            headers={"X-API-Key": "local-key"},
            json={},
        )
        assert migrated.status_code == 200
        assert migrated.json()["nodeID"] == "node_remote"

    # Non-shared workspace: keep_files=False, but volume slices survive.
    assert destroyed == [("local", False, True)]


async def test_migrate_rollback_preserves_volume_slices(
    workspace, monkeypatch, tmp_path
):
    """C1: rollback destroys the target with keep_volume_slices."""
    control = _control_app_with_shared_volume(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        _volume_id, sandbox_id = await _create_quota_volume_and_sandbox(client)
        control.state.nodes.register(
            node_id="node_remote",
            address="http://127.0.0.1:9",
            total_memory_mb=4096,
            total_cpu_percent=400,
            total_disk_mb=8192,
            total_processes=256,
        )
        tar_path = tmp_path / "export.tar.gz"
        tar_path.write_bytes(b"")

        async def _stop(request, record, node):
            return True

        async def _export(request, record, node):
            return tar_path

        async def _import(request, record, node, tar_path):
            return None

        async def _provision_fail(request, record, node, settings, snapshot,
                                  volume_mounts, snapshot_id=None):
            raise OfficialError(502, "target provision failed")

        destroyed = []

        async def _destroy(request, record, node, keep_files=False,
                           keep_volume_slices=False):
            destroyed.append((node.node_id, keep_files, keep_volume_slices))

        monkeypatch.setattr(sandboxes, "_stop_source_runtime", _stop)
        monkeypatch.setattr(sandboxes, "_export_sandbox_archive", _export)
        monkeypatch.setattr(sandboxes, "_import_sandbox_archive", _import)
        monkeypatch.setattr(sandboxes, "_provision_remote", _provision_fail)
        monkeypatch.setattr(sandboxes, "_destroy_remote", _destroy)

        failed = await client.post(
            f"/sandboxes/{sandbox_id}/migrate",
            headers={"X-API-Key": "local-key"},
            json={},
        )
        assert failed.status_code == 502

    # The partial target is destroyed without touching the shared slice.
    assert destroyed == [("node_remote", False, True)]
