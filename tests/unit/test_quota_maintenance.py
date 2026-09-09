"""E2.4: orphan project reconciliation + disk watermark monitoring."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
import pytest

import envd_service.agent as agent
import envd_service.app as app_module
import envd_service.quota_maintenance as quota_maintenance
import envd_service.xfs_quota as xfs_quota
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.nodes import NodeRegistry
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.quota_maintenance import QuotaMonitor
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.xfs_quota import (
    ProjectQuotaError,
    ProjectQuotaUsage,
    _parse_project_usage,
    project_quota_table,
    reconcile_orphan_projects,
)

MOUNT = "/srv/sandboxes"


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _report(*projects: str) -> str:
    lines = [
        f"Project quota on {MOUNT} (/dev/loop0)",
        "                               Blocks",
        "Project ID       Used       Soft       Hard    Warn/Time",
    ]
    lines.extend(projects)
    return "\n".join(lines) + "\n"


def _fake_subprocess(monkeypatch, quota_responses, lsattr_stdout: str = ""):
    """Patch subprocess.run; quota commands keyed by the -c command string."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[0] == "lsattr":
            return _FakeProc(0, stdout=lsattr_stdout)
        command = args[args.index("-c") + 1]
        returncode, stdout, stderr = quota_responses.get(command, (0, "", ""))
        return _FakeProc(returncode, stdout, stderr)

    monkeypatch.setattr(xfs_quota.subprocess, "run", fake_run)
    return calls


def _write_record(workspace: Path, sandbox_id: str, project_id: int | None) -> None:
    path = workspace / sandbox_id / "sandbox.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sandbox_id": sandbox_id, "project_id": project_id}),
        encoding="utf-8",
    )


class _DiskUsage:
    def __init__(self, used: int, total: int) -> None:
        self.used = used
        self.total = total


# ---------------------------------------------------------------- parsing


def test_parse_project_usage_extracts_used_soft_hard_blocks():
    rows = _parse_project_usage(
        _report(
            "#0                  4          0          0    00 [--------]",
            "#100               40          0       1024    00 [--------]",
            "123                512         0       4096    00 [--------]",
        )
    )
    assert rows == {
        0: ProjectQuotaUsage(0, 4, 0, 0),
        100: ProjectQuotaUsage(100, 40, 0, 1024),
        123: ProjectQuotaUsage(123, 512, 0, 4096),
    }


def test_parse_project_usage_ignores_header_and_blank_rows():
    rows = _parse_project_usage(_report())
    assert rows == {}


def test_project_quota_table_runs_report_locally(monkeypatch):
    calls = _fake_subprocess(
        monkeypatch,
        {"report -p": (0, _report("#0 4 0 0 00 [--------]"), "")},
    )
    table = project_quota_table(MOUNT)
    assert table == {0: ProjectQuotaUsage(0, 4, 0, 0)}
    assert calls == [["xfs_quota", "-x", "-c", "report -p", MOUNT]]


def test_project_quota_table_via_agent_dispatches(monkeypatch):
    seen: dict = {}

    def report(**kwargs):
        seen.update(kwargs)
        return {
            "projects": {
                "10": {"used_blocks": 1, "soft_blocks": 2, "hard_blocks": 3}
            }
        }

    monkeypatch.setattr(xfs_quota, "agent_ops", {"report": report})
    table = project_quota_table(MOUNT, via_agent=True)
    assert seen == {"mount_point": MOUNT}
    assert table == {10: ProjectQuotaUsage(10, 1, 2, 3)}


def test_project_quota_table_via_agent_invalid_rows_raise(monkeypatch):
    monkeypatch.setattr(
        xfs_quota,
        "agent_ops",
        {"report": lambda **_kw: {"projects": {"10": {"bogus": 1}}}},
    )
    with pytest.raises(ProjectQuotaError) as excinfo:
        project_quota_table(MOUNT, via_agent=True)
    assert str(excinfo.value) == (
        "quota-agent report returned invalid rows: 'used_blocks'"
    )


