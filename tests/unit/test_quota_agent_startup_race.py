"""W6: bounded startup retry for a worker that comes up before quota-agent.

The worker and quota-agent are separate containers in one rollout. When the
worker wins that race, its startup quota probes used to record a degrade that
was only a startup race, and ``QuotaMonitor`` cached that verdict for the
whole process lifetime. These cases pin the three conclusions apart:

* ``E2B_QUOTA_AGENT_URL`` empty — nothing is wired, and the explicit
  "quota-agent not configured" WARNING stays exactly as it was;
* agent not up *yet* — retried inside a bounded window and reported at INFO;
* agent still absent when the window runs out — the ordinary degrade WARNING
  is still logged, so a real degrade is never hidden.
"""

from __future__ import annotations

import logging
import types
from pathlib import Path

import pytest

from envd_service import quota_agent, quota_maintenance, xfs_quota
from envd_service.app import _startup_reconcile
from envd_service.config import Settings
from envd_service.quota_maintenance import QuotaMonitor
from envd_service.xfs_quota import ProjectQuotaError, xfs_project_supported

MOUNT = "/srv/sandboxes"
REFUSED = "quota-agent unreachable: Connection refused"
FACTS = {
    "fs_type": "xfs",
    "projid32bit": True,
    "prjquota": True,
    "xfs_quota": True,
}


def _sleeps(monkeypatch) -> list[float]:
    """Replace the retry's sleep with a recorder (the window is policy)."""
    recorded: list[float] = []
    monkeypatch.setattr(quota_agent, "_sleep", recorded.append)
    return recorded


def _healthy_disk(monkeypatch) -> None:
    """Keep the disk watermark out of the way of the log assertions."""
    monkeypatch.setattr(
        quota_maintenance.shutil,
        "disk_usage",
        lambda path: types.SimpleNamespace(total=1000, used=1, free=999),
    )


def _agent_records(caplog) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name.startswith("envd_service")
    ]


def test_unconfigured_agent_keeps_its_explicit_warning(monkeypatch, caplog) -> None:
    monkeypatch.setattr(xfs_quota, "agent_query", None)
    sleeps = _sleeps(monkeypatch)

    with caplog.at_level(logging.INFO):
        verdict = quota_agent.wait_for_startup_readiness(MOUNT)
        supported, reason = xfs_project_supported(MOUNT, via_agent=True)

    assert verdict == ("unconfigured", None)
    assert sleeps == []
    assert (supported, reason) == (False, "quota-agent not configured (E2.6)")
    assert _agent_records(caplog) == [
        (
            logging.WARNING,
            "XFS project quota unavailable for /srv/sandboxes: "
            "quota-agent not configured (E2.6)",
        )
    ]


def test_a_late_agent_is_absorbed_by_the_monitors_first_scan(
    monkeypatch, caplog, tmp_path: Path
) -> None:
    """The RED case: worker first, agent ready on the third attempt.

    Asserted on what the scan does and logs only — no seam from the fix — so
    the pre-fix tree fails on the recorded degrade warning itself.
    """
    calls: list[str] = []

    def late_agent(mount_point: str) -> dict:
        calls.append(mount_point)
        if len(calls) < 3:
            raise ProjectQuotaError(REFUSED)
        return dict(FACTS)

    monkeypatch.setattr(xfs_quota, "agent_query", late_agent)
    _healthy_disk(monkeypatch)
    table_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        quota_maintenance,
        "project_quota_table",
        lambda mount_point, via_agent=False: (
            table_calls.append((str(mount_point), via_agent)) or {}
        ),
    )
    monitor = QuotaMonitor(
        workspace_base=tmp_path, mount_point=tmp_path, via_agent=True
    )

    with caplog.at_level(logging.INFO):
        monitor.inspect_once()

    # Three attempts inside the window (two refusals, then the answer) and
    # one ordinary probe once the agent is known to be up.
    assert calls == [str(tmp_path)] * 4
    # The scan moved on as a *supported* quota source instead of caching the
    # race as "quota unavailable".
    assert table_calls == [(str(tmp_path), True)]
    assert _agent_records(caplog) == [
        (
            logging.INFO,
            "quota-agent answered on startup attempt 3/8: the startup probe "
            "is no longer racing the agent",
        )
    ]


def test_the_window_spaces_its_attempts_by_the_configured_delay(monkeypatch) -> None:
    calls: list[str] = []

    def late_agent(mount_point: str) -> dict:
        calls.append(mount_point)
        if len(calls) < 3:
            raise ProjectQuotaError(REFUSED)
        return dict(FACTS)

    monkeypatch.setattr(xfs_quota, "agent_query", late_agent)
    sleeps = _sleeps(monkeypatch)

    assert quota_agent.wait_for_startup_readiness(MOUNT) == ("ready", None)
    assert calls == [MOUNT] * 3
    assert sleeps == [quota_agent.STARTUP_READY_DELAY_S] * 2


