"""XFS project quota management (E2.2): allocation, provision, release."""

from __future__ import annotations

import logging
import subprocess

import pytest

import envd_service.xfs_quota as xfs_quota
from envd_service.xfs_quota import (
    ProjectQuotaError,
    allocate_project_id,
    provision_project,
    release_project,
)

MOUNT = "/srv/sandboxes"


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(monkeypatch, responses, calls):
    """Patch subprocess.run; responses map the -c command string to a result."""

    def fake_run(args, **kwargs):
        calls.append(args)
        command = args[args.index("-c") + 1] if "-c" in args else ""
        returncode, stdout, stderr = responses.get(command, (0, "", ""))
        return _FakeProc(returncode, stdout, stderr)

    monkeypatch.setattr(xfs_quota.subprocess, "run", fake_run)


def _report(*projects: str) -> str:
    lines = [
        f"Project quota on {MOUNT} (/dev/loop0)",
        "                               Blocks",
        "Project ID       Used       Soft       Hard    Warn/Time",
    ]
    lines.extend(projects)
    return "\n".join(lines) + "\n"


def test_parse_project_report_extracts_numeric_and_hash_rows():
    output = _report(
        "#0                  4          0          0    00 [--------]",
        "#100               40          0       1024    00 [--------]",
    )
    assert xfs_quota._parse_project_report(output) == {0, 100}


def test_parse_project_report_supports_numeric_format():
    output = _report(
        "0                    4          0          0    00 [--------]",
        "123                 40          0       1024    00 [--------]",
    )
    assert xfs_quota._parse_project_report(output) == {0, 123}


def test_hash_projid_stable_in_range_and_distinct():
    first = xfs_quota._hash_projid("sbx_alpha")
    assert xfs_quota._hash_projid("sbx_alpha") == first
    assert 1 <= first <= xfs_quota._PROJID_MAX
    second = xfs_quota._hash_projid("sbx_beta")
    assert second != first
    assert 1 <= second <= xfs_quota._PROJID_MAX


def test_allocate_uses_hash_when_project_free(monkeypatch):
    calls: list[list[str]] = []
    _fake_run(monkeypatch, {"report -p": (0, _report("#0 4 0 0 00 [--------]"), "")}, calls)
    expected = xfs_quota._hash_projid("sbx_free")
    assert allocate_project_id("sbx_free", MOUNT) == expected
    assert calls == [["xfs_quota", "-x", "-c", "report -p", MOUNT]]


def test_allocate_probes_forward_on_collision(monkeypatch):
    calls: list[list[str]] = []
    collision = xfs_quota._hash_projid("sbx_collide")
    _fake_run(
        monkeypatch,
        {"report -p": (0, _report(f"#{collision} 40 0 1024 00 [--------]"), "")},
        calls,
    )
    expected = collision + 1
    assert allocate_project_id("sbx_collide", MOUNT) == expected


def test_allocate_wraps_to_min_at_max(monkeypatch):
    calls: list[list[str]] = []
    top = xfs_quota._PROJID_MAX
    monkeypatch.setattr(xfs_quota, "_hash_projid", lambda _sid: top)
    _fake_run(
        monkeypatch,
        {"report -p": (0, _report(f"#{top} 40 0 1024 00 [--------]"), "")},
        calls,
    )
    assert allocate_project_id("sbx_top", MOUNT) == xfs_quota._PROJID_MIN


def test_allocate_report_failure_raises(monkeypatch):
    calls: list[list[str]] = []
    _fake_run(monkeypatch, {"report -p": (1, "", "report boom")}, calls)
    with pytest.raises(ProjectQuotaError) as excinfo:
        allocate_project_id("sbx_fail", MOUNT)
    assert str(excinfo.value) == "xfs_quota 'report -p' failed: report boom"


def test_provision_runs_project_then_limit(monkeypatch):
    calls: list[list[str]] = []
    free = xfs_quota._hash_projid("sbx_prov")
    _fake_run(
        monkeypatch,
        {"report -p": (0, _report("#0 4 0 0 00 [--------]"), "")},
        calls,
    )
    projid = provision_project(
        sandbox_id="sbx_prov",
        project_dir=f"{MOUNT}/sbx_prov",
        mount_point=MOUNT,
        disk_mb=1024,
    )
    assert projid == free
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        [
            "xfs_quota",
            "-x",
            "-c",
            f"project -s -p {MOUNT}/sbx_prov {free}",
            MOUNT,
        ],
        ["xfs_quota", "-x", "-c", f"limit -p bhard=1024M {free}", MOUNT],
    ]