def test_project_quota_table_via_agent_invalid_shape_raises(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_ops", {"report": lambda **_kw: {"nope": 1}})
    with pytest.raises(ProjectQuotaError) as excinfo:
        project_quota_table(MOUNT, via_agent=True)
    assert str(excinfo.value) == (
        "quota-agent report returned invalid data: {'nope': 1}"
    )


# ----------------------------------------------------- orphan reconciliation


def test_reconcile_cleans_zero_usage_orphan_and_keeps_recorded(tmp_path, monkeypatch):
    _write_record(tmp_path, "sbx_keep", project_id=100)
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report(
                    "#0                  4          0          0    00 [--------]",
                    "#100                0          0       1024    00 [--------]",
                    "#200                0          0       2048    00 [--------]",
                ),
                "",
            )
        },
    )
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    assert result == {"cleaned": [200], "skipped": []}
    # Zero-usage orphans need no directory scan: only the limit reset runs.
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        ["xfs_quota", "-x", "-c", "limit -p bsoft=0 bhard=0 200", MOUNT],
    ]


def test_reconcile_cleans_orphan_with_leftover_dir_but_keeps_dir(
    tmp_path, monkeypatch, caplog
):
    orphan_dir = tmp_path / "sbx_orphan"
    orphan_dir.mkdir()
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report("#300               512         0       4096    00 [--------]"),
                "",
            )
        },
        lsattr_stdout=f"     300 ---------------- {orphan_dir}\n",
    )
    caplog.set_level(logging.INFO)
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    assert result == {"cleaned": [300], "skipped": []}
    # Directory project state is cleared, the quota record dropped, but the
    # directory itself is kept (orphan cleanup never deletes user files).
    assert orphan_dir.is_dir()
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        ["lsattr", "-p", "-d", str(orphan_dir)],
        [
            "xfs_quota",
            "-x",
            "-c",
            f"project -C -p {orphan_dir} 300",
            MOUNT,
        ],
        ["xfs_quota", "-x", "-c", "limit -p bsoft=0 bhard=0 300", MOUNT],
    ]
    assert [r.message for r in caplog.records] == ["cleaned orphan project 300"]


def test_reconcile_keeps_normal_projids_and_project_zero(tmp_path, monkeypatch):
    _write_record(tmp_path, "sbx_a", project_id=100)
    _write_record(tmp_path, "sbx_b", project_id=200)
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report(
                    "#0                  4          0          0    00 [--------]",
                    "#100                0          0       1024    00 [--------]",
                    "#200                0          0       1024    00 [--------]",
                    "#300                0          0       2048    00 [--------]",
                ),
                "",
            )
        },
    )
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    # Only the unrecorded projid is cleaned; recorded ones and project 0 stay.
    assert result == {"cleaned": [300], "skipped": []}
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        ["xfs_quota", "-x", "-c", "limit -p bsoft=0 bhard=0 300", MOUNT],
    ]


def test_reconcile_skips_used_orphan_without_dir(tmp_path, monkeypatch):
    # A sandbox-shaped directory exists but carries a different projid, so
    # the scan runs and finds no directory for the orphaned id.
    (tmp_path / "sbx_unrelated").mkdir()
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report("#400                10          0       1024    00 [--------]"),
                "",
            )
        },
        lsattr_stdout="",
    )
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    assert result == {
        "cleaned": [],
        "skipped": [
            {
                "projid": 400,
                "reason": (
                    "10 used blocks but no project directory; "
                    "entry left for manual review"
                ),
            }
        ],
    }
    # No destructive command ran: report + read-only dir scan only.
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        ["lsattr", "-p", "-d", str(tmp_path / "sbx_unrelated")],
    ]


def test_reconcile_cleanup_failure_skips_with_warning(tmp_path, monkeypatch, caplog):
    _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report("#200                0          0       2048    00 [--------]"),
                "",
            ),
            "limit -p bsoft=0 bhard=0 200": (1, "", "limit boom"),
        },
    )
    caplog.set_level(logging.WARNING)
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    assert result == {
        "cleaned": [],
        "skipped": [
            {
                "projid": 200,
                "reason": (
                    "xfs_quota 'limit -p bsoft=0 bhard=0 200' failed: limit boom"
                ),
            }
        ],
    }
    assert [r.message for r in caplog.records] == [
        "orphan project 200 cleanup failed: "
        "xfs_quota 'limit -p bsoft=0 bhard=0 200' failed: limit boom",
    ]


