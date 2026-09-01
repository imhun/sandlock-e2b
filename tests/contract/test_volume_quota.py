"""E2.5: per-sandbox volume quota contract (mount views + lifecycle).

Two shapes:

- XFS project quota (``E2B_XFS_QUOTA_INTEGRATION=1`` + prjquota mount at
  ``E2B_XFS_TEST_MOUNT``): every sandbox mounting a quota-limited volume gets
  its own ``volume/<volume_id>/<sandbox_id>/`` slice with an independent
  project + hard limit. A hitting its limit (ENOSPC, project-quota semantics)
  must not affect B, and deleting A cleans A's slice + project while B and
  the volume root survive.
- Non-XFS degradation: the same create flow mounts the volume root directly
  (pre-E2.5 behavior) with a warning, so sandboxes keep working.

The XFS shape talks to the worker agent directly (like the E2.2 integration
test), because that is the code path that provisions the per-sandbox
projects; the degradation shape exercises the control-plane local create.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import uuid
from pathlib import Path

import httpx
import pytest

from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.xfs_quota import (
    _local_run_xfs_quota,
    xfs_project_supported,
)

XFS_MOUNT = Path(os.environ.get("E2B_XFS_TEST_MOUNT", "/var/lib/e2b-sandboxes"))

_PROJECT_ROW = re.compile(
    r"^\s*#?(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+", re.MULTILINE
)


def _report_rows() -> dict[int, tuple[int, int, int]]:
    output = _local_run_xfs_quota(XFS_MOUNT, "report -p")
    return {
        int(match.group(1)): (
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
        )
        for match in _PROJECT_ROW.finditer(output)
    }


def _xfs_ready() -> bool:
    if os.environ.get("E2B_XFS_QUOTA_INTEGRATION") != "1":
        return False
    supported, _reason = xfs_project_supported(XFS_MOUNT)
    return supported


@pytest.fixture
def xfs_envd_app():
    if not _xfs_ready():
        pytest.skip(
            "XFS quota integration requires E2B_XFS_QUOTA_INTEGRATION=1 "
            f"and prjquota support on {XFS_MOUNT}"
        )
    return create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=XFS_MOUNT),
        runtime_registry=RuntimeRegistry(XFS_MOUNT),
    )


@pytest.fixture
def xfs_two_envd_apps():
    """Two worker agents with distinct workspace bases on the same XFS mount.

    Non-shared workspace (each worker owns its sandbox directory) + a shared
    volume root: the exact deployment shape the E2.5 migration review C1
    covers — the target re-provisions the *same* volume slice.
    """
    if not _xfs_ready():
        pytest.skip(
            "XFS quota integration requires E2B_XFS_QUOTA_INTEGRATION=1 "
            f"and prjquota support on {XFS_MOUNT}"
        )
    src_base = XFS_MOUNT / f"src_{uuid.uuid4().hex[:8]}"
    dst_base = XFS_MOUNT / f"dst_{uuid.uuid4().hex[:8]}"
    src_base.mkdir(parents=True)
    dst_base.mkdir(parents=True)
    src_app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=src_base),
        runtime_registry=RuntimeRegistry(src_base),
    )
    dst_app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=dst_base),
        runtime_registry=RuntimeRegistry(dst_base),
    )
    try:
        yield src_app, dst_app, src_base, dst_base
    finally:
        shutil.rmtree(src_base, ignore_errors=True)
        shutil.rmtree(dst_base, ignore_errors=True)


async def _agent_create_sandbox(
    xfs_envd_app, *, sandbox_id: str, disk_mb: int, volume_mounts: list[dict]
) -> object:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=xfs_envd_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agent/sandboxes",
            headers={"X-Internal-Key": "internal-key"},
            json={
                "sandboxID": sandbox_id,
                "accessToken": "tok",
                "envVars": {},
                "baseImage": None,
                "memoryMB": 512,
                "cpuPercent": 100,
                "diskMB": disk_mb,
                "maxProcesses": 64,
                "allowInternetAccess": False,
                "maxCommandTimeout": 3600,
                "volumeMounts": volume_mounts,
            },
        )
    assert response.status_code == 201
    record = xfs_envd_app.state.runtime_registry.get(sandbox_id)
    assert record is not None
    return record


async def _agent_delete_sandbox(
    xfs_envd_app, sandbox_id: str, params: str = ""
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=xfs_envd_app), base_url="http://test"
    ) as client:
        response = await client.delete(
            f"/agent/sandboxes/{sandbox_id}{params}",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert response.status_code == 204


async def _write_until_enospc(fd, *, total_mb: int) -> None:
    written = 0
    while written < total_mb * 1024 * 1024:
        written += os.write(fd, b"\0" * 1024 * 1024)


async def test_multi_sandbox_same_volume_quota_is_independent(xfs_envd_app):
    """A hitting its per-sandbox limit never affects B (XFS project quota)."""
    volume_id = f"vol_{uuid.uuid4().hex[:12]}"
    volume_root = XFS_MOUNT / volume_id
    volume_root.mkdir(parents=True, exist_ok=True)
    mount = {
        "name": volume_id,
        "path": "mnt/data",
        "hostPath": str(volume_root),
        "perSandboxQuotaMb": 4,
    }
    sandbox_a = f"sbx_{uuid.uuid4().hex[:12]}"
    sandbox_b = f"sbx_{uuid.uuid4().hex[:12]}"
    record_a = await _agent_create_sandbox(
        xfs_envd_app, sandbox_id=sandbox_a, disk_mb=32, volume_mounts=[mount]
    )
    record_b = await _agent_create_sandbox(
        xfs_envd_app, sandbox_id=sandbox_b, disk_mb=32, volume_mounts=[mount]
    )
    slice_a = volume_root / sandbox_a
    slice_b = volume_root / sandbox_b
    try:
        # Each sandbox sees only its own slice, and each slice has its own
        # independent project id + limit.
        assert [p["projid"] for p in record_a.volume_projects] != [
            p["projid"] for p in record_b.volume_projects
        ]
        assert record_a.volume_mounts[0]["hostPath"] == str(slice_a)
        assert record_b.volume_mounts[0]["hostPath"] == str(slice_b)
        assert slice_a.is_dir() and slice_b.is_dir()
        rows = _report_rows()
        assert (rows[record_a.volume_projects[0]["projid"]][1], rows[record_a.volume_projects[0]["projid"]][2]) == (0, 4 * 1024)
        assert (rows[record_b.volume_projects[0]["projid"]][1], rows[record_b.volume_projects[0]["projid"]][2]) == (0, 4 * 1024)

        # A writes through its mount view until the 4 MiB hard limit.
        view_a = XFS_MOUNT / sandbox_a / "mnt" / "data"
        assert view_a.resolve() == slice_a.resolve()
        fd_a = os.open(
            view_a / "overflow.bin", os.O_WRONLY | os.O_CREAT, 0o644
        )
        try:
            with pytest.raises(OSError) as excinfo:
                await _write_until_enospc(fd_a, total_mb=16)
            assert excinfo.value.errno == errno.ENOSPC
        finally:
            os.close(fd_a)

        # B is completely unaffected: its own 4 MiB fits within B's limit.
        view_b = XFS_MOUNT / sandbox_b / "mnt" / "data"
        assert view_b.resolve() == slice_b.resolve()
        fd_b = os.open(
            view_b / "payload.bin", os.O_WRONLY | os.O_CREAT, 0o644
        )
        try:
            await _write_until_enospc(fd_b, total_mb=4)
        finally:
            os.close(fd_b)

        rows = _report_rows()
        projid_a = record_a.volume_projects[0]["projid"]
        projid_b = record_b.volume_projects[0]["projid"]
        used_a, _, hard_a = rows[projid_a]
        used_b, _, hard_b = rows[projid_b]
        assert hard_a == 4 * 1024 and hard_b == 4 * 1024
        # A stopped at the hard limit (within one 1 MiB chunk of it).
        assert hard_a - 1024 < used_a <= hard_a
        # B's usage is its own budget, untouched by A's overflow.
        assert used_b >= 4 * 1024
    finally:
        await _agent_delete_sandbox(xfs_envd_app, sandbox_a)
        await _agent_delete_sandbox(xfs_envd_app, sandbox_b)
        volume_root.rmdir() if not list(volume_root.iterdir()) else None


async def test_delete_sandbox_cleans_only_its_own_slice(xfs_envd_app):
    volume_id = f"vol_{uuid.uuid4().hex[:12]}"
    volume_root = XFS_MOUNT / volume_id
    volume_root.mkdir(parents=True, exist_ok=True)
    mount = {
        "name": volume_id,
        "path": "mnt/data",
        "hostPath": str(volume_root),
        "perSandboxQuotaMb": 8,
    }
    sandbox_a = f"sbx_{uuid.uuid4().hex[:12]}"
    sandbox_b = f"sbx_{uuid.uuid4().hex[:12]}"
    record_a = await _agent_create_sandbox(
        xfs_envd_app, sandbox_id=sandbox_a, disk_mb=32, volume_mounts=[mount]
    )
    record_b = await _agent_create_sandbox(
        xfs_envd_app, sandbox_id=sandbox_b, disk_mb=32, volume_mounts=[mount]
    )
    projid_a = record_a.volume_projects[0]["projid"]
    projid_b = record_b.volume_projects[0]["projid"]
    slice_a = volume_root / sandbox_a
    slice_b = volume_root / sandbox_b
    (slice_a / "a.bin").write_bytes(b"\0" * 1024 * 1024)
    (slice_b / "b.bin").write_bytes(b"\0" * 1024 * 1024)
    try:
        await _agent_delete_sandbox(xfs_envd_app, sandbox_a)
        # A's slice and project accounting are gone; B's slice and usage
        # survive; the volume root is untouched (reference counting).
        assert not slice_a.exists()
        assert slice_b.is_dir()
        rows = _report_rows()
        assert rows[projid_a][0] == 0
        assert rows[projid_b][0] >= 1024
        assert volume_root.is_dir()
    finally:
        await _agent_delete_sandbox(xfs_envd_app, sandbox_b)
        volume_root.rmdir() if not list(volume_root.iterdir()) else None


async def test_quota_unsupported_degrades_to_volume_root(make_apps, workspace):
    """Non-XFS workers keep the pre-E2.5 shared-root mount (backward compat)."""
    if _xfs_ready():
        pytest.skip("XFS supported: degradation path not exercised")
    control, envd = make_apps()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        created = await client.post(
            "/volumes",
            headers={"X-API-Key": "local-key"},
            json={"name": "data", "perSandboxQuotaMb": 512},
        )
        assert created.status_code == 201
        assert created.json()["perSandboxQuotaMb"] == 512
        vid = created.json()["volumeID"]
        token = created.json()["token"]
        await client.put(
            f"/volumecontent/{vid}/file",
            headers={"Authorization": f"Bearer {token}"},
            params={"path": "shared.txt"},
            content=b"shared",
        )
        first = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={
                "templateID": "base",
                "volumeMounts": [{"name": vid, "path": "mnt/data"}],
            },
        )
        assert first.status_code == 201
        first_id = first.json()["sandboxID"]
        second = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={
                "templateID": "base",
                "volumeMounts": [{"name": vid, "path": "mnt/data"}],
            },
        )
        assert second.status_code == 201
        second_id = second.json()["sandboxID"]
        volume_root = workspace / "_volumes" / vid
        # No per-sandbox slice: both sandboxes mount the volume root and see
        # the same shared files (quota degraded, sandbox still works).
        assert not (volume_root / first_id).exists()
        assert not (volume_root / second_id).exists()
        assert (
            workspace / first_id / "mnt" / "data" / "shared.txt"
        ).is_symlink() or (workspace / first_id / "mnt" / "data" / "shared.txt").exists()
        assert (
            workspace / second_id / "mnt" / "data" / "shared.txt"
        ).is_symlink() or (workspace / second_id / "mnt" / "data" / "shared.txt").exists()
        for sandbox_id in (first_id, second_id):
            deleted = await client.delete(
                f"/sandboxes/{sandbox_id}",
                headers={"X-API-Key": "local-key"},
            )
            assert deleted.status_code == 204
        assert volume_root.is_dir()


def _quota_mount(volume_root: Path) -> dict:
    return {
        "name": volume_root.name,
        "path": "mnt/data",
        "hostPath": str(volume_root),
        "perSandboxQuotaMb": 8,
    }


async def test_migration_shared_volume_slice_survives_source_destroy(
    xfs_two_envd_apps,
):
    """C1+I2: cross-node migration keeps the shared slice and its data.

    Source and target are separate workers (distinct workspace bases) that
    share the same volume root on the XFS mount. Migration success destroys
    the source runtime/workspace with ``keepVolumeSlices=true``: the shared
    slice the target re-provisioned must survive with its data, and the
    target gets a fresh projid while the source's original projid drops to
    zero usage (E2.4 reconciles the orphan).
    """
    src_app, dst_app, src_base, dst_base = xfs_two_envd_apps
    volume_id = f"vol_{uuid.uuid4().hex[:12]}"
    volume_root = XFS_MOUNT / volume_id
    volume_root.mkdir(parents=True, exist_ok=True)
    mount = _quota_mount(volume_root)
    sandbox_id = f"sbx_{uuid.uuid4().hex[:12]}"
    try:
        src_record = await _agent_create_sandbox(
            src_app, sandbox_id=sandbox_id, disk_mb=32, volume_mounts=[mount]
        )
        projid_src = src_record.volume_projects[0]["projid"]
        slice_dir = volume_root / sandbox_id
        assert src_record.volume_mounts[0]["hostPath"] == str(slice_dir)
        (slice_dir / "payload.bin").write_bytes(b"migration-data")

        # Migration stop: source runtime released, workspace + slice kept
        # so the workspace can still be exported.
        await _agent_delete_sandbox(
            src_app, sandbox_id, params="?keepFiles=true"
        )
        assert (slice_dir / "payload.bin").read_bytes() == b"migration-data"

        # Target re-provision: same shared slice, fresh projid (the original
        # projid is still in the project table, so a new one is allocated).
        dst_record = await _agent_create_sandbox(
            dst_app, sandbox_id=sandbox_id, disk_mb=32, volume_mounts=[mount]
        )
        projid_dst = dst_record.volume_projects[0]["projid"]
        assert dst_record.volume_mounts[0]["hostPath"] == str(slice_dir)
        assert projid_dst != projid_src
        rows = _report_rows()
        assert (rows[projid_dst][1], rows[projid_dst][2]) == (0, 8 * 1024)

        # Migration success cleanup: the source workspace is gone but the
        # shared slice survives (C1 — the target still serves it).
        await _agent_delete_sandbox(
            src_app, sandbox_id, params="?keepVolumeSlices=true"
        )
        assert not (src_base / sandbox_id).exists()
        assert (slice_dir / "payload.bin").read_bytes() == b"migration-data"

        # I2: the target's new projid owns the slice usage; the original
        # source projid is a zero-usage orphan for E2.4 reconciliation.
        rows = _report_rows()
        assert rows[projid_dst][0] >= 1
        assert rows[projid_src][0] == 0
    finally:
        # Real sandbox deletion (on the target) is what finally frees the
        # slice; the source workspace was already released above.
        await _agent_delete_sandbox(dst_app, sandbox_id)
        if volume_root.exists() and not list(volume_root.iterdir()):
            volume_root.rmdir()


async def test_migration_rollback_keeps_shared_slice(xfs_two_envd_apps):
    """C1 rollback: a failed target provision never deletes the shared slice.

    The partial target runtime is destroyed with ``keepVolumeSlices=true``,
    the slice (with the original data) survives, and re-provisioning the
    source restores the mount view.
    """
    src_app, dst_app, src_base, dst_base = xfs_two_envd_apps
    volume_id = f"vol_{uuid.uuid4().hex[:12]}"
    volume_root = XFS_MOUNT / volume_id
    volume_root.mkdir(parents=True, exist_ok=True)
    mount = _quota_mount(volume_root)
    sandbox_id = f"sbx_{uuid.uuid4().hex[:12]}"
    try:
        src_record = await _agent_create_sandbox(
            src_app, sandbox_id=sandbox_id, disk_mb=32, volume_mounts=[mount]
        )
        slice_dir = volume_root / sandbox_id
        assert src_record.volume_mounts[0]["hostPath"] == str(slice_dir)
        (slice_dir / "payload.bin").write_bytes(b"rollback-data")

        await _agent_delete_sandbox(
            src_app, sandbox_id, params="?keepFiles=true"
        )
        dst_record = await _agent_create_sandbox(
            dst_app, sandbox_id=sandbox_id, disk_mb=32, volume_mounts=[mount]
        )
        assert dst_record.volume_projects[0]["projid"] != src_record.volume_projects[0]["projid"]

        # Rollback: destroy the partial target without touching the slice.
        await _agent_delete_sandbox(
            dst_app, sandbox_id, params="?keepVolumeSlices=true"
        )
        assert not (dst_base / sandbox_id).exists()
        assert (slice_dir / "payload.bin").read_bytes() == b"rollback-data"

        # Re-provision on the source: data is still there.
        restored = await _agent_create_sandbox(
            src_app, sandbox_id=sandbox_id, disk_mb=32, volume_mounts=[mount]
        )
        assert restored.volume_mounts[0]["hostPath"] == str(slice_dir)
        assert (slice_dir / "payload.bin").read_bytes() == b"rollback-data"
    finally:
        await _agent_delete_sandbox(src_app, sandbox_id)
        if volume_root.exists() and not list(volume_root.iterdir()):
            volume_root.rmdir()


async def test_snapshot_fork_quota_gets_fresh_slice(xfs_envd_app):
    """I1: forking a quota-limited volume yields a fresh, empty slice.

    Snapshots capture the workspace only; volume contents stay inside the
    parent sandbox's per-sandbox slice. A fork is a new sandbox with a new
    id, so it provisions its own empty slice (per-sandbox quota isolation)
    and never inherits the parent's slice or projid.
    """
    volume_id = f"vol_{uuid.uuid4().hex[:12]}"
    volume_root = XFS_MOUNT / volume_id
    volume_root.mkdir(parents=True, exist_ok=True)
    mount = _quota_mount(volume_root)
    parent_id = f"sbx_{uuid.uuid4().hex[:12]}"
    fork_id = f"sbx_{uuid.uuid4().hex[:12]}"
    try:
        parent = await _agent_create_sandbox(
            xfs_envd_app, sandbox_id=parent_id, disk_mb=32, volume_mounts=[mount]
        )
        parent_slice = volume_root / parent_id
        (parent_slice / "parent.bin").write_bytes(b"parent-data")

        # The control-plane fork path re-provisions with the fork's own
        # sandbox id and empty existing_volume_projects.
        fork = await _agent_create_sandbox(
            xfs_envd_app, sandbox_id=fork_id, disk_mb=32, volume_mounts=[mount]
        )
        fork_slice = volume_root / fork_id
        assert fork.volume_mounts[0]["hostPath"] == str(fork_slice)
        assert (
            fork.volume_projects[0]["projid"]
            != parent.volume_projects[0]["projid"]
        )
        assert fork_slice.is_dir()
        assert list(fork_slice.iterdir()) == []
        # The parent's slice and data are untouched by the fork.
        assert (parent_slice / "parent.bin").read_bytes() == b"parent-data"
    finally:
        await _agent_delete_sandbox(xfs_envd_app, parent_id)
        await _agent_delete_sandbox(xfs_envd_app, fork_id)
        if volume_root.exists() and not list(volume_root.iterdir()):
            volume_root.rmdir()