def test_an_agent_that_never_answers_keeps_the_degrade_visible(
    monkeypatch, caplog, tmp_path: Path
) -> None:
    def absent_agent(mount_point: str) -> dict:
        raise ProjectQuotaError(REFUSED)

    monkeypatch.setattr(xfs_quota, "agent_query", absent_agent)
    sleeps = _sleeps(monkeypatch)
    _healthy_disk(monkeypatch)
    table_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        quota_maintenance,
        "project_quota_table",
        lambda mount_point, via_agent=False: (
            table_calls.append((str(mount_point), via_agent)) or {}
        ),
    )
    monitor = QuotaMonitor(
        workspace_base=tmp_path, mount_point=tmp_path, via_agent=True
    )

    with caplog.at_level(logging.INFO):
        verdict = quota_agent.wait_for_startup_readiness(tmp_path)
        monitor.inspect_once()

    assert verdict == ("unreachable", REFUSED)
    assert sleeps == [quota_agent.STARTUP_READY_DELAY_S] * (
        quota_agent.STARTUP_READY_ATTEMPTS - 1
    )
    assert table_calls == []
    assert _agent_records(caplog) == [
        (
            logging.INFO,
            "quota-agent unreachable after 8 startup attempt(s) over 17.5s: "
            "quota-agent unreachable: Connection refused; the startup probe "
            "now records its own WARNING",
        ),
        (
            logging.WARNING,
            f"XFS project quota unavailable for {tmp_path}: quota-agent query "
            "failed: quota-agent unreachable: Connection refused",
        ),
    ]


def test_the_startup_window_is_bounded_and_runs_once_per_wired_agent(
    monkeypatch,
) -> None:
    first_calls: list[str] = []

    def first_agent(mount_point: str) -> dict:
        first_calls.append(mount_point)
        return dict(FACTS)

    monkeypatch.setattr(xfs_quota, "agent_query", first_agent)
    sleeps = _sleeps(monkeypatch)

    assert quota_agent.wait_for_startup_readiness(MOUNT) == ("ready", None)
    assert quota_agent.wait_for_startup_readiness(MOUNT) == ("ready", None)
    # Ready on the first attempt: no retry, and the second probe (the other
    # startup caller) reused the verdict instead of probing again.
    assert first_calls == [MOUNT]
    assert sleeps == []

    second_calls: list[str] = []
    monkeypatch.setattr(
        xfs_quota,
        "agent_query",
        lambda mount_point: (second_calls.append(mount_point), dict(FACTS))[1],
    )
    assert quota_agent.wait_for_startup_readiness(MOUNT) == ("ready", None)
    assert second_calls == [MOUNT]


@pytest.mark.parametrize("late", [True, False])
async def test_startup_reconcile_waits_for_the_agent(
    monkeypatch, caplog, tmp_path: Path, late: bool
) -> None:
    calls: list[str] = []

    def late_agent(mount_point: str) -> dict:
        calls.append(mount_point)
        if late and len(calls) < 2:
            raise ProjectQuotaError(REFUSED)
        if not late:
            raise ProjectQuotaError(REFUSED)
        return dict(FACTS)

    def reconcile(**kwargs) -> dict:
        if not late:
            raise ProjectQuotaError(REFUSED)
        return {"cleaned": [7], "skipped": []}

    monkeypatch.setattr(xfs_quota, "agent_query", late_agent)
    monkeypatch.setattr(
        xfs_quota,
        "agent_ops",
        {"reconcile": reconcile},
    )
    _sleeps(monkeypatch)
    settings = Settings(workspace_base=tmp_path, quota_via_agent=True)

    with caplog.at_level(logging.INFO):
        await _startup_reconcile(settings)

    if late:
        assert calls == [str(tmp_path), str(tmp_path)]
        assert _agent_records(caplog) == [
            (
                logging.INFO,
                "quota-agent answered on startup attempt 2/8: the startup "
                "probe is no longer racing the agent",
            ),
            (
                logging.INFO,
                "startup quota reconciliation: cleaned=[7] skipped=[]",
            ),
        ]
    else:
        assert calls == [str(tmp_path)] * quota_agent.STARTUP_READY_ATTEMPTS
        assert _agent_records(caplog) == [
            (
                logging.INFO,
                "quota-agent unreachable after 8 startup attempt(s) over "
                "17.5s: quota-agent unreachable: Connection refused; the "
                "startup probe now records its own WARNING",
            ),
            (
                logging.WARNING,
                "startup quota reconciliation skipped: quota-agent reconcile "
                "failed: quota-agent unreachable: Connection refused",
            ),
        ]
