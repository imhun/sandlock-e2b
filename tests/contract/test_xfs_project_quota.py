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
import shutil
import uuid
from pathlib import Path

import httpx
import pytest

from envd_service import xfs_quota
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.xfs_quota import (
    _local_run_xfs_quota,
    reconcile_orphan_projects,
    xfs_project_supported,
)

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
    # N12: the delete finishes its own row. `project -C` clears the directory's
    # project state, removing the tree zeroes the usage, and the worker then
    # resets the limits -- XFS drops a record whose usage *and* limits are zero,
    # so there is nothing left for the next reconciliation (before this, the row
    # sat at "0 used, hard_blocks=N" until one ran; a create/delete burst left
    # 40 of them).
    assert projid not in _report_rows()
    assert not (XFS_MOUNT / sandbox_id).exists()


async def test_reconcile_removes_zero_usage_orphan_entry(xfs_app):
    """Reconciliation drops a zero-usage entry nothing else removed.

    This is the case the pass still exists for: N12 made the *delete path*
    finish its own row, so an entry like this now only appears when the tree
    disappears without that path running -- a crash between the two, or a tree
    removed by hand. Planted that way here (the directory goes behind the
    worker's back), the row is left at zero usage with non-zero limits, which is
    exactly what the reconciliation resets.
    """
    sandbox_id, record = await _create_sandbox(xfs_app, disk_mb=8)
    projid = record.project_id
    assert projid in _report_rows()
    shutil.rmtree(XFS_MOUNT / sandbox_id)
    assert _report_rows()[projid][0] == 0
    result = reconcile_orphan_projects(
        workspace_base=XFS_MOUNT,
        mount_point=XFS_MOUNT,
    )
    assert projid in result["cleaned"], f"reconcile returned {result}"
    assert result["skipped"] == []
    assert projid not in _report_rows()


async def test_reconcile_removes_record_lost_project_but_keeps_dir(xfs_app):
    """A project whose sandbox.json record vanished is cleaned without
    deleting the leftover directory (E2.4 conservative directory policy)."""
    sandbox_id, record = await _create_sandbox(xfs_app, disk_mb=8)
    projid = record.project_id
    record_path = XFS_MOUNT / sandbox_id / "sandbox.json"
    assert record_path.is_file()
    record_path.unlink()
    target = XFS_MOUNT / sandbox_id / "workspace" / "attributed.bin"
    target.write_bytes(b"\0" * 1024 * 1024)
    assert _report_rows()[projid][0] >= 1024
    result = reconcile_orphan_projects(
        workspace_base=XFS_MOUNT,
        mount_point=XFS_MOUNT,
    )
    assert projid in result["cleaned"], f"reconcile returned {result}"
    assert projid not in _report_rows()
    # Files are disowned from the orphan project but never deleted.
    assert (XFS_MOUNT / sandbox_id).is_dir()
    await _delete_sandbox(xfs_app, sandbox_id)


def test_the_quota_row_is_a_position_boundary_not_a_write_budget(monkeypatch):
    """N30: the row is a ceiling on what the tree *holds*, and it is handed back.

    Provisioning gives XFS ``bhard=<disk_mb>M`` -- a boundary on the tree's
    *current* size (存量口径), not an allowance of bytes to write -- and the
    delete path's two calls reset the state and then ``bsoft``/``bhard``, so
    the accounting is **returned** rather than kept as a high-water mark. That
    second half is the whole difference from the rejected peak reading (which
    would hold a spent budget: write once to the limit and the tree is
    permanently read-only). Both halves are the commands below, verbatim, and
    the pair on the delete side is the one ``envd_service/agent.py``'s teardown
    makes (``release_project`` before the tree goes, ``clear_project_limits``
    after it -- N12). Unlike the rest of this file the case needs no XFS: it
    fakes the two module seams
    (``_local_run_xfs_quota`` / ``_use_quotactl``) the way
    ``tests/contract/test_teardown_failure_semantics.py::_reconcile_rows``
    does, so it is the one row here that runs on the APFS dev host too.
    """
    commands: list[str] = []
    monkeypatch.setattr(xfs_quota, "_use_quotactl", lambda _mount: False)
    monkeypatch.setattr(
        xfs_quota,
        "_local_run_xfs_quota",
        lambda _mount, command: commands.append(command) or "",
    )

    projid = xfs_quota.provision_project(
        sandbox_id="sbx_n30",
        project_dir="/srv/sandboxes/sbx_n30",
        mount_point="/srv/sandboxes",
        disk_mb=64,
        project_id=1004,
    )
    assert projid == 1004
    assert commands == [
        "project -s -p /srv/sandboxes/sbx_n30 1004",
        "limit -p bhard=64M 1004",
    ]

    xfs_quota.release_project(
        project_dir="/srv/sandboxes/sbx_n30",
        mount_point="/srv/sandboxes",
        projid=1004,
    )
    xfs_quota.clear_project_limits(mount_point="/srv/sandboxes", projid=1004)
    assert commands == [
        "project -s -p /srv/sandboxes/sbx_n30 1004",
        "limit -p bhard=64M 1004",
        "project -C -p /srv/sandboxes/sbx_n30 1004",
        "limit -p bsoft=0 bhard=0 1004",
    ]
