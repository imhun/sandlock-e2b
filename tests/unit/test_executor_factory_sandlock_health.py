"""B1 review: a *broken* sandlock install must never look like a missing one.

``_sandlock_available()`` used to answer ``False`` for every failure, so a
wheel built against a different ``libsandlock_ffi.so`` (or a partially
upgraded image) degraded to :class:`LocalExecutor` -- a sandbox with **no
confinement** -- behind a single INFO line. These tests pin the split:
``ModuleNotFoundError`` keeps the documented fallback, everything else is
loud, and fatal when the operator asked for sandlock explicitly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import envd_service.executors.factory as factory_mod
from envd_service.executors.factory import create_executor
from envd_service.executors.local import LocalExecutor


def _settings(executor: str) -> SimpleNamespace:
    return SimpleNamespace(
        executor=executor,
        enable_network=False,
        enable_netns=False,
        enable_net_isolation=False,
        fd_inject_connect=False,
        port_mappings={},
        network_deny_cidrs=(),
        sandbox_notify_rate_limit=0,
        iam_signing_key="k",
        image_cache_dir=Path("tmp/cache"),
    )


def _create(settings) -> object:
    return create_executor(
        settings,
        workspace_dir="/tmp/ws",
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        network=None,
    )


def test_explicit_sandlock_mode_refuses_a_broken_package(monkeypatch) -> None:
    """``E2B_EXECUTOR=sandlock`` + broken install == hard failure, never local."""
    broken = ImportError(
        "sandlock_create_with_err is missing from the loaded sandlock library"
    )
    # The dev host is macOS; the probe is Linux-only, so pin it here.
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: broken)

    with pytest.raises(RuntimeError) as info:
        _create(_settings("sandlock"))

    assert "unusable" in str(info.value)
    assert "sandlock_create_with_err is missing" in str(info.value)
    assert "no sandbox confinement" in str(info.value)


def test_auto_mode_logs_an_error_before_falling_back(monkeypatch, caplog) -> None:
    """Auto mode may still use the local executor, but never quietly."""
    broken = ImportError("cannot import name 'InstanceClosedError'")
    # The dev host is macOS; the probe is Linux-only, so pin it here.
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: broken)

    with caplog.at_level(logging.INFO, logger="envd_service.executors.factory"):
        executor = _create(_settings("auto"))

    assert isinstance(executor, LocalExecutor)
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(records) == 1
    assert "installed but unusable" in records[0].message
    assert "NO sandbox confinement" in records[0].message
    assert "InstanceClosedError" in records[0].message
    # The fallback is still announced, and the loud line is not the only one.
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_missing_package_keeps_the_quiet_fallback(monkeypatch, caplog) -> None:
    """A package that is simply absent is the documented auto-mode case."""
    missing = ModuleNotFoundError("No module named 'sandlock'")
    # The dev host is macOS; the probe is Linux-only, so pin it here.
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: missing)

    with caplog.at_level(logging.INFO, logger="envd_service.executors.factory"):
        executor = _create(_settings("auto"))

    assert isinstance(executor, LocalExecutor)
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_explicit_sandlock_mode_still_works_with_a_healthy_package(
    monkeypatch,
) -> None:
    """Guard the guard: a healthy import keeps the previous selection path."""
    # The dev host is macOS; the probe is Linux-only, so pin it here.
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: None)
    monkeypatch.setattr(factory_mod, "_landlock_ok", lambda: True)

    executor = _create(_settings("sandlock"))

    assert type(executor).__name__ == "SandlockExecutor"
