"""XFS project quota live integration (E2.2).

Gated on ``E2B_XFS_QUOTA_INTEGRATION=1`` plus a real XFS mount with
``prjquota`` (default ``/var/lib/e2b-sandboxes``, override with
``E2B_XFS_TEST_MOUNT``). See docs/sandbox-disk-quota.md §4 for the OrbStack
loop-device setup; run in the privileged test container.

Note: XFS project quota returns ``ENOSPC`` (not ``EDQUOT``) when a project
exceeds its hard limit — current kernels map project-quota violations to
``-ENOSPC`` in ``xfs_trans_dqresv`` (fs/xfs/xfs_trans_dquot.c); EDQUOT is
only used for user/group quotas. The hard limit is still enforced exactly.
"""

from __future__ import annotations

import errno
import json
import os
import re
import uuid
from pathlib import Path

import httpx
import pytest

from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.xfs_quota import _local_run_xfs_quota, xfs_project_supported

XFS_MOUNT = Path(os.environ.get("E2B_XFS_TEST_MOUNT", "/var/lib/e2b-sandboxes"))

#: Rows look like ``#100  2048  0  4096  00 [--------]`` (blocks are 1 KiB).
_PROJECT_ROW = re.compile(
    r"^\s*#?(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+", re.MULTILINE
)


def _report_rows() -> dict[int, tuple[int, int, int]]:
    """Return projid -> (used_blocks, soft_blocks, hard_blocks) from report -p."""
    output = _local_run_xfs_quota(XFS_MOUNT, "report -p")
    return {
        int(match.group(1)): (
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
        )
        for match in _PROJECT_ROW.finditer(output)
    }


@pytest.fixture
def xfs_app():
    if os.environ.get("E2B_XFS_QUOTA_INTEGRATION") != "1":
        pytest.skip("XFS quota integration requires E2B_XFS_QUOTA_INTEGRATION=1")
    supported, reason = xfs_project_supported(XFS_MOUNT)
    if not supported:
        pytest.skip(
            f"workspace {XFS_MOUNT} does not support XFS project quota: {reason}"
        )
    app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=XFS_MOUNT),
        runtime_registry=RuntimeRegistry(XFS_MOUNT),
    )
    return app


async def _create_sandbox(xfs_app, disk_mb: int) -> tuple[str, object]:
    sandbox_id = f"sbx_{uuid.uuid4().hex[:12]}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=xfs_app), base_url="http://test"
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
            },
        )
    assert response.status_code == 201
    record = xfs_app.state.runtime_registry.get(sandbox_id)
    assert record is not None
    return sandbox_id, record


async def _delete_sandbox(xfs_app, sandbox_id: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=xfs_app), base_url="http://test"
    ) as client:
        response = await client.delete(
            f"/agent/sandboxes/{sandbox_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert response.status_code == 204


async def test_agent_create_provisions_project_limit_and_persists(xfs_app):
    sandbox_id, record = await _create_sandbox(xfs_app, disk_mb=8)
    try:
        projid = record.project_id
        assert isinstance(projid, int)
        rows = _report_rows()
        assert projid in rows
        used, soft, hard = rows[projid]
        assert (soft, hard) == (0, 8 * 1024)
        record_path = XFS_MOUNT / sandbox_id / "sandbox.json"
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        assert payload["project_id"] == projid

        # A new file inside the sandbox is attributed to the project.
        target = XFS_MOUNT / sandbox_id / "workspace" / "attributed.bin"
        target.write_bytes(b"\0" * 1024 * 1024)
        assert _report_rows()[projid][0] >= 1024
    finally:
        await _delete_sandbox(xfs_app, sandbox_id)


async def test_over_limit_write_enforced_with_enospc(xfs_app):
    sandbox_id, record = await _create_sandbox(xfs_app, disk_mb=4)
    try:
        projid = record.project_id
        target = XFS_MOUNT / sandbox_id / "workspace" / "overflow.bin"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            with pytest.raises(OSError) as excinfo:
                total = 0
                while total < 16 * 1024 * 1024:
                    total += os.write(fd, b"\0" * 1024 * 1024)
            assert excinfo.value.errno == errno.ENOSPC
        finally:
            os.close(fd)
        used, soft, hard = _report_rows()[projid]
        assert (soft, hard) == (0, 4 * 1024)
        # XFS stops allocation at the hard limit; the failing 1 MiB write may
        # partially complete, so usage sits within one chunk of the limit.
        assert hard - 1024 < used <= hard
    finally:
        await _delete_sandbox(xfs_app, sandbox_id)


async def test_agent_delete_clears_project_and_dir(xfs_app):
    sandbox_id, record = await _create_sandbox(xfs_app, disk_mb=8)
    projid = record.project_id
    assert projid in _report_rows()
    await _delete_sandbox(xfs_app, sandbox_id)
    # project -C clears the directory's project state and deleting the
    # directory releases the quota accounting (usage drops to 0). The quota
    # table entry itself stays as a zero-usage orphan until the E2.x orphan
    # cleanup milestone (design doc §3.3).
    assert _report_rows()[projid][0] == 0
    assert not (XFS_MOUNT / sandbox_id).exists()
