"""Agent sandbox create/delete wired to XFS project quota (E2.2)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from gateway_common.paths import sandbox_record_path

import envd_service.agent as agent
import envd_service.xfs_quota as xfs_quota
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.xfs_quota import ProjectQuotaError
from tests._disk_projids import install_disk_projids as _install_disk_projids
from tests._disk_projids import read_calls
from tests.conftest import uid_startup_disclosure


def _warnings(caplog) -> list[str]:
    """Warnings from the two loggers these contracts are about.

    ``caplog.records`` is whatever the whole process emitted, and a worker has
    background loggers that are not part of any of these assertions -- the
    quota-maintenance disk watermark is the one that bites: on a workspace volume
    past its threshold it lands a WARNING in the middle of an exact-list check
    (measured: 98% used turned one of the tests below red with nothing but a
    quota-log line added at index 0). The message lists below stay exact for the
    loggers under test.
    """
    return [
        record.message
        for record in caplog.records
        if record.name
        in {
            "envd_service.agent",
            "envd_service.app",
            "envd_service.xfs_quota",  # "quota unavailable: filesystem is X"
        }
    ]


def _make_app(workspace: Path, **settings_overrides):
    overrides = dict(executor="local", workspace_base=workspace)
    overrides.update(settings_overrides)
    return create_envd_app(
        settings=EnvdSettings(**overrides),
        runtime_registry=RuntimeRegistry(workspace),
    )


def _payload(sandbox_id: str, **overrides) -> dict:
    payload = {
        "sandboxID": sandbox_id,
        "accessToken": "tok",
        "envVars": {},
        "baseImage": None,
        "memoryMB": 512,
        "cpuPercent": 100,
        "diskMB": 1024,
        "maxProcesses": 64,
        "allowInternetAccess": False,
        "maxCommandTimeout": 3600,
    }
    payload.update(overrides)
    return payload


async def _post_sandbox(app, sandbox_id: str, **overrides):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/agent/sandboxes",
            headers={"X-Internal-Key": "internal-key"},
            json=_payload(sandbox_id, **overrides),
        )


async def _delete_sandbox(app, sandbox_id: str, keep_files: bool = False):
    url = f"/agent/sandboxes/{sandbox_id}"
    if keep_files:
        url += "?keepFiles=true"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.delete(url, headers={"X-Internal-Key": "internal-key"})


async def test_create_provisions_quota_and_persists_project_id(workspace, monkeypatch):
    calls: dict = {}

    def fake_provision(**kwargs):
        calls.update(kwargs)
        return 42

    monkeypatch.setattr(
        agent, "xfs_project_supported", lambda _mp, via_agent=False: (True, "")
    )
    monkeypatch.setattr(agent, "provision_project", fake_provision)
    app = _make_app(workspace)

    response = await _post_sandbox(app, "sbx_quota1", diskMB=2048)
    assert response.status_code == 201
    assert calls == {
        "sandbox_id": "sbx_quota1",
        "project_dir": workspace / "sbx_quota1",
        "mount_point": workspace,
        "disk_mb": 2048,
        "via_agent": False,
        "project_id": None,
    }
    record = app.state.runtime_registry.get("sbx_quota1")
    assert record is not None
    assert record.project_id == 42
    record_path = sandbox_record_path(workspace, "sbx_quota1")
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    assert payload["project_id"] == 42
    assert (workspace / "sbx_quota1" / "workspace").is_dir()


async def test_create_quota_failure_degrades_with_warning(workspace, monkeypatch, caplog):
    def boom(**kwargs):
        raise ProjectQuotaError("xfs_quota 'limit -p bhard=1024M 42' failed: boom")

    monkeypatch.setattr(
        agent, "xfs_project_supported", lambda _mp, via_agent=False: (True, "")
    )
    monkeypatch.setattr(agent, "provision_project", boom)
    caplog.set_level(logging.WARNING)
    app = _make_app(workspace)

    response = await _post_sandbox(app, "sbx_quota_fail")
    assert response.status_code == 201
    record = app.state.runtime_registry.get("sbx_quota_fail")
    assert record is not None
    assert record.project_id is None
    assert _warnings(caplog) == [
        *uid_startup_disclosure(),
        "XFS project quota setup failed for sbx_quota_fail: "
        "xfs_quota 'limit -p bhard=1024M 42' failed: boom",
    ]


async def test_create_unsupported_skips_quota_with_warning(workspace, monkeypatch, caplog):
    def fake_detect(_mount_point, via_agent=False):
        quota_logger = logging.getLogger("envd_service.xfs_quota")
        quota_logger.warning(
            "XFS project quota unavailable for %s: filesystem is ext4, not xfs",
            str(_mount_point),
        )
        return (False, "filesystem is ext4, not xfs")

    calls: list[str] = []

    def fake_provision(**kwargs):
        calls.append(kwargs["sandbox_id"])

    monkeypatch.setattr(agent, "xfs_project_supported", fake_detect)
    monkeypatch.setattr(agent, "provision_project", fake_provision)
    caplog.set_level(logging.WARNING)
    app = _make_app(workspace)

    response = await _post_sandbox(app, "sbx_noquota")
    assert response.status_code == 201
    assert calls == []
    record = app.state.runtime_registry.get("sbx_noquota")
    assert record is not None
    assert record.project_id is None
    assert _warnings(caplog) == [
        *uid_startup_disclosure(),
        f"XFS project quota unavailable for {workspace}: filesystem is ext4, not xfs",
    ]


async def test_create_reprovision_reuses_existing_project_id(workspace, monkeypatch):
    app = _make_app(workspace)
    app.state.runtime_registry.register(
        sandbox_id="sbx_again",
        access_token="tok",
        workspace_dir=str(workspace / "sbx_again"),
        disk_mb=1024,
        project_id=7,
    )
    calls: dict = {}

    def fake_provision(**kwargs):
        calls.update(kwargs)
        return 42

    monkeypatch.setattr(
        agent, "xfs_project_supported", lambda _mp, via_agent=False: (True, "")
    )
    monkeypatch.setattr(agent, "provision_project", fake_provision)

    response = await _post_sandbox(app, "sbx_again")
    assert response.status_code == 201
    assert calls["project_id"] == 7
    assert app.state.runtime_registry.get("sbx_again").project_id == 42


async def test_create_reprovision_failure_persists_none_after_cleanup(
    workspace, monkeypatch, caplog
):
    """Re-provision failure after cleanup must not persist the stale projid:
    limit fails, project -C succeeds, sandbox.json records project_id=None
    (E2.2 review Important: otherwise quota silently stops applying)."""
    app = _make_app(workspace)
    app.state.runtime_registry.register(
        sandbox_id="sbx_reprov_fail",
        access_token="tok",
        workspace_dir=str(workspace / "sbx_reprov_fail"),
        disk_mb=1024,
        project_id=777,
    )

    class _FakeProc:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        command = args[args.index("-c") + 1]
        if command.startswith("project -s -p"):
            return _FakeProc(0)
        if command.startswith("limit -p"):
            return _FakeProc(1, "", "limit boom")
        if command.startswith("project -C -p"):
            return _FakeProc(0)
        raise AssertionError(f"unexpected xfs_quota command: {command}")

    monkeypatch.setattr(
        agent, "xfs_project_supported", lambda _mp, via_agent=False: (True, "")
    )
    monkeypatch.setattr(xfs_quota.subprocess, "run", fake_run)
    caplog.set_level(logging.WARNING)

    response = await _post_sandbox(app, "sbx_reprov_fail")
    assert response.status_code == 201
    record = app.state.runtime_registry.get("sbx_reprov_fail")
    assert record is not None
    assert record.project_id is None
    payload = json.loads(
        sandbox_record_path(workspace, "sbx_reprov_fail").read_text(
            encoding="utf-8"
        )
    )
    assert payload["project_id"] is None
    assert calls == [
        [
            "xfs_quota",
            "-x",
            "-c",
            f"project -s -p {workspace}/sbx_reprov_fail 777",
            str(workspace),
        ],
        ["xfs_quota", "-x", "-c", "limit -p bhard=1024M 777", str(workspace)],
        [
            "xfs_quota",
            "-x",
            "-c",
            f"project -C -p {workspace}/sbx_reprov_fail 777",
            str(workspace),
        ],
    ]
    assert _warnings(caplog) == [
        *uid_startup_disclosure(),
        "XFS project quota setup failed for sbx_reprov_fail: "
        "quota limit setup failed for sbx_reprov_fail: xfs_quota "
        "'limit -p bhard=1024M 777' failed: limit boom",
    ]


async def test_delete_releases_project_and_removes_dir(
    workspace, monkeypatch, disk_read_backend
):
    app = _make_app(workspace)
    # The tree is the worker's own doing (``_agent_create_sandbox`` makes it);
    # ``register`` writes only the platform's record, which lives beside the
    # tree since the platform/workspace split.
    (workspace / "sbx_del").mkdir(parents=True, exist_ok=True)
    app.state.runtime_registry.register(
        sandbox_id="sbx_del",
        access_token="tok",
        workspace_dir=str(workspace / "sbx_del"),
        disk_mb=1024,
        project_id=42,
    )
    calls: dict = {}

    def fake_release(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(agent, "release_project", fake_release)
    # The release target is the disk's answer, not the record's claim.
    disk = _install_disk_projids(
        monkeypatch,
        {workspace / "sbx_del": 42},
        backend=disk_read_backend,
    )

    response = await _delete_sandbox(app, "sbx_del")
    assert response.status_code == 204
    assert calls == {
        "project_dir": workspace / "sbx_del",
        "mount_point": workspace,
        "projid": 42,
        "via_agent": False,
    }
    assert disk.calls == read_calls(disk_read_backend, workspace / "sbx_del")
    assert not (workspace / "sbx_del").exists()


async def test_delete_refuses_a_record_pointing_at_another_tree(
    workspace, monkeypatch, caplog
):
    """A rewritten record may not aim the teardown at another tenant (W1).

    ``sandbox.json`` is written inside the directory the sandbox owns, so the
    sandbox can unlink and recreate it: with ``workspace_dir`` pointing at
    somebody else's tree, the delete used to release that tenant's project id
    and rmtree their workspace. The record is now only ever *checked* against
    the tree the registry read it from, and a contradiction refuses the
    teardown with a reason instead of acting on it. (The end-to-end shape,
    victim tree + victim quota row, is pinned by
    ``tests/contract/test_delete_trusted_targets.py``.)
    """
    app = _make_app(workspace)
    victim_dir = workspace / "custom" / "sbx_victim"
    victim_dir.mkdir(parents=True)
    (victim_dir / "payload.bin").write_bytes(b"another tenant's data")
    # The liar's own tree exists too (the worker creates it); the refusal is
    # about the record pointing at the *victim* instead.
    (workspace / "sbx_relocated").mkdir(parents=True, exist_ok=True)
    app.state.runtime_registry.register(
        sandbox_id="sbx_relocated",
        access_token="tok",
        workspace_dir=str(victim_dir),
        disk_mb=1024,
        project_id=42,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        agent, "release_project", lambda **kw: calls.append("release")
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete_sandbox(app, "sbx_relocated")
    assert response.status_code == 409
    assert response.text == (
        "refusing to tear down sbx_relocated: its sandbox.json points at "
        f"{victim_dir}"
    )
    assert calls == []
    # Everything the record tried to aim the teardown at is untouched, and the
    # sandbox's own tree is left standing too (nothing was torn down at all).
    assert (victim_dir / "payload.bin").read_bytes() == b"another tenant's data"
    assert Path(app.state.runtime_registry.workspace_base, "sbx_relocated").is_dir()
    assert _warnings(caplog) == [
        "delete: refusing to tear down sbx_relocated: its sandbox.json points "
        f"at {victim_dir}"
    ]


async def test_delete_keep_files_skips_release_and_keeps_dir(workspace, monkeypatch):
    app = _make_app(workspace)
    (workspace / "sbx_keep").mkdir(parents=True, exist_ok=True)
    app.state.runtime_registry.register(
        sandbox_id="sbx_keep",
        access_token="tok",
        workspace_dir=str(workspace / "sbx_keep"),
        disk_mb=1024,
        project_id=42,
    )
    calls: list[str] = []

    def fake_release(**kwargs):
        calls.append("release")

    monkeypatch.setattr(agent, "release_project", fake_release)

    response = await _delete_sandbox(app, "sbx_keep", keep_files=True)
    assert response.status_code == 204
    assert calls == []
    assert (workspace / "sbx_keep").is_dir()
    assert sandbox_record_path(workspace, "sbx_keep").is_file()


async def test_delete_cleanup_failure_degrades_with_warning(
    workspace, monkeypatch, caplog, disk_read_backend
):
    app = _make_app(workspace)
    (workspace / "sbx_cleanup_fail").mkdir(parents=True, exist_ok=True)
    app.state.runtime_registry.register(
        sandbox_id="sbx_cleanup_fail",
        access_token="tok",
        workspace_dir=str(workspace / "sbx_cleanup_fail"),
        disk_mb=1024,
        project_id=42,
    )

    def fake_release(**kwargs):
        raise ProjectQuotaError(
            "xfs_quota 'project -C -p /srv/sandboxes/sbx_cleanup_fail 42' failed: boom"
        )

    def fake_clear_limits(**kwargs):
        # N12 added a second best-effort step to the delete path (reset the
        # limits once the tree is gone). Both degrades are faked here so the
        # expectation stays deterministic: going through the real function would
        # assert on this platform's xfs_quota error text.
        raise ProjectQuotaError(
            "xfs_quota 'limit -p bsoft=0 bhard=0 42' failed: boom"
        )

    monkeypatch.setattr(agent, "release_project", fake_release)
    monkeypatch.setattr(agent, "clear_project_limits", fake_clear_limits)
    _install_disk_projids(
        monkeypatch,
        {workspace / "sbx_cleanup_fail": 42},
        backend=disk_read_backend,
    )
    caplog.set_level(logging.WARNING)

    response = await _delete_sandbox(app, "sbx_cleanup_fail")
    assert response.status_code == 204
    assert not (workspace / "sbx_cleanup_fail").exists()
    assert _warnings(caplog) == [
        *uid_startup_disclosure(),
        "XFS project quota cleanup failed for sbx_cleanup_fail: "
        "xfs_quota 'project -C -p /srv/sandboxes/sbx_cleanup_fail 42' failed: boom",
        # The second step fails independently and is reported the same way: the
        # delete is not blocked by either, and the row is left to the reconcile.
        "XFS project row cleanup failed for sbx_cleanup_fail: "
        "xfs_quota 'limit -p bsoft=0 bhard=0 42' failed: boom",
    ]


async def test_delete_without_record_skips_release(workspace, monkeypatch):
    app = _make_app(workspace)
    calls: list[str] = []
    monkeypatch.setattr(agent, "release_project", lambda **kw: calls.append("release"))

    response = await _delete_sandbox(app, "sbx_never_existed")
    assert response.status_code == 204
    assert calls == []


async def test_create_via_agent_flag_forwards_to_agent_ops(workspace, monkeypatch):
    calls: dict = {}

    def fake_provision(**kwargs):
        calls.update(kwargs)
        return 99

    monkeypatch.setattr(
        agent, "xfs_project_supported", lambda _mp, via_agent=False: (True, "")
    )
    monkeypatch.setattr(agent, "provision_project", fake_provision)
    app = _make_app(workspace, quota_via_agent=True)

    response = await _post_sandbox(app, "sbx_agent")
    assert response.status_code == 201
    assert calls["via_agent"] is True


def test_agent_form_never_shells_out_to_a_local_quota_tool(monkeypatch):
    """A6: the agent form is the worker's only quota implementation.

    The deployed worker no longer holds CAP_SYS_ADMIN, so every agent-form
    entry point must dispatch over the agent hooks and never spawn a local
    quota command (``xfs_quota``/``xfs_info``/``lsattr``). ``subprocess.run``
    is replaced with a landmine so any local exec fails loudly.
    """
    calls: list[tuple[str, dict]] = []

    def hook(name: str):
        def call(**kwargs):
            calls.append((name, kwargs))
            if name == "provision":
                return 7
            return {"projects": {}} if name == "report" else {}

        return call

    monkeypatch.setattr(
        xfs_quota,
        "agent_ops",
        {
            "provision": hook("provision"),
            "release": hook("release"),
            "report": hook("report"),
            "reconcile": hook("reconcile"),
        },
    )
    monkeypatch.setattr(
        xfs_quota,
        "agent_query",
        lambda _mount: {
            "fs_type": "xfs",
            "projid32bit": True,
            "prjquota": True,
            "xfs_quota": True,
        },
    )

    def landmine(*args, **kwargs):
        raise AssertionError(f"a local quota tool was executed: {args!r}")

    monkeypatch.setattr(xfs_quota.subprocess, "run", landmine)

    assert xfs_quota.xfs_project_supported("/mnt/nfs", via_agent=True) == (True, "")
    assert xfs_quota.project_quota_table("/mnt/nfs", via_agent=True) == {}
    assert (
        xfs_quota.provision_project(
            sandbox_id="sbx_agent_only",
            project_dir="/mnt/nfs/sbx_agent_only",
            mount_point="/mnt/nfs",
            disk_mb=512,
            via_agent=True,
        )
        == 7
    )
    assert (
        xfs_quota.release_project(
            project_dir="/mnt/nfs/sbx_agent_only",
            mount_point="/mnt/nfs",
            projid=7,
            via_agent=True,
        )
        is None
    )
    assert (
        xfs_quota.reconcile_orphan_projects(
            workspace_base="/mnt/nfs",
            mount_point="/mnt/nfs",
            via_agent=True,
        )
        == {}
    )
    assert calls == [
        ("report", {"mount_point": "/mnt/nfs"}),
        ("provision", {
            "sandbox_id": "sbx_agent_only",
            "project_dir": "/mnt/nfs/sbx_agent_only",
            "mount_point": "/mnt/nfs",
            "disk_mb": 512,
            "project_id": None,
        }),
        ("release", {
            "project_dir": "/mnt/nfs/sbx_agent_only",
            "mount_point": "/mnt/nfs",
            "projid": 7,
        }),
        ("reconcile", {
            "workspace_base": "/mnt/nfs",
            "mount_point": "/mnt/nfs",
        }),
    ]
