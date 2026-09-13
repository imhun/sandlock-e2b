"""E2.5: per-sandbox volume quota provisioning, views and cleanup (mocked)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import envd_service.volumes as volumes
import envd_service.xfs_quota as xfs_quota
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeSandbox
from envd_service.volumes import (
    build_volume_mounts,
    cleanup_volume_projects,
    provision_sandbox_volume_mount,
    volume_projid_key,
)


def _sandbox_dir(volume_path: Path, sandbox_id: str) -> Path:
    return volume_path / sandbox_id


def _supported(_mount, *, via_agent=False):
    return (True, "")


def _unsupported(_mount, *, via_agent=False):
    return (False, "not xfs")


def _install_disk_projids(monkeypatch, mapping: dict[Path, int]) -> list[list[str]]:
    """Answer the project-id read from an explicit disk table (review W1).

    The delete endpoint releases the project id the *disk* reports for the
    tree, never the one the sandbox-writable ``sandbox.json`` claims, so the
    one read this host cannot do itself is supplied here -- through
    ``lsattr -p -d``'s stdout, so the real parser still runs.
    """
    import subprocess

    calls: list[list[str]] = []
    real_run = subprocess.run
    monkeypatch.setattr(xfs_quota, "_use_quotactl_read", lambda mount_point: False)

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "lsattr":
            calls.append(list(argv))
            projid = mapping.get(Path(argv[-1]))
            stdout = (
                ""
                if projid is None
                else f"{projid:>8} ---------------- {argv[-1]}\n"
            )
            return subprocess.CompletedProcess(list(argv), 0, stdout, "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(xfs_quota.subprocess, "run", fake_run)
    return calls


# ------------------------------------------------------------- projid seed


def test_volume_projid_key_stable_and_distinct():
    first = volume_projid_key("sbx_a", "vol_1", "mnt/data")
    assert volume_projid_key("sbx_a", "vol_1", "mnt/data") == first
    assert volume_projid_key("sbx_b", "vol_1", "mnt/data") != first
    assert volume_projid_key("sbx_a", "vol_2", "mnt/data") != first
    assert volume_projid_key("sbx_a", "vol_1", "mnt/other") != first
    # Volume seeds never collide with the workspace seed (plain sandbox id).
    assert first != "sbx_a"


# -------------------------------------------------- provision (quota path)


def test_provision_quota_zero_returns_volume_root_without_side_effects(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(volumes, "provision_project", lambda **kw: calls.append(kw))
    view, projid = provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_1",
        mount_path="mnt/data",
        volume_path=tmp_path / "vol_1",
        per_sandbox_quota_mb=0,
        fallback_mount_point="/srv",
        via_agent=False,
    )
    assert view == tmp_path / "vol_1"
    assert projid is None
    assert calls == []
    assert not (tmp_path / "vol_1").exists()


def test_provision_quota_creates_subdir_and_provision_project(
    monkeypatch, tmp_path
):
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    seen = {}

    def fake_provision(**kwargs):
        seen.update(kwargs)
        return 4242

    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)
    monkeypatch.setattr(volumes, "provision_project", fake_provision)
    monkeypatch.setattr(volumes, "containing_mount_point", lambda _path: None)
    view, projid = provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_1",
        mount_path="mnt/data",
        volume_path=volume_path,
        per_sandbox_quota_mb=512,
        fallback_mount_point="/srv",
        via_agent=True,
    )
    assert view == _sandbox_dir(volume_path, "sbx_a")
    assert view.is_dir()
    assert projid == 4242
    assert seen == {
        "sandbox_id": volume_projid_key("sbx_a", "vol_1", "mnt/data"),
        "project_dir": view,
        "mount_point": Path("/srv"),
        "disk_mb": 512,
        "via_agent": True,
        "project_id": None,
    }


def test_provision_reuses_existing_projid(monkeypatch, tmp_path):
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    seen = {}

    def fake_provision(**kwargs):
        seen.update(kwargs)
        return kwargs["project_id"]

    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)
    monkeypatch.setattr(volumes, "provision_project", fake_provision)
    monkeypatch.setattr(volumes, "containing_mount_point", lambda _path: None)
    view, projid = provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_1",
        mount_path="mnt/data",
        volume_path=volume_path,
        per_sandbox_quota_mb=512,
        fallback_mount_point="/srv",
        via_agent=False,
        existing_projid=777,
    )
    assert view.is_dir()
    assert projid == 777
    assert seen["project_id"] == 777


def test_provision_unsupported_degrades_to_volume_root(monkeypatch, tmp_path):
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    calls = []
    monkeypatch.setattr(volumes, "xfs_project_supported", _unsupported)
    monkeypatch.setattr(volumes, "provision_project", lambda **kw: calls.append(kw))
    view, projid = provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_1",
        mount_path="mnt/data",
        volume_path=volume_path,
        per_sandbox_quota_mb=512,
        fallback_mount_point="/srv",
        via_agent=False,
    )
    assert view == volume_path
    assert projid is None
    assert calls == []
    # No subdirectory is created when quota cannot be enforced.
    assert not _sandbox_dir(volume_path, "sbx_a").exists()


def test_provision_failure_degrades_and_removes_empty_subdir(
    monkeypatch, tmp_path, caplog
):
    import logging

    from envd_service.xfs_quota import ProjectQuotaError

    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()

    def boom(**kwargs):
        raise ProjectQuotaError("limit boom")

    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)
    monkeypatch.setattr(volumes, "provision_project", boom)
    caplog.set_level(logging.WARNING)
    view, projid = provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_1",
        mount_path="mnt/data",
        volume_path=volume_path,
        per_sandbox_quota_mb=512,
        fallback_mount_point="/srv",
        via_agent=False,
    )
    assert view == volume_path
    assert projid is None
    assert not _sandbox_dir(volume_path, "sbx_a").exists()
    assert any(
        "volume quota setup failed for sbx_a" in r.message
        for r in caplog.records
    )


# ------------------------------------------- E3.2 host-uid permission model


def _chownable(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("E3.2 ownership assertions require root (Docker runner)")


def test_provision_with_host_uid_sets_root_1777_and_slice_owner(
    monkeypatch, tmp_path
):
    _chownable(tmp_path)
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)
    monkeypatch.setattr(volumes, "provision_project", lambda **kw: 4242)
    monkeypatch.setattr(volumes, "containing_mount_point", lambda _path: None)
    view, projid = provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_1",
        mount_path="mnt/data",
        volume_path=volume_path,
        per_sandbox_quota_mb=512,
        fallback_mount_point="/srv",
        via_agent=False,
        host_uid=10000,
    )
    assert projid == 4242
    assert view == volume_path / "sbx_a"
    # Volume root: shared 1777 (world rwx + sticky), owner = creator uid.
    root_st = volume_path.stat()
    assert stat.S_IMODE(root_st.st_mode) == 0o1777
    assert root_st.st_uid == 10000
    # Slice: owned by the mounting sandbox uid, group = the worker's gid,
    # `0770` (fix round 1 / c1).
    slice_st = view.stat()
    assert slice_st.st_uid == 10000
    assert stat.S_IMODE(slice_st.st_mode) == 0o770


def test_provision_quota_zero_with_host_uid_makes_root_1777(
    monkeypatch, tmp_path
):
    _chownable(tmp_path)
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    calls = []
    monkeypatch.setattr(
        volumes, "provision_project", lambda **kw: calls.append(kw)
    )
    view, projid = provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_1",
        mount_path="mnt/data",
        volume_path=volume_path,
        per_sandbox_quota_mb=0,
        fallback_mount_point="/srv",
        via_agent=False,
        host_uid=10000,
    )
    assert view == volume_path
    assert projid is None
    assert calls == []
    st = volume_path.stat()
    assert stat.S_IMODE(st.st_mode) == 0o1777
    assert st.st_uid == 10000


def test_provision_without_host_uid_keeps_legacy_permissions(
    monkeypatch, tmp_path
):
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)
    monkeypatch.setattr(volumes, "provision_project", lambda **kw: 4242)
    view, projid = provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_1",
        mount_path="mnt/data",
        volume_path=volume_path,
        per_sandbox_quota_mb=512,
        fallback_mount_point="/srv",
        via_agent=False,
    )
    assert projid == 4242
    root_st = volume_path.stat()
    assert stat.S_IMODE(root_st.st_mode) == 0o755
    assert root_st.st_uid != 10000


def test_build_volume_mounts_passes_host_uid_to_slice(
    monkeypatch, tmp_path
):
    _chownable(tmp_path)
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)
    monkeypatch.setattr(volumes, "provision_project", lambda **kw: 4242)
    workspace = tmp_path / "sbx_a"
    workspace.mkdir()
    mount_paths, volume_projects = build_volume_mounts(
        sandbox_id="sbx_a",
        volume_mounts=[_mount_input(volume_path, quota=512)],
        shared_volume_root=tmp_path,
        workspace_dir=workspace,
        fallback_mount_point="/srv",
        via_agent=False,
        host_uid=10000,
    )
    slice_dir = volume_path / "sbx_a"
    assert mount_paths == [
        {"path": "mnt/data", "hostPath": str(slice_dir)}
    ]
    assert slice_dir.stat().st_uid == 10000
    assert stat.S_IMODE(slice_dir.stat().st_mode) == 0o770
    assert volume_path.stat().st_uid == 10000
    assert stat.S_IMODE(volume_path.stat().st_mode) == 0o1777


# ------------------------------------------------------ mount view builder


def _mount_input(volume_path: Path, *, quota: int, path: str = "mnt/data"):
    return {
        "name": volume_path.name,
        "path": path,
        "hostPath": str(volume_path),
        "perSandboxQuotaMb": quota,
    }


def test_build_volume_mounts_quota_zero_keeps_volume_root_view(
    monkeypatch, tmp_path
):
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    calls = []
    monkeypatch.setattr(volumes, "provision_project", lambda **kw: calls.append(kw))
    workspace = tmp_path / "sbx_a"
    workspace.mkdir()
    mount_paths, volume_projects = build_volume_mounts(
        sandbox_id="sbx_a",
        volume_mounts=[_mount_input(volume_path, quota=0)],
        shared_volume_root=tmp_path,
        workspace_dir=workspace,
        fallback_mount_point="/srv",
        via_agent=False,
    )
    assert mount_paths == [{"path": "mnt/data", "hostPath": str(volume_path)}]
    assert volume_projects == []
    assert calls == []
    target = workspace / "mnt" / "data"
    assert target.is_symlink()
    assert target.resolve() == volume_path.resolve()


def test_build_volume_mounts_quota_views_subdir_and_records_project(
    monkeypatch, tmp_path
):
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)
    monkeypatch.setattr(volumes, "provision_project", lambda **kw: 4242)
    workspace = tmp_path / "sbx_a"
    workspace.mkdir()
    mount_paths, volume_projects = build_volume_mounts(
        sandbox_id="sbx_a",
        volume_mounts=[_mount_input(volume_path, quota=512)],
        shared_volume_root=tmp_path,
        workspace_dir=workspace,
        fallback_mount_point="/srv",
        via_agent=False,
    )
    view = _sandbox_dir(volume_path, "sbx_a")
    assert view.is_dir()
    assert mount_paths == [{"path": "mnt/data", "hostPath": str(view)}]
    assert volume_projects == [
        {
            "volume_id": "vol_1",
            "sandbox_id": "sbx_a",
            "mount_path": "mnt/data",
            "sandbox_dir": str(view),
            "projid": 4242,
        }
    ]
    target = workspace / "mnt" / "data"
    assert target.is_symlink()
    assert target.resolve() == view.resolve()


def test_build_volume_mounts_reuses_persisted_projid(monkeypatch, tmp_path):
    volume_path = tmp_path / "vol_1"
    volume_path.mkdir()
    seen = {}
    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)

    def fake_provision(**kwargs):
        seen["project_id"] = kwargs["project_id"]
        return kwargs["project_id"]

    monkeypatch.setattr(volumes, "provision_project", fake_provision)
    workspace = tmp_path / "sbx_a"
    workspace.mkdir()
    build_volume_mounts(
        sandbox_id="sbx_a",
        volume_mounts=[_mount_input(volume_path, quota=512)],
        shared_volume_root=tmp_path,
        workspace_dir=workspace,
        fallback_mount_point="/srv",
        via_agent=False,
        existing_volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_a",
                "mount_path": "mnt/data",
                "sandbox_dir": str(_sandbox_dir(volume_path, "sbx_a")),
                "projid": 777,
            }
        ],
    )
    assert seen["project_id"] == 777


@pytest.mark.parametrize(
    "mount",
    [
        "oops",
        {"name": "vol_1", "path": "mnt/data"},
        {"name": "vol_1", "hostPath": "/vol"},
        {"name": "vol_1", "path": "mnt/data", "hostPath": "/vol", "perSandboxQuotaMb": -1},
        {"name": "vol_1", "path": "mnt/data", "hostPath": "/vol", "perSandboxQuotaMb": "8"},
    ],
)
def test_build_volume_mounts_rejects_invalid_config(monkeypatch, tmp_path, mount):
    workspace = tmp_path / "sbx_a"
    workspace.mkdir()
    with pytest.raises(ValueError):
        build_volume_mounts(
            sandbox_id="sbx_a",
            volume_mounts=[mount],
            shared_volume_root=tmp_path,
            workspace_dir=workspace,
            fallback_mount_point="/srv",
            via_agent=False,
        )


def test_build_volume_mounts_rejects_host_outside_shared_root(tmp_path):
    workspace = tmp_path / "sbx_a"
    workspace.mkdir()
    outside = tmp_path.parent / "elsewhere"
    with pytest.raises(ValueError, match="outside the shared volume root"):
        build_volume_mounts(
            sandbox_id="sbx_a",
            volume_mounts=[_mount_input(outside, quota=0)],
            shared_volume_root=tmp_path,
            workspace_dir=workspace,
            fallback_mount_point="/srv",
            via_agent=False,
        )


# ------------------------------------------------------------- lifecycle


def test_cleanup_volume_projects_releases_and_removes_only_own_slice(
    monkeypatch, tmp_path
):
    volume_path = tmp_path / "vol_1"
    (volume_path / "sbx_a").mkdir(parents=True)
    (volume_path / "sbx_b").mkdir()
    (volume_path / "shared.txt").write_text("keep me")
    released = []
    monkeypatch.setattr(volumes, "release_project", lambda **kw: released.append(kw))
    monkeypatch.setattr(volumes, "containing_mount_point", lambda _path: None)
    cleanup_volume_projects(
        volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_a",
                "mount_path": "mnt/data",
                "sandbox_dir": str(volume_path / "sbx_a"),
                "projid": 4242,
            }
        ],
        fallback_mount_point="/srv",
        via_agent=False,
    )
    assert released == [
        {
            "project_dir": volume_path / "sbx_a",
            "mount_point": Path("/srv"),
            "projid": 4242,
            "via_agent": False,
        }
    ]
    assert not (volume_path / "sbx_a").exists()
    # Other slices and the volume root survive.
    assert (volume_path / "sbx_b").is_dir()
    assert (volume_path / "shared.txt").read_text() == "keep me"


def test_cleanup_volume_projects_skips_invalid_and_mismatched_records(
    monkeypatch, tmp_path
):
    volume_path = tmp_path / "vol_1"
    (volume_path / "sbx_a").mkdir(parents=True)
    (volume_path / "sbx_b").mkdir()
    released = []
    monkeypatch.setattr(volumes, "release_project", lambda **kw: released.append(kw))
    cleanup_volume_projects(
        volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_a",
                "mount_path": "mnt/data",
                "sandbox_dir": str(volume_path / "sbx_b"),  # mismatched name
                "projid": 1,
            },
            {"volume_id": "vol_1"},  # missing sandbox_id
        ],
        fallback_mount_point="/srv",
        via_agent=False,
    )
    assert released == []
    assert (volume_path / "sbx_a").is_dir()
    assert (volume_path / "sbx_b").is_dir()


def test_cleanup_volume_projects_survives_release_failure(monkeypatch, tmp_path):
    from envd_service.xfs_quota import ProjectQuotaError

    volume_path = tmp_path / "vol_1"
    (volume_path / "sbx_a").mkdir(parents=True)

    def boom(**kwargs):
        raise ProjectQuotaError("release boom")

    monkeypatch.setattr(volumes, "release_project", boom)
    cleanup_volume_projects(
        volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_a",
                "mount_path": "mnt/data",
                "sandbox_dir": str(volume_path / "sbx_a"),
                "projid": 4242,
            }
        ],
        fallback_mount_point="/srv",
        via_agent=False,
    )
    # Files are still removed; the quota entry is left for E2.4 reconcile.
    assert not (volume_path / "sbx_a").exists()


# ------------------------------------------------- E2.4 reconcile interplay


def test_recorded_projids_includes_volume_projects(tmp_path):
    sandbox_dir = tmp_path / "sbx_a"
    sandbox_dir.mkdir()
    (sandbox_dir / "sandbox.json").write_text(
        json.dumps(
            {
                "sandbox_id": "sbx_a",
                "project_id": 100,
                "volume_projects": [
                    {
                        "volume_id": "vol_1",
                        "sandbox_id": "sbx_a",
                        "mount_path": "mnt/data",
                        "sandbox_dir": str(sandbox_dir),
                        "projid": 200,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert xfs_quota._recorded_projids(tmp_path) == {100, 200}


# ------------------------------------------------- runtime wiring (E2.5)


def test_runtime_context_exposes_volume_subdir_view_to_executor(
    tmp_path, monkeypatch
):
    from envd_service.executors.base import Executor
    from envd_service.runtime import context as context_module
    from envd_service.runtime.context import SandboxRuntimeContext

    captured = {}

    class _FakeExecutor(Executor):
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self, config):  # pragma: no cover - never invoked
            raise NotImplementedError

    monkeypatch.setattr(
        context_module, "create_executor", lambda settings, **kw: _FakeExecutor(**kw)
    )
    workspace = tmp_path / "sbx_a"
    workspace.mkdir()
    volume_path = tmp_path / "vol_1"
    subdir = volume_path / "sbx_a"
    subdir.mkdir(parents=True)
    record = RuntimeSandbox(
        sandbox_id="sbx_a",
        access_token="tok",
        workspace_dir=str(workspace),
        volume_mounts=[{"path": "mnt/data", "hostPath": str(subdir)}],
        volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_a",
                "mount_path": "mnt/data",
                "sandbox_dir": str(subdir),
                "projid": 4242,
            }
        ],
    )
    context = SandboxRuntimeContext(
        record, EnvdSettings(executor="sandlock", workspace_base=tmp_path)
    )
    # Landlock second-layer fallback: only the sandbox's own slice is
    # writable, and the chroot mount view points at that slice under BOTH
    # workspace aliases (a relative open resolves against whichever alias
    # the sandbox's cwd names).
    assert captured["extra_fs_writable"] == [str(subdir)]
    assert captured["fs_mounts"] == {
        "/workspace/mnt/data": str(subdir),
        "/home/user/mnt/data": str(subdir),
    }


def test_sandlock_policy_only_writes_the_volume_subdir(tmp_path):
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    workspace = tmp_path / "sbx_a"
    workspace.mkdir()
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    subdir = tmp_path / "vol_1" / "sbx_a"
    subdir.mkdir(parents=True)
    executor = SandlockExecutor(
        workspace_dir=str(workspace),
        base_image="python:3.11-slim",
        image_rootfs=rootfs,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        extra_fs_writable=[str(subdir)],
        fs_mounts={"/workspace/mnt/data": str(subdir)},
    )
    policy = executor._build_sandbox(
        ExecConfig(cmd=["/bin/sh"], env={}, cwd=str(workspace), stdin_enabled=False)
    )
    assert str(subdir) in policy.fs_writable
    assert policy.fs_mount["/workspace/mnt/data"] == str(subdir)
    # The volume root (parent) is not writable through Landlock.
    assert str(subdir.parent) not in policy.fs_writable


# ------------------------------------------------------- worker agent wiring


def _agent_app(tmp_path, monkeypatch, cleanup_calls):
    import envd_service.agent as agent_module
    from envd_service.app import create_app as create_envd_app
    from envd_service.runtime.registry import RuntimeRegistry

    monkeypatch.setattr(xfs_quota, "xfs_project_supported", _supported)
    monkeypatch.setattr(agent_module, "xfs_project_supported", _supported)
    monkeypatch.setattr(volumes, "xfs_project_supported", _supported)
    monkeypatch.setattr(
        agent_module, "provision_project", lambda **kw: 777
    )
    monkeypatch.setattr(volumes, "provision_project", lambda **kw: 4242)
    monkeypatch.setattr(volumes, "containing_mount_point", lambda _path: None)
    monkeypatch.setattr(
        agent_module,
        "cleanup_volume_projects",
        lambda **kw: cleanup_calls.append(kw),
    )
    return create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=RuntimeRegistry(tmp_path),
    )


async def _agent_post(app, sandbox_id, volume_mounts):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/agent/sandboxes",
            headers={"X-Internal-Key": "internal-key"},
            json={
                "sandboxID": sandbox_id,
                "accessToken": "tok",
                "envVars": {},
                "baseImage": None,
                "memoryMB": 512,
                "cpuPercent": 100,
                "diskMB": 32,
                "maxProcesses": 64,
                "allowInternetAccess": False,
                "maxCommandTimeout": 3600,
                "volumeMounts": volume_mounts,
            },
        )


async def test_agent_create_persists_volume_projects_and_delete_cleans(
    tmp_path, monkeypatch
):
    cleanup_calls = []
    app = _agent_app(tmp_path, monkeypatch, cleanup_calls)
    sandbox_id = "sbx_agent"
    volume_root = tmp_path / "vol_1"
    volume_root.mkdir()
    response = await _agent_post(
        app,
        sandbox_id,
        [
            {
                "name": "vol_1",
                "path": "mnt/data",
                "hostPath": str(volume_root),
                "perSandboxQuotaMb": 512,
            }
        ],
    )
    assert response.status_code == 201
    record = app.state.runtime_registry.get(sandbox_id)
    slice_dir = volume_root / sandbox_id
    assert record.volume_mounts == [
        {"path": "mnt/data", "hostPath": str(slice_dir)}
    ]
    assert record.volume_projects == [
        {
            "volume_id": "vol_1",
            "sandbox_id": sandbox_id,
            "mount_path": "mnt/data",
            "sandbox_dir": str(slice_dir),
            "projid": 4242,
        }
    ]
    assert slice_dir.is_dir()

    # The delete's targets come from the disk (the record is sandbox-writable,
    # review W1): the workspace tree reports 777, the slice 4242 -- the same
    # values the create provisioned.
    _install_disk_projids(
        monkeypatch, {tmp_path / sandbox_id: 777, slice_dir: 4242}
    )

    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        deleted = await client.delete(
            f"/agent/sandboxes/{sandbox_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert deleted.status_code == 204
    assert cleanup_calls == [
        {
            "volume_projects": record.volume_projects,
            "fallback_mount_point": tmp_path,
            "via_agent": False,
        }
    ]


async def test_agent_delete_keepfiles_skips_volume_cleanup(tmp_path, monkeypatch):
    cleanup_calls = []
    app = _agent_app(tmp_path, monkeypatch, cleanup_calls)
    sandbox_id = "sbx_keep"
    volume_root = tmp_path / "vol_1"
    volume_root.mkdir()
    response = await _agent_post(
        app,
        sandbox_id,
        [
            {
                "name": "vol_1",
                "path": "mnt/data",
                "hostPath": str(volume_root),
                "perSandboxQuotaMb": 512,
            }
        ],
    )
    assert response.status_code == 201
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        deleted = await client.delete(
            f"/agent/sandboxes/{sandbox_id}?keepFiles=true",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert deleted.status_code == 204
    assert cleanup_calls == []
    # Files (workspace + volume slice) survive for migration rollback.
    assert (volume_root / sandbox_id).is_dir()


async def test_agent_delete_keep_volume_slices_removes_workspace_but_keeps_slices(
    tmp_path, monkeypatch
):
    """C1: migration cleanup removes the workspace, never the shared slice."""
    cleanup_calls = []
    app = _agent_app(tmp_path, monkeypatch, cleanup_calls)
    sandbox_id = "sbx_migrated"
    volume_root = tmp_path / "vol_1"
    volume_root.mkdir()
    response = await _agent_post(
        app,
        sandbox_id,
        [
            {
                "name": "vol_1",
                "path": "mnt/data",
                "hostPath": str(volume_root),
                "perSandboxQuotaMb": 512,
            }
        ],
    )
    assert response.status_code == 201
    slice_dir = volume_root / sandbox_id
    (slice_dir / "payload.bin").write_bytes(b"keep-me")
    workspace_dir = tmp_path / sandbox_id
    # The workspace release is driven by the projid the disk reports for the
    # tree (review W1); the slice is kept by the flag below.
    _install_disk_projids(monkeypatch, {workspace_dir: 777})

    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        deleted = await client.delete(
            f"/agent/sandboxes/{sandbox_id}?keepVolumeSlices=true",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert deleted.status_code == 204
    # Volume slice cleanup is skipped (the target node re-provisioned the
    # same shared slice), while the migrated workspace is released.
    assert cleanup_calls == []
    assert (slice_dir / "payload.bin").read_bytes() == b"keep-me"
    assert not workspace_dir.exists()
