"""E3.2 contract: per-sandbox host uids + shared-volume permission model.

Covers the two worker-visible contracts:

- volume sharing across sandboxes with *distinct* host uids: the volume root
  is 1777 (world rwx + sticky, the only cross-uid sharing shape allowed by a
  single-entry userns), so sandbox B can read/write files sandbox A created;
- uid lifecycle: allocation at create, ``sandbox.json`` persistence, release
  at delete, and startup reconciliation that reclaims orphan uids (a pool
  uid owning a stale workspace with no record) without touching live ones.

Requires a root worker + sandlock (the privileged S1.2 RunAs path).

The image-rootfs (chroot) shape of the ownership assertions below is the T5
regression suite: there mediation runs in a ``sandlock-supervise`` slot whose
euid *is* the sandbox host uid (route B, ``envd_service/route_b.py``) instead
of in the root worker process, so a mediated write lands owned by the sandbox
in both shapes.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import uuid
from pathlib import Path

import httpx
import pytest

from envd_service.config import Settings as EnvdSettings
from envd_service.connect.codec import decode_envelopes, encode_message
from tests.security.conftest import sandlock_ready

POOL_START = 20000
POOL_SIZE = 16


pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not sandlock_ready(),
    reason=(
        "E3.2 uid contract tests need a root worker and sandlock "
        "(run inside the privileged Docker test runner)"
    ),
)


def _envd_settings(workspace: Path) -> EnvdSettings:
    return EnvdSettings(
        executor="sandlock",
        per_sandbox_uid=True,
        uid_pool_start=POOL_START,
        uid_pool_size=POOL_SIZE,
        workspace_base=workspace,
    )


def _headers(sandbox: dict) -> dict:
    return {
        "E2b-Sandbox-Id": sandbox["sandboxID"],
        "X-Access-Token": sandbox["envdAccessToken"],
    }


async def _run_cmd(envd, sandbox: dict, sh_cmd: str):
    request = {
        "process": {
            "cmd": "/bin/sh",
            "args": ["-c", sh_cmd],
            "envs": {},
            "cwd": "",
        },
        "stdin": False,
    }
    response = await envd.post(
        "/process.Process/Start",
        headers={
            **_headers(sandbox),
            "Content-Type": "application/connect+json",
        },
        content=encode_message(request),
    )
    assert response.status_code == 200
    return decode_envelopes(response.content)


def _result(messages) -> tuple[int, bytes, bytes]:
    stdout = b"".join(
        base64.b64decode(m["event"]["data"]["stdout"])
        for m in messages
        if "data" in m.get("event", {}) and "stdout" in m["event"]["data"]
    )
    stderr = b"".join(
        base64.b64decode(m["event"]["data"]["stderr"])
        for m in messages
        if "data" in m.get("event", {}) and "stderr" in m["event"]["data"]
    )
    ends = [m for m in messages if "end" in m.get("event", {})]
    assert len(ends) == 1
    return ends[0]["event"]["end"]["exitCode"], stdout, stderr


async def test_volume_shared_rw_across_distinct_uids(make_apps, workspace):
    control, envd = make_apps(envd_settings=_envd_settings(workspace))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        created = await client.post(
            "/volumes", headers={"X-API-Key": "local-key"}, json={"name": "shared"}
        )
        assert created.status_code == 201
        vid = created.json()["volumeID"]
        vol_path = workspace / "_volumes" / vid
        st = vol_path.stat()
        assert stat.S_IMODE(st.st_mode) == 0o1777

        mount = [{"name": vid, "path": "mnt/data"}]
        a = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300, "volumeMounts": mount},
        )
        b = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300, "volumeMounts": mount},
        )
        assert a.status_code == 201
        assert b.status_code == 201
        a_payload, b_payload = a.json(), b.json()
        assert a_payload["sandboxID"] != b_payload["sandboxID"]

    registry = envd.state.runtime_registry
    ra = registry.get(a_payload["sandboxID"])
    rb = registry.get(b_payload["sandboxID"])
    assert ra is not None and rb is not None
    assert ra.host_uid == POOL_START
    assert rb.host_uid == POOL_START + 1

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        code_a, out_a, err_a = _result(
            await _run_cmd(client, a_payload, "echo from-A > mnt/data/a.txt")
        )
        assert code_a == 0
        assert out_a == b""
        assert err_a == b""
        # Who really owns what A's sandbox wrote? Capture it now -- the unlink
        # below removes the file, and the answer is the precondition of the
        # per-uid protection being asserted.
        written_by = (vol_path / "a.txt").stat().st_uid
        code_b, out_b, err_b = _result(
            await _run_cmd(client, b_payload, "echo from-B > mnt/data/b.txt")
        )
        assert code_b == 0
        assert out_b == b""
        assert err_b == b""

        # Cross-sandbox read/write through the shared 1777 root: B reads A's
        # file and writes next to it; A reads B's file back.
        code_br, out_br, err_br = _result(
            await _run_cmd(client, b_payload, "cat mnt/data/a.txt")
        )
        assert code_br == 0
        assert out_br == b"from-A\n"
        assert err_br == b""
        code_ar, out_ar, err_ar = _result(
            await _run_cmd(client, a_payload, "cat mnt/data/b.txt")
        )
        assert code_ar == 0
        assert out_ar == b"from-B\n"
        assert err_ar == b""

        # Sticky bit: B cannot delete A's file in the shared root (the
        # unlink runs with the child's host identity, so the 1777 sticky
        # protection holds cross-uid).
        code_bd, _out_bd, err_bd = _result(
            await _run_cmd(client, b_payload, "rm mnt/data/a.txt")
        )
        # The protection is a kernel DAC decision: the sticky bit denies B the
        # unlink because A owns the file, so the storage has to have recorded
        # A's sandbox host uid as the owner. Assert that instead of assuming it
        # -- the measurement that used to surface T5 in the image-rootfs
        # shape (supervisor-tier mediation attributed the write to the worker
        # instead); route B leases a supervise slot at this sandbox's own host
        # uid for that shape, so the same assertion now holds in both shapes.
        assert written_by == ra.host_uid, (
            f"sandbox writes landed owned by uid {written_by}, not the sandbox "
            f"host uid {ra.host_uid}: per-uid isolation is not in effect"
        )
        assert code_bd != 0
        assert (
            err_bd
            == b"rm: cannot remove 'mnt/data/a.txt': Operation not permitted\n"
        )
        assert (vol_path / "a.txt").is_file()

    # Host side: workspaces are owned by their distinct allocated uids with
    # 0700 (kernel isolation), and the shared volume root keeps 1777.
    assert (vol_path).stat().st_uid == ra.host_uid
    assert stat.S_IMODE((vol_path).stat().st_mode) == 0o1777
    ws_a = Path(ra.workspace_dir)
    ws_b = Path(rb.workspace_dir)
    assert ws_a.stat().st_uid == ra.host_uid
    assert ws_b.stat().st_uid == rb.host_uid
    assert stat.S_IMODE(ws_a.stat().st_mode) == 0o700
    assert stat.S_IMODE(ws_b.stat().st_mode) == 0o700


async def test_agent_uid_lifecycle_and_orphan_reconcile(make_apps, workspace):
    control, envd = make_apps(envd_settings=_envd_settings(workspace))
    registry = envd.state.runtime_registry
    pool = registry.uid_pool
    assert pool is not None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        sbx_id = f"sbx_{uuid.uuid4().hex[:12]}"
        created = await client.post(
            "/agent/sandboxes",
            headers={"X-Internal-Key": "internal-key"},
            json={"sandboxID": sbx_id, "accessToken": "token"},
        )
        assert created.status_code == 201
        record = registry.get(sbx_id)
        assert record is not None
        assert record.host_uid == POOL_START
        ws = Path(record.workspace_dir)
        assert ws.stat().st_uid == POOL_START
        assert stat.S_IMODE(ws.stat().st_mode) == 0o700
        persisted = json.loads((ws / "sandbox.json").read_text(encoding="utf-8"))
        assert persisted["host_uid"] == POOL_START

        deleted = await client.delete(
            f"/agent/sandboxes/{sbx_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
        assert deleted.status_code == 204
        assert POOL_START not in pool.allocated_uids()

        # Orphan uid: a stale workspace (no sandbox.json) owned by a pool uid.
        stale_id = f"sbx_{uuid.uuid4().hex[:12]}"
        stale = workspace / stale_id
        stale.mkdir()
        leftover = stale / "leftover.txt"
        leftover.write_text("x", encoding="utf-8")
        os.chown(stale, POOL_START, POOL_START)
        os.chown(leftover, POOL_START, POOL_START)
        result = pool.reconcile()
        assert POOL_START in result["reclaimed"]
        assert stale.stat().st_uid == os.geteuid()
        assert leftover.stat().st_uid == os.geteuid()

        # The reclaimed uid is immediately allocatable again.
        second_id = f"sbx_{uuid.uuid4().hex[:12]}"
        second = await client.post(
            "/agent/sandboxes",
            headers={"X-Internal-Key": "internal-key"},
            json={"sandboxID": second_id, "accessToken": "token"},
        )
        assert second.status_code == 201
        second_record = registry.get(second_id)
        assert second_record is not None
        assert second_record.host_uid == POOL_START


async def test_agent_create_failure_releases_uid(make_apps, workspace):
    """I3 (agent path): a create that fails between acquire and register
    (invalid volumeMounts -> 400) must return the reserved uid to the pool
    instead of leaking a slot on every bad request."""
    control, envd = make_apps(envd_settings=_envd_settings(workspace))
    registry = envd.state.runtime_registry
    pool = registry.uid_pool
    assert pool is not None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        failed_id = f"sbx_{uuid.uuid4().hex[:12]}"
        failed = await client.post(
            "/agent/sandboxes",
            headers={"X-Internal-Key": "internal-key"},
            json={
                "sandboxID": failed_id,
                "accessToken": "token",
                # Missing hostPath/path: build_volume_mounts rejects it after
                # the uid was already reserved.
                "volumeMounts": [{"name": "vol_1"}],
            },
        )
        assert failed.status_code == 400
        assert failed.text == "volumeMounts need hostPath and path"
        # The failed create must not leak a pool slot: the next successful
        # create gets the lowest uid again.
        ok_id = f"sbx_{uuid.uuid4().hex[:12]}"
        ok = await client.post(
            "/agent/sandboxes",
            headers={"X-Internal-Key": "internal-key"},
            json={"sandboxID": ok_id, "accessToken": "token"},
        )
        assert ok.status_code == 201
        record = registry.get(ok_id)
        assert record is not None
        assert record.host_uid == POOL_START


async def test_local_create_failure_releases_uid(make_apps, workspace):
    """I3 (local control-plane path): a provisioning failure after acquire
    (mount path already exists -> 400) returns the reserved uid to the pool
    instead of leaking a slot."""
    control, envd = make_apps(envd_settings=_envd_settings(workspace))
    pool = envd.state.runtime_registry.uid_pool
    assert pool is not None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        created = await client.post(
            "/volumes",
            headers={"X-API-Key": "local-key"},
            json={"name": "shared"},
        )
        assert created.status_code == 201
        vid = created.json()["volumeID"]
        # Every fresh sandbox workspace contains a real "workspace" directory,
        # so mounting onto it fails inside build_volume_mounts after the uid
        # was allocated.
        bad = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={
                "templateID": "base",
                "timeout": 300,
                "volumeMounts": [{"name": vid, "path": "workspace"}],
            },
        )
        assert bad.status_code == 400
        assert bad.json() == {
            "code": 400,
            "message": "Mount path workspace already exists",
        }
        # The failed create must not leak a pool slot: the next successful
        # create gets the lowest uid again.
        ok = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert ok.status_code == 201
        record = envd.state.runtime_registry.get(ok.json()["sandboxID"])
        assert record is not None
        assert record.host_uid == POOL_START
