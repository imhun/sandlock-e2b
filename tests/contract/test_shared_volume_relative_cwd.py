"""Shared volume views must resolve from BOTH workspace aliases.

Regression for backlog #25: a cwd-derived relative open (`cat mnt/data/x`)
bypassed the /workspace/<rel> sub-mount, so chroot sandboxes saw EACCES (or
ENOENT) for every relative volume path once the bind workaround was removed.

Shape scope: both aliases are *rooted* virtual paths -- resolved by the mount
table (``/home/user`` and ``/workspace`` are both bound to the workspace), which
is why a command that inherits the sandbox cwd reports ``/home/user``. Two
shapes have such a root: the image's rootfs, and the pure shape's synthesized
skeleton (N16, ``E2B_PURE_ROOTFS=synth``). The end-to-end test below is
therefore gated on *a rooted shape* (``_ROOTED_SHAPE_ONLY``), the mirror image
of ``tests/contract/test_pure_shape_workspace_ownership.py``'s
``_NO_BASE_IMAGE``; the alias key set itself is asserted shape-independently by
``test_runtime_context_registers_both_volume_aliases`` here and by
``tests/unit/test_policy_mapping.py::test_volume_views_map_under_both_workspace_aliases``.

The old gate was ``E2B_BASE_IMAGE`` alone, and its skip reason said "the pure
shape runs with the host workspace cwd and has no /home/user alias". That
premise expired with N15 (which put the workspace under both aliases in *both*
shapes -- the pure one through the same mediator with the host root as its
root) and is wrong twice over since N16 gave the pure shape a root of its own.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

from tests.contract.test_uid_permissions import (
    _control_settings,
    _envd_settings,
    _result,
    _run_cmd,
)
from tests.security.conftest import sandlock_ready

#: The shape gate below says *when the aliases exist*; this one says whether we
#: can build the sandbox that has them. Both are needed: since 2026-09-27 the
#: pure shape is rooted by default, so the shape condition is satisfied on a
#: machine with no sandlock wheel at all -- the test then died inside
#: ``create_executor`` ("the sandlock package is not installed") instead of
#: skipping, which is the same environment gate its sibling contracts
#: (``test_pure_shape_workspace_ownership``, ``test_own_identity_executor``) carry.
pytestmark = pytest.mark.skipif(
    not sandlock_ready(),
    reason=(
        "the workspace-alias contract needs Linux + sandlock "
        "(run inside the Docker test runner)"
    ),
)

# Mirrors tests/contract/test_pure_shape_workspace_ownership.py::_NO_BASE_IMAGE
# (marker object + decorator), inverted and widened: this contract needs a shape
# with a *root* to be confined to, which is what makes the two aliases
# sandbox-visible paths. The two legal pure shapes are set by the 2026-09-26
# ruling (docs/superpowers/plans/2026-09-26-decisions.md): the identity root
# (no root at all -- out of scope here) and the synthesized root, which is the
# *default* since 2026-09-27.
_ROOTED_SHAPE_ONLY = pytest.mark.skipif(
    not (
        os.environ.get("E2B_BASE_IMAGE")
        or (os.environ.get("E2B_PURE_ROOTFS") or "synth").strip().lower() == "synth"
    ),
    reason=(
        "the workspace aliases are resolved by the mount table, which needs a "
        "sandbox root: set E2B_BASE_IMAGE (image shape) or "
        "E2B_PURE_ROOTFS=synth (pure shape)"
    ),
)

#: This file's own uid range, so its sandboxes can never collide with the
#: ownership contracts' 20000/21000 slots in a shared container.
ALIAS_POOL_START = 22000
#: Same size as the other contract files' pools (16): the control plane hands
#: the uid out and the worker checks it against this same range.
ALIAS_POOL_SIZE = 16


@pytest.mark.asyncio
@_ROOTED_SHAPE_ONLY
async def test_volume_visible_from_both_workspace_aliases(make_apps, workspace):
    # This file gets its **own** uid pool. Route-B leases one live slot per
    # uid, and the suite's other ownership contracts run on 20000/21000: a
    # second sandbox on a uid whose sandbox from another file is still alive
    # cannot lease a slot at all, and the create then fails inside the
    # sandbox with exit 127 and this only visible on stderr --
    # "route-B uid 20000 already has a live slot (sandbox …); W1 recycles a
    # uid only by restarting its process, never by sharing it"
    # (measured 2026-09-21). Per-file ranges are the suite's existing
    # convention for exactly this reason.
    control, envd = make_apps(
        control_settings=_control_settings(
            uid_pool_start=ALIAS_POOL_START, uid_pool_size=ALIAS_POOL_SIZE
        ),
        envd_settings=_envd_settings(workspace, uid_pool_start=ALIAS_POOL_START),
    )
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

        # Decision ① (2026-09-10): a rooted shape's canonical workspace alias is
        # `/home/user`, so a command that inherits the sandbox cwd reports
        # `/home/user` -- the alias the pre-A2 reverse lookup handed out by
        # accident, now pinned by the declaration order. The assertion holds in
        # both rooted shapes (image rootfs and synthesized pure root, measured
        # 2026-09-26). The rootless identity shape has no `/home/user` to report
        # at all -- it answers "can't cd to /home/user" and its `pwd` is the
        # host workspace path (same probe) -- which is why it is skipped above.
        # The sandbox above is reused on purpose: one sandbox per test file
        # keeps the worker's per-uid route-B slot ledger free of cross-test
        # leases.
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