def test_reconcile_ignores_malformed_and_none_records(tmp_path, monkeypatch):
    bad = tmp_path / "sbx_bad"
    bad.mkdir()
    (bad / "sandbox.json").write_text("{not json", encoding="utf-8")
    _write_record(tmp_path, "sbx_none", project_id=None)
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report("#777                0          0       1024    00 [--------]"),
                "",
            )
        },
    )
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    # Neither a malformed record nor project_id=None protects a projid.
    assert result == {"cleaned": [777], "skipped": []}
    assert calls[-1] == [
        "xfs_quota",
        "-x",
        "-c",
        "limit -p bsoft=0 bhard=0 777",
        MOUNT,
    ]


def test_reconcile_fail_closed_when_workspace_base_missing(tmp_path, monkeypatch):
    # A zero-usage projid that would be cleaned if the missing base were read
    # as "no recorded projects"; the base is missing so reconcile must refuse.
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report(
                    "#0                  4          0          0    00 [--------]",
                    "#200                0          0       2048    00 [--------]",
                ),
                "",
            )
        },
    )
    missing = tmp_path / "no-such-workspace"
    with pytest.raises(ProjectQuotaError) as excinfo:
        reconcile_orphan_projects(workspace_base=missing, mount_point=MOUNT)
    assert str(excinfo.value) == (
        "reconcile workspace_base missing or unreadable: "
        f"{missing} (FileNotFoundError)"
    )
    # Read-only report only: no limit reset ran (fail-closed, nothing wiped).
    assert calls == [["xfs_quota", "-x", "-c", "report -p", MOUNT]]


def test_reconcile_via_agent_dispatches(monkeypatch):
    seen: dict = {}

    def reconcile(**kwargs):
        seen.update(kwargs)
        return {"cleaned": [7], "skipped": []}

    monkeypatch.setattr(xfs_quota, "agent_ops", {"reconcile": reconcile})
    result = reconcile_orphan_projects(
        workspace_base="/nfs/sandboxes",
        mount_point="/nfs",
        via_agent=True,
    )
    assert seen == {"workspace_base": "/nfs/sandboxes", "mount_point": "/nfs"}
    assert result == {"cleaned": [7], "skipped": []}


