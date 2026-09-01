"""Agent sandbox create/delete wired to XFS project quota (E2.2)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

import envd_service.agent as agent
import envd_service.xfs_quota as xfs_quota
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.xfs_quota import ProjectQuotaError


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
    record_path = workspace / "sbx_quota1" / "sandbox.json"
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
    assert [r.message for r in caplog.records] == [
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
    assert [r.message for r in caplog.records] == [
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
        (workspace / "sbx_reprov_fail" / "sandbox.json").read_text(
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
    assert [r.message for r in caplog.records] == [
        "XFS project quota setup failed for sbx_reprov_fail: "
        "quota limit setup failed for sbx_reprov_fail: xfs_quota "
        "'limit -p bhard=1024M 777' failed: limit boom",
    ]


async def test_delete_releases_project_and_removes_dir(workspace, monkeypatch):
    app = _make_app(workspace)
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

    response = await _delete_sandbox(app, "sbx_del")
    assert response.status_code == 204
    assert calls == {
        "project_dir": workspace / "sbx_del",
        "mount_point": workspace,
        "projid": 42,
        "via_agent": False,
    }
    assert not (workspace / "sbx_del").exists()


async def test_delete_release_uses_record_workspace_dir(workspace, monkeypatch):
    """Delete derives project_dir and removal target from the record, not the
    workspace_base/id convention (E2.2 review Minor)."""
    app = _make_app(workspace)
    recorded_dir = workspace / "custom" / "sbx_relocated"
    recorded_dir.mkdir(parents=True)
    app.state.runtime_registry.register(
        sandbox_id="sbx_relocated",
        access_token="tok",
        workspace_dir=str(recorded_dir),
        disk_mb=1024,
        project_id=42,
    )
    calls: dict = {}

    def fake_release(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(agent, "release_project", fake_release)

    response = await _delete_sandbox(app, "sbx_relocated")
    assert response.status_code == 204
    assert calls == {
        "project_dir": recorded_dir,
        "mount_point": workspace,
        "projid": 42,
        "via_agent": False,
    }
    assert not recorded_dir.exists()


async def test_delete_keep_files_skips_release_and_keeps_dir(workspace, monkeypatch):
    app = _make_app(workspace)
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
    assert (workspace / "sbx_keep" / "sandbox.json").is_file()


async def test_delete_cleanup_failure_degrades_with_warning(workspace, monkeypatch, caplog):
    app = _make_app(workspace)
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

    monkeypatch.setattr(agent, "release_project", fake_release)
    caplog.set_level(logging.WARNING)

    response = await _delete_sandbox(app, "sbx_cleanup_fail")
    assert response.status_code == 204
    assert not (workspace / "sbx_cleanup_fail").exists()
    assert [r.message for r in caplog.records] == [
        "XFS project quota cleanup failed for sbx_cleanup_fail: "
        "xfs_quota 'project -C -p /srv/sandboxes/sbx_cleanup_fail 42' failed: boom",
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
