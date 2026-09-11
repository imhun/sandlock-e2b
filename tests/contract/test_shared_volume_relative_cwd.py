"""Shared volume views must resolve from BOTH workspace aliases.

Regression for backlog #25: a cwd-derived relative open (`cat mnt/data/x`)
bypassed the /workspace/<rel> sub-mount, so chroot sandboxes saw EACCES (or
ENOENT) for every relative volume path once the bind workaround was removed.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from tests.contract.test_uid_permissions import (
    _envd_settings,
    _result,
    _run_cmd,
)


@pytest.mark.asyncio
async def test_volume_visible_from_both_workspace_aliases(make_apps, workspace):
    control, envd = make_apps(envd_settings=_envd_settings(workspace))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        vol = await client.post(
            "/volumes",
            headers={"X-API-Key": "local-key"},
            json={"name": f"alias-{uuid.uuid4().hex[:8]}"},
        )
        assert vol.status_code == 201
        vid = vol.json()["volumeID"]
        sbx = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={
                "templateID": "base",
                "timeout": 300,
                "volumeMounts": [{"name": vid, "path": "mnt/data"}],
            },
        )
        assert sbx.status_code == 201
        payload = sbx.json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        code, stdout, stderr = _result(
            await _run_cmd(
                client,
                payload,
                "echo hello > mnt/data/a.txt && cat /workspace/mnt/data/a.txt "
                "&& cd /home/user && cat mnt/data/a.txt && cd /workspace "
                "&& cat ./mnt/data/a.txt",
            )
        )
        assert code == 0
        assert stdout == b"hello\nhello\nhello\n"
        assert stderr == b""

        # Decision ① (2026-09-10): the chroot shape's canonical workspace
        # alias is `/home/user`, so a command that inherits the sandbox cwd
        # reports `/home/user` -- the alias the pre-A2 reverse lookup handed
        # out by accident, now pinned by the declaration order. The sandbox
        # above is reused on purpose: one sandbox per test file keeps the
        # worker's per-uid route-B slot ledger free of cross-test leases.
        code, stdout, stderr = _result(
            await _run_cmd(client, payload, "pwd && pwd -P")
        )
        assert (code, stdout, stderr) == (
            0,
            b"/home/user\n/home/user\n",
            b"",
        )
    # Release the sandbox, its pooled host uid and its route-B slot: the
    # worker's slot fleet is process-global, so a sandbox left alive here
    # would collide with the next file's uid-20000 sandbox in one pytest run.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        killed = await client.delete(
            f"/sandboxes/{payload['sandboxID']}",
            headers={"X-API-Key": "local-key"},
        )
        assert killed.status_code == 204


def test_runtime_context_registers_both_volume_aliases(monkeypatch, tmp_path):
    """The `Produces` contract: every volume view is registered under BOTH
    workspace aliases, so a relative open resolves against whichever alias
    the sandbox's cwd names without relying on the mount-alias walk."""
    from envd_service.config import Settings as EnvdSettings
    from envd_service.runtime import context as context_mod
    from envd_service.runtime.context import SandboxRuntimeContext
    from envd_service.runtime.registry import RuntimeSandbox

    captured: dict = {}
    real = context_mod.create_executor

    def _spy(settings, **kwargs):
        captured.update(kwargs)
        return real(settings, **kwargs)

    monkeypatch.setattr(context_mod, "create_executor", _spy)
    ws = tmp_path / "ws"
    ws.mkdir()
    vol = tmp_path / "vol"
    vol.mkdir()
    record = RuntimeSandbox(
        sandbox_id="sbx_alias",
        access_token="at",
        workspace_dir=str(ws),
        base_image="python-mcp:3.14",
        volume_mounts=[{"path": "mnt/data", "hostPath": str(vol)}],
    )
    SandboxRuntimeContext(record, EnvdSettings(executor="local"))
    assert captured["fs_mounts"] == {
        "/workspace/mnt/data": str(vol),
        "/home/user/mnt/data": str(vol),
    }
    # The Landlock second layer keeps the volume slice writable (the chroot
    # mount view is what exposes it inside the sandbox).
    assert captured["extra_fs_writable"] == [str(vol)]