def test_reconcile_via_agent_not_configured_raises(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_ops", None)
    with pytest.raises(ProjectQuotaError) as excinfo:
        reconcile_orphan_projects(
            workspace_base="/nfs/sandboxes",
            mount_point="/nfs",
            via_agent=True,
        )
    assert str(excinfo.value) == "quota-agent not configured (E2.6)"


def test_reconcile_via_agent_invalid_data_raises(monkeypatch):
    monkeypatch.setattr(
        xfs_quota, "agent_ops", {"reconcile": lambda **_kw: ["not", "a", "dict"]}
    )
    with pytest.raises(ProjectQuotaError) as excinfo:
        reconcile_orphan_projects(
            workspace_base="/nfs/sandboxes",
            mount_point="/nfs",
            via_agent=True,
        )
    assert str(excinfo.value) == (
        "quota-agent reconcile returned invalid data: ['not', 'a', 'dict']"
    )


# ------------------------------------------------------------ quota monitor


def _monitor_with_quota(monkeypatch, table, disk_usage=None):
    monitor = QuotaMonitor(
        workspace_base=MOUNT,
        mount_point=MOUNT,
        quota_warn_ratio=0.9,
        disk_warn_ratio=0.9,
        disk_error_ratio=0.98,
    )
    monkeypatch.setattr(
        quota_maintenance, "xfs_project_supported", lambda _mp, via_agent=False: (True, "")
    )
    monkeypatch.setattr(
        quota_maintenance, "project_quota_table", lambda _mp, via_agent=False: table
    )
    monkeypatch.setattr(
        quota_maintenance.shutil,
        "disk_usage",
        lambda _path: disk_usage or _DiskUsage(used=10, total=1000),
    )
    return monitor


def test_monitor_reports_over_and_near_limit(monkeypatch, caplog):
    monitor = _monitor_with_quota(
        monkeypatch,
        {
            10: ProjectQuotaUsage(10, 100, 0, 100),
            20: ProjectQuotaUsage(20, 95, 0, 100),
            30: ProjectQuotaUsage(30, 50, 0, 100),
            40: ProjectQuotaUsage(40, 0, 0, 0),
        },
    )
    caplog.set_level(logging.WARNING)
    summary = monitor.inspect_once()
    assert summary["over_limit"] == [10]
    assert summary["near_limit"] == [20]
    assert monitor.over_limit_count == 1
    assert monitor.near_limit_count == 1
    assert monitor.metrics() == {
        "quotaOverLimit": [10],
        "quotaNearLimit": [20],
        "quotaOverLimitCount": 1,
        "quotaNearLimitCount": 1,
        "diskWarnCount": 0,
        "diskErrorCount": 0,
    }
    assert [r.message for r in caplog.records] == [
        "sandbox quota over limit: projid 10 used 100 blocks hard 100",
        "sandbox quota near limit: projid 20 used 95 blocks hard 100",
    ]
    assert [r.levelno for r in caplog.records] == [logging.ERROR, logging.WARNING]


def test_monitor_second_scan_accumulates_counts(monkeypatch):
    monitor = _monitor_with_quota(
        monkeypatch,
        {10: ProjectQuotaUsage(10, 100, 0, 100)},
    )
    monitor.inspect_once()
    monitor.inspect_once()
    assert monitor.over_limit_count == 2
    assert monitor.over_limit == [10]


def test_monitor_disk_watermark_warning(monkeypatch, caplog):
    monitor = _monitor_with_quota(
        monkeypatch,
        {},
        disk_usage=_DiskUsage(used=950, total=1000),
    )
    caplog.set_level(logging.WARNING)
    summary = monitor.inspect_once()
    assert summary["disk_used_ratio"] == 0.95
    assert monitor.disk_warn_count == 1
    assert monitor.disk_error_count == 0
    assert [r.message for r in caplog.records] == [
        "workspace disk watermark warning: 95.0% used (950/1000 bytes)",
    ]


def test_monitor_disk_watermark_critical(monkeypatch, caplog):
    monitor = _monitor_with_quota(
        monkeypatch,
        {},
        disk_usage=_DiskUsage(used=990, total=1000),
    )
    caplog.set_level(logging.WARNING)
    monitor.inspect_once()
    assert monitor.disk_warn_count == 0
    assert monitor.disk_error_count == 1
    assert [r.levelno for r in caplog.records] == [logging.ERROR]


def test_monitor_skips_quota_when_unsupported(monkeypatch):
    monitor = QuotaMonitor(
        workspace_base=MOUNT,
        mount_point=MOUNT,
        disk_warn_ratio=0.9,
        disk_error_ratio=0.98,
    )
    monkeypatch.setattr(
        quota_maintenance,
        "xfs_project_supported",
        lambda _mp, via_agent=False: (False, "filesystem is ext4, not xfs"),
    )
    monkeypatch.setattr(
        quota_maintenance,
        "project_quota_table",
        lambda *_a, **_kw: pytest.fail("quota table must not be read"),
    )
    monkeypatch.setattr(
        quota_maintenance.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(used=10, total=1000),
    )
    summary = monitor.inspect_once()
    assert summary["over_limit"] == []
    assert summary["near_limit"] == []
    assert summary["disk_used_ratio"] == 0.01


def test_monitor_rejects_invalid_ratios():
    with pytest.raises(ValueError):
        QuotaMonitor(
            workspace_base=MOUNT,
            mount_point=MOUNT,
            quota_warn_ratio=0.0,
        )
    with pytest.raises(ValueError):
        QuotaMonitor(
            workspace_base=MOUNT,
            mount_point=MOUNT,
            disk_warn_ratio=0.99,
            disk_error_ratio=0.5,
        )


# ----------------------------------------- heartbeat payload / control plane


def test_heartbeat_usage_payload_includes_disk_and_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(used=512 * 1024 * 1024, total=1024 * 1024 * 1024),
    )
    payload = agent._heartbeat_usage_payload(
        EnvdSettings(workspace_base=tmp_path),
        lambda: {
            "quotaOverLimit": [10],
            "quotaNearLimit": [20],
            "quotaOverLimitCount": 3,
            "quotaNearLimitCount": 2,
            "diskWarnCount": 1,
            "diskErrorCount": 2,
        },
    )
    assert payload == {
        "diskUsedMB": 512,
        "diskTotalMB": 1024,
        "quotaOverLimit": [10],
        "quotaNearLimit": [20],
        "quotaOverLimitCount": 3,
        "quotaNearLimitCount": 2,
        "diskWarnCount": 1,
        "diskErrorCount": 2,
    }


