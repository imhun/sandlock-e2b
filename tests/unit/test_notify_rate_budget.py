"""N79: the stat family gets its own seccomp notification budget.

Metadata-heavy work runs thousands of stat-family syscalls per second. While
those shared the general ``notify_rate_limit`` window, an ordinary
``find``/``git status`` spent the budget in a fraction of a second and the
supervisor then slept out the rest of the second (measured 2026-10-05:
852-864 ms every 1.0007 s). The budget is split now:
``E2B_SANDBOX_STAT_NOTIFY_RATE_LIMIT`` gives the stat family its own window,
and 0 folds it back into the general one -- the behaviour before N79, and the
rollback.
"""

from __future__ import annotations

from pathlib import Path

from envd_service.config import Settings
from envd_service.executors.sandlock import SandlockExecutor


def _executor(tmp_path: Path, **overrides) -> SandlockExecutor:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        workspace_dir=str(ws),
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id="sbx_n79",
        pure_rootfs_dir=str(tmp_path / "_pure_rootfs"),
    )
    kwargs.update(overrides)
    return SandlockExecutor(**kwargs)


def test_the_stat_budget_defaults_to_20000(monkeypatch) -> None:
    monkeypatch.delenv("E2B_SANDBOX_NOTIFY_RATE_LIMIT", raising=False)
    monkeypatch.delenv("E2B_SANDBOX_STAT_NOTIFY_RATE_LIMIT", raising=False)
    settings = Settings()
    assert settings.sandbox_notify_rate_limit == 5000
    assert settings.sandbox_stat_notify_rate_limit == 20000


def test_the_stat_budget_is_env_tunable(monkeypatch) -> None:
    monkeypatch.setenv("E2B_SANDBOX_STAT_NOTIFY_RATE_LIMIT", "60000")
    assert Settings().sandbox_stat_notify_rate_limit == 60000


def test_a_zero_stat_budget_folds_back_into_the_general_window(tmp_path) -> None:
    """0 is not "unlimited": it is "no separate budget", the pre-N79 shape."""
    ceiling = _executor(
        tmp_path, notify_rate_limit=5000, notify_rate_limit_stat=0
    )._policy_ceiling()
    assert ceiling["notify_rate_limit_stat"] is None
    # The general window is still set, so stats stay bounded there.
    assert ceiling["notify_rate_limit"] == 5000


def test_the_stat_budget_lands_in_the_supervise_policy(tmp_path) -> None:
    ceiling = _executor(tmp_path, notify_rate_limit_stat=20000)._policy_ceiling()
    assert ceiling["notify_rate_limit_stat"] == 20000
