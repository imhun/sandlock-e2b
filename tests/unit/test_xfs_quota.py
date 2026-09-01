"""XFS project quota detection (E2.1): local + quota-agent paths."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import envd_service.xfs_quota as xfs_quota
from envd_service.xfs_quota import xfs_project_supported


def _mounts(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _xfs_info(projid32bit: int) -> str:
    return (
        "meta-data=/dev/nvme0n1p2     isize=512    agcount=4, agsize=131072 blks\n"
        f"         =                   sectsz=512   attr=2, projid32bit={projid32bit}\n"
    )


def test_local_non_xfs_fs_fails(monkeypatch, caplog):
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: _mounts("/dev/sda1 /srv/sandboxes ext4 rw,relatime 0 0"),
    )
    caplog.set_level(logging.WARNING)
    assert xfs_project_supported("/srv/sandboxes") == (
        False,
        "filesystem is ext4, not xfs",
    )
    assert [r.message for r in caplog.records] == [
        "XFS project quota unavailable for /srv/sandboxes: "
        "filesystem is ext4, not xfs",
    ]


def test_local_projid32bit_disabled_fails(monkeypatch):
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: _mounts("/dev/nvme0n1p2 /srv/sandboxes xfs rw,prjquota,relatime 0 0"),
    )
    monkeypatch.setattr(xfs_quota, "_run_xfs_info", lambda _mp: _xfs_info(0))
    monkeypatch.setattr(xfs_quota, "_xfs_quota_available", lambda: True)
    assert xfs_project_supported("/srv/sandboxes") == (
        False,
        "xfs projid32bit not enabled (projid32bit=0)",
    )


def test_local_noquota_fails(monkeypatch):
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: _mounts("/dev/nvme0n1p2 /srv/sandboxes xfs rw,noquota,relatime 0 0"),
    )
    monkeypatch.setattr(xfs_quota, "_run_xfs_info", lambda _mp: _xfs_info(1))
    monkeypatch.setattr(xfs_quota, "_xfs_quota_available", lambda: True)
    assert xfs_project_supported("/srv/sandboxes") == (
        False,
        "mount option prjquota not enabled",
    )


def test_local_missing_xfs_quota_tool_fails(monkeypatch):
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: _mounts("/dev/nvme0n1p2 /srv/sandboxes xfs rw,prjquota,relatime 0 0"),
    )
    monkeypatch.setattr(xfs_quota, "_run_xfs_info", lambda _mp: _xfs_info(1))
    monkeypatch.setattr(xfs_quota, "_xfs_quota_available", lambda: False)
    assert xfs_project_supported("/srv/sandboxes") == (
        False,
        "xfs_quota tool not found",
    )


def test_local_xfs_prjquota_supported(monkeypatch, caplog):
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: _mounts("/dev/nvme0n1p2 /srv/sandboxes xfs rw,prjquota,relatime 0 0"),
    )
    monkeypatch.setattr(xfs_quota, "_run_xfs_info", lambda _mp: _xfs_info(1))
    monkeypatch.setattr(xfs_quota, "_xfs_quota_available", lambda: True)
    caplog.set_level(logging.WARNING)
    assert xfs_project_supported(Path("/srv/sandboxes")) == (True, "")
    assert [r.message for r in caplog.records] == []


def test_local_proc_mounts_unreadable_degrades_without_raising(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(xfs_quota, "_read_proc_mounts", lambda: None)
    monkeypatch.setattr(
        xfs_quota,
        "_run_xfs_info",
        lambda _mp: calls.append("xfs_info") or "unused",
    )
    monkeypatch.setattr(
        xfs_quota,
        "_xfs_quota_available",
        lambda: calls.append("tool") or True,
    )
    assert xfs_project_supported("/srv/sandboxes") == (
        False,
        "filesystem detection unavailable: cannot read /proc/mounts",
    )
    assert calls == []


def test_local_mount_not_found_fails(monkeypatch):
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: _mounts("/dev/sda1 /other xfs rw,prjquota 0 0"),
    )
    assert xfs_project_supported("/srv/sandboxes") == (
        False,
        "no mount entry found for /srv/sandboxes",
    )


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (
            {"fs_type": "ext4", "projid32bit": False, "prjquota": False, "xfs_quota": True},
            (False, "filesystem is ext4, not xfs"),
        ),
        (
            {"fs_type": "xfs", "projid32bit": False, "prjquota": True, "xfs_quota": True},
            (False, "xfs projid32bit not enabled (projid32bit=0)"),
        ),
        (
            {"fs_type": "xfs", "projid32bit": True, "prjquota": False, "xfs_quota": True},
            (False, "mount option prjquota not enabled"),
        ),
        (
            {"fs_type": "xfs", "projid32bit": True, "prjquota": True, "xfs_quota": False},
            (False, "xfs_quota tool not found"),
        ),
    ],
)
def test_via_agent_server_side_facts(monkeypatch, facts, expected):
    seen: list[str] = []

    def query(mount_point: str) -> dict:
        seen.append(mount_point)
        return facts

    monkeypatch.setattr(xfs_quota, "agent_query", query)
    assert xfs_project_supported("/mnt/nfs", via_agent=True) == expected
    assert seen == ["/mnt/nfs"]


def test_via_agent_supported_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr(
        xfs_quota,
        "agent_query",
        lambda _mp: {
            "fs_type": "xfs",
            "projid32bit": True,
            "prjquota": True,
            "xfs_quota": True,
        },
    )
    caplog.set_level(logging.WARNING)
    assert xfs_project_supported("/mnt/nfs", via_agent=True) == (True, "")
    assert [r.message for r in caplog.records] == []


def test_via_agent_unsupported_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(
        xfs_quota,
        "agent_query",
        lambda _mp: {
            "fs_type": "ext4",
            "projid32bit": False,
            "prjquota": False,
            "xfs_quota": True,
        },
    )
    caplog.set_level(logging.WARNING)
    assert xfs_project_supported("/mnt/nfs", via_agent=True) == (
        False,
        "filesystem is ext4, not xfs",
    )
    assert [r.message for r in caplog.records] == [
        "XFS project quota unavailable for /mnt/nfs: filesystem is ext4, not xfs",
    ]


def test_via_agent_not_configured_degrades(monkeypatch, caplog):
    monkeypatch.setattr(xfs_quota, "agent_query", None)
    caplog.set_level(logging.WARNING)
    assert xfs_project_supported("/mnt/nfs", via_agent=True) == (
        False,
        "quota-agent not configured (E2.6)",
    )
    assert [r.message for r in caplog.records] == [
        "XFS project quota unavailable for /mnt/nfs: "
        "quota-agent not configured (E2.6)",
    ]


def test_via_agent_query_error_degrades(monkeypatch):
    def boom(_mount_point: str) -> dict:
        raise RuntimeError("agent unreachable")

    monkeypatch.setattr(xfs_quota, "agent_query", boom)
    assert xfs_project_supported("/mnt/nfs", via_agent=True) == (
        False,
        "quota-agent query failed: agent unreachable",
    )


def test_configure_agent_query_wires_module_hook(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_query", None)

    def fake_query(_mount_point: str) -> dict:
        return {"fs_type": "xfs", "projid32bit": True, "prjquota": True, "xfs_quota": True}

    xfs_quota.configure_agent_query(fake_query)
    try:
        assert xfs_quota.agent_query is fake_query
        assert xfs_project_supported("/mnt/nfs", via_agent=True) == (True, "")
    finally:
        xfs_quota.configure_agent_query(None)
    assert xfs_quota.agent_query is None