def test_heartbeat_usage_payload_survives_broken_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(used=1, total=1000),
    )

    def broken():
        raise RuntimeError("boom")

    payload = agent._heartbeat_usage_payload(
        EnvdSettings(workspace_base=tmp_path), broken
    )
    assert payload == {"diskUsedMB": 0, "diskTotalMB": 0}


def test_node_record_update_usage_exposed_in_to_dict():
    registry = NodeRegistry()
    record = registry.register(
        node_id="node_u",
        address="http://127.0.0.1:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=4096,
        total_processes=128,
    )
    record.update_usage(
        used_disk_mb=1234,
        disk_total_mb=4096,
        quota_over_limit=[9, 10],
        quota_near_limit=[11],
        quota_over_limit_count=5,
        quota_near_limit_count=2,
        disk_warn_count=1,
        disk_error_count=3,
    )
    data = record.to_dict()
    assert data["usedDiskMB"] == 1234
    assert data["diskTotalMB"] == 4096
    assert data["quotaOverLimit"] == [9, 10]
    assert data["quotaNearLimit"] == [11]
    assert data["quotaOverLimitCount"] == 5
    assert data["quotaNearLimitCount"] == 2
    assert data["diskWarnCount"] == 1
    assert data["diskErrorCount"] == 3


async def test_heartbeat_endpoint_stores_usage_snapshot(tmp_path):
    control = create_control_app(
        settings=ControlSettings(api_keys=("local-key",)),
        runtime_registry=RuntimeRegistry(tmp_path),
        workspace_base=tmp_path,
    )
    headers = {"X-Internal-Key": "internal-key"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        registered = await client.post(
            "/internal/nodes/register",
            headers=headers,
            json={
                "address": "http://127.0.0.1:49983",
                "totalMemoryMB": 1024,
                "totalCPUPercent": 200,
                "totalDiskMB": 4096,
                "totalProcesses": 128,
            },
        )
        node_id = registered.json()["nodeID"]
        response = await client.post(
            f"/internal/nodes/{node_id}/heartbeat",
            headers=headers,
            json={
                "diskUsedMB": 42,
                "diskTotalMB": 4096,
                "quotaOverLimit": [9],
                "quotaNearLimit": [],
                "quotaOverLimitCount": 5,
                "quotaNearLimitCount": 0,
                "diskWarnCount": 1,
                "diskErrorCount": 2,
            },
        )
        assert response.status_code == 204
        record = control.state.nodes.get(node_id)
        assert record.used_disk_mb == 42
        assert record.disk_total_mb == 4096
        assert record.quota_over_limit == [9]
        assert record.quota_near_limit == []
        assert record.quota_over_limit_count == 5
        assert record.quota_near_limit_count == 0
        assert record.disk_warn_count == 1
        assert record.disk_error_count == 2


# ------------------------------------------------------------- app lifespan


async def test_app_lifespan_starts_quota_maintenance(tmp_path, monkeypatch):
    calls: dict = {}

    def fake_reconcile(**kwargs):
        calls.update(kwargs)
        return {"cleaned": [1], "skipped": []}

    monkeypatch.setattr(app_module, "reconcile_orphan_projects", fake_reconcile)
    monkeypatch.setattr(
        quota_maintenance,
        "xfs_project_supported",
        lambda _mp, via_agent=False: (False, "not xfs"),
    )
    app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    async with app.router.lifespan_context(app):
        assert app.state.quota_monitor is not None
        assert app.state.reconcile_task is not None
        await app.state.reconcile_task
        assert calls == {
            "workspace_base": tmp_path,
            "mount_point": tmp_path,
            "via_agent": False,
        }
    assert app.state.quota_monitor._task is None


def test_create_app_nonroot_discloses_direct_quota_downgrade(
    tmp_path, monkeypatch, caplog
):
    """E5.1 review (Important-1/2): a non-root worker without effective
    CAP_SYS_ADMIN on an XFS workspace with the direct local quota path
    (E2B_QUOTA_VIA_AGENT=false) must get a startup warning with the required
    configuration instead of silently losing disk hard limits."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    monkeypatch.setattr(xfs_quota, "_has_effective_cap_sys_admin", lambda: False)
    mount_path = str(Path(tmp_path).resolve())
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: f"/dev/nvme0n1p2 {mount_path} xfs rw,prjquota 0 0\n",
    )
    caplog.set_level(logging.WARNING)
    create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r.message for r in caplog.records if r.name == "envd_service.app"] == [
        app_module.PER_UID_NONROOT_WARNING,
        f"{xfs_quota.NONROOT_DIRECT_QUOTA_REASON} (direct xfs_quota requires "
        "root/CAP_SYS_ADMIN; per-sandbox disk hard limits are disabled while "
        "E2B_QUOTA_VIA_AGENT=false; set E2B_QUOTA_VIA_AGENT=true and deploy "
        "quota-agent, or run the worker as root)",
    ]


def test_create_app_nonroot_with_sys_admin_cap_no_disclosure(
    tmp_path, monkeypatch, caplog
):
    """E5.1 review (Important-2): a non-root worker with effective
    CAP_SYS_ADMIN (k8s runAsUser 65534 + SYS_ADMIN) keeps the direct quota
    path, so the startup downgrade warning must not fire."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_self_status",
        lambda: "Name:\tpytest\nCapEff:\t0000003fffffffff\n",
    )
    caplog.set_level(logging.WARNING)
    create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r for r in caplog.records if "磁盘配额不可用" in r.message] == []