def test_provision_quotes_project_dir_with_space(monkeypatch):
    calls: list[list[str]] = []
    free = xfs_quota._hash_projid("sbx_space")
    _fake_run(
        monkeypatch,
        {"report -p": (0, _report("#0 4 0 0 00 [--------]"), "")},
        calls,
    )
    provision_project(
        sandbox_id="sbx_space",
        project_dir="/srv/my sandboxes/sbx_space",
        mount_point=MOUNT,
        disk_mb=512,
    )
    assert calls[1][3] == f"project -s -p '/srv/my sandboxes/sbx_space' {free}"
    assert calls[2][3] == f"limit -p bhard=512M {free}"


def test_provision_reuses_existing_project_id_without_allocation(monkeypatch):
    calls: list[list[str]] = []
    _fake_run(monkeypatch, {}, calls)
    projid = provision_project(
        sandbox_id="sbx_reuse",
        project_dir=f"{MOUNT}/sbx_reuse",
        mount_point=MOUNT,
        disk_mb=2048,
        project_id=777,
    )
    assert projid == 777
    assert calls == [
        ["xfs_quota", "-x", "-c", f"project -s -p {MOUNT}/sbx_reuse 777", MOUNT],
        ["xfs_quota", "-x", "-c", "limit -p bhard=2048M 777", MOUNT],
    ]


def test_provision_project_setup_failure_raises(monkeypatch):
    calls: list[list[str]] = []
    free = xfs_quota._hash_projid("sbx_fail")
    responses = {
        "report -p": (0, _report("#0 4 0 0 00 [--------]"), ""),
        f"project -s -p {MOUNT}/sbx_fail {free}": (1, "", "setup boom"),
    }
    _fake_run(monkeypatch, responses, calls)
    with pytest.raises(ProjectQuotaError) as excinfo:
        provision_project(
            sandbox_id="sbx_fail",
            project_dir=f"{MOUNT}/sbx_fail",
            mount_point=MOUNT,
            disk_mb=1024,
        )
    assert str(excinfo.value) == (
        f"project setup failed for sbx_fail: xfs_quota "
        f"'project -s -p {MOUNT}/sbx_fail {free}' failed: setup boom"
    )


def test_provision_limit_failure_cleans_up_project(monkeypatch, caplog):
    calls: list[list[str]] = []
    free = xfs_quota._hash_projid("sbx_partial")
    _fake_run(monkeypatch, {}, calls)
    responses = {
        "report -p": (0, _report("#0 4 0 0 00 [--------]"), ""),
        f"project -s -p {MOUNT}/sbx_partial {free}": (0, "", ""),
        f"limit -p bhard=1024M {free}": (1, "", "limit boom"),
        f"project -C -p {MOUNT}/sbx_partial {free}": (0, "", ""),
    }
    _fake_run(monkeypatch, responses, calls)
    with pytest.raises(ProjectQuotaError) as excinfo:
        provision_project(
            sandbox_id="sbx_partial",
            project_dir=f"{MOUNT}/sbx_partial",
            mount_point=MOUNT,
            disk_mb=1024,
        )
    assert str(excinfo.value) == (
        f"quota limit setup failed for sbx_partial: xfs_quota "
        f"'limit -p bhard=1024M {free}' failed: limit boom"
    )
    assert calls[-1] == [
        "xfs_quota",
        "-x",
        "-c",
        f"project -C -p {MOUNT}/sbx_partial {free}",
        MOUNT,
    ]
    assert [r.message for r in caplog.records] == []


def test_provision_cleanup_failure_logs_warning(monkeypatch, caplog):
    calls: list[list[str]] = []
    free = xfs_quota._hash_projid("sbx_messy")
    caplog.set_level(logging.WARNING)
    responses = {
        "report -p": (0, _report("#0 4 0 0 00 [--------]"), ""),
        f"project -s -p {MOUNT}/sbx_messy {free}": (0, "", ""),
        f"limit -p bhard=1024M {free}": (1, "", "limit boom"),
        f"project -C -p {MOUNT}/sbx_messy {free}": (1, "", "cleanup boom"),
    }
    _fake_run(monkeypatch, responses, calls)
    with pytest.raises(ProjectQuotaError) as excinfo:
        provision_project(
            sandbox_id="sbx_messy",
            project_dir=f"{MOUNT}/sbx_messy",
            mount_point=MOUNT,
            disk_mb=1024,
        )
    assert [r.message for r in caplog.records] == [
        f"project cleanup failed for sbx_messy (projid {free}): xfs_quota "
        f"'project -C -p {MOUNT}/sbx_messy {free}' failed: cleanup boom",
    ]
    assert str(excinfo.value).startswith("quota limit setup failed for sbx_messy:")


def test_provision_oserror_degrades(monkeypatch):
    def boom(_args, **_kwargs):
        raise OSError("xfs_quota missing")

    monkeypatch.setattr(xfs_quota.subprocess, "run", boom)
    with pytest.raises(ProjectQuotaError) as excinfo:
        provision_project(
            sandbox_id="sbx_missing",
            project_dir=f"{MOUNT}/sbx_missing",
            mount_point=MOUNT,
            disk_mb=1024,
            project_id=9,
        )
    assert str(excinfo.value) == (
        "project setup failed for sbx_missing: xfs_quota "
        f"'project -s -p {MOUNT}/sbx_missing 9' failed: xfs_quota missing"
    )