def test_create_app_nonroot_non_xfs_host_no_disclosure(
    tmp_path, monkeypatch, caplog
):
    """E5.1 review (Minor-13): on a non-XFS host the startup warning must
    not blame missing root/CAP_SYS_ADMIN; the real filesystem reason is
    reported by detection instead."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    monkeypatch.setattr(xfs_quota, "_has_effective_cap_sys_admin", lambda: False)
    mount_path = str(Path(tmp_path).resolve())
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: f"/dev/disk1s5 {mount_path} apfs rw,local 0 0\n",
    )
    caplog.set_level(logging.WARNING)
    create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r for r in caplog.records if "磁盘配额不可用" in r.message] == []


def test_create_app_root_direct_quota_no_disclosure(tmp_path, monkeypatch, caplog):
    """Root workers keep the direct xfs_quota path; no downgrade warning."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    caplog.set_level(logging.WARNING)
    create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r for r in caplog.records if "磁盘配额不可用" in r.message] == []


def test_create_app_nonroot_via_agent_no_disclosure(tmp_path, monkeypatch, caplog):
    """Non-root + E2B_QUOTA_VIA_AGENT=true delegates to quota-agent, so the
    direct-path downgrade warning must not fire."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    caplog.set_level(logging.WARNING)
    create_envd_app(
        settings=EnvdSettings(
            executor="local",
            workspace_base=tmp_path,
            quota_via_agent=True,
        ),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r for r in caplog.records if "磁盘配额不可用" in r.message] == []