def test_provision_timeout_degrades(monkeypatch):
    def boom(args, **_kwargs):
        raise subprocess.TimeoutExpired(args, timeout=10)

    monkeypatch.setattr(xfs_quota.subprocess, "run", boom)
    with pytest.raises(ProjectQuotaError) as excinfo:
        provision_project(
            sandbox_id="sbx_slow",
            project_dir=f"{MOUNT}/sbx_slow",
            mount_point=MOUNT,
            disk_mb=1024,
            project_id=9,
        )
    assert str(excinfo.value).startswith(
        "project setup failed for sbx_slow: xfs_quota 'project -s -p "
    )


def test_release_runs_project_clear(monkeypatch):
    calls: list[list[str]] = []
    _fake_run(monkeypatch, {}, calls)
    release_project(
        project_dir=f"{MOUNT}/sbx_del",
        mount_point=MOUNT,
        projid=321,
    )
    assert calls == [
        ["xfs_quota", "-x", "-c", f"project -C -p {MOUNT}/sbx_del 321", MOUNT],
    ]


def test_release_failure_raises(monkeypatch):
    calls: list[list[str]] = []
    _fake_run(
        monkeypatch,
        {f"project -C -p {MOUNT}/sbx_del 321": (1, "", "clear boom")},
        calls,
    )
    with pytest.raises(ProjectQuotaError) as excinfo:
        release_project(
            project_dir=f"{MOUNT}/sbx_del",
            mount_point=MOUNT,
            projid=321,
        )
    assert str(excinfo.value) == (
        f"xfs_quota 'project -C -p {MOUNT}/sbx_del 321' failed: clear boom"
    )


def test_provision_via_agent_dispatches(monkeypatch):
    seen: dict = {}

    def provision(**kwargs):
        seen.update(kwargs)
        return 4242

    monkeypatch.setattr(xfs_quota, "agent_ops", {"provision": provision})
    projid = provision_project(
        sandbox_id="sbx_nfs",
        project_dir=f"{MOUNT}/sbx_nfs",
        mount_point=MOUNT,
        disk_mb=1024,
        via_agent=True,
        project_id=None,
    )
    assert projid == 4242
    assert seen == {
        "sandbox_id": "sbx_nfs",
        "project_dir": f"{MOUNT}/sbx_nfs",
        "mount_point": MOUNT,
        "disk_mb": 1024,
        "project_id": None,
    }


def test_release_via_agent_dispatches(monkeypatch):
    seen: dict = {}

    def release(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(xfs_quota, "agent_ops", {"release": release})
    release_project(
        project_dir=f"{MOUNT}/sbx_nfs",
        mount_point=MOUNT,
        projid=4242,
        via_agent=True,
    )
    assert seen == {
        "project_dir": f"{MOUNT}/sbx_nfs",
        "mount_point": MOUNT,
        "projid": 4242,
    }


def test_via_agent_not_configured_raises(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_ops", None)
    with pytest.raises(ProjectQuotaError) as excinfo:
        provision_project(
            sandbox_id="sbx_nfs",
            project_dir=f"{MOUNT}/sbx_nfs",
            mount_point=MOUNT,
            disk_mb=1024,
            via_agent=True,
        )
    assert str(excinfo.value) == "quota-agent not configured (E2.6)"


def test_via_agent_missing_op_raises(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_ops", {"provision": lambda **_kw: 1})
    with pytest.raises(ProjectQuotaError) as excinfo:
        release_project(
            project_dir=f"{MOUNT}/sbx_nfs",
            mount_point=MOUNT,
            projid=1,
            via_agent=True,
        )
    assert str(excinfo.value) == "quota-agent op 'release' not configured (E2.6)"


def test_via_agent_op_failure_wraps(monkeypatch):
    def provision(**kwargs):
        raise RuntimeError("agent unreachable")

    monkeypatch.setattr(xfs_quota, "agent_ops", {"provision": provision})
    with pytest.raises(ProjectQuotaError) as excinfo:
        provision_project(
            sandbox_id="sbx_nfs",
            project_dir=f"{MOUNT}/sbx_nfs",
            mount_point=MOUNT,
            disk_mb=1024,
            via_agent=True,
        )
    assert str(excinfo.value) == "quota-agent provision failed: agent unreachable"


def test_configure_agent_ops_wires_and_resets(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_ops", None)
    ops = {"provision": lambda **_kw: 1}
    xfs_quota.configure_agent_ops(ops)
    try:
        assert xfs_quota.agent_ops is ops
    finally:
        xfs_quota.configure_agent_ops(None)
    assert xfs_quota.agent_ops is None
