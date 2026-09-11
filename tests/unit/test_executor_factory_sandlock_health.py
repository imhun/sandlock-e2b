"""B1 review: a *broken* sandlock install must never look like a missing one.

``_sandlock_available()`` used to answer ``False`` for every failure, so a
wheel built against a different ``libsandlock_ffi.so`` (or a partially
upgraded image) degraded to :class:`LocalExecutor` -- a sandbox with **no
confinement**. These tests pin the split:

* ``ModuleNotFoundError`` (not installed) keeps the documented fallback to
  :class:`LocalExecutor`; and
* an installed-but-unusable package fails the sandbox creation for **both**
  ``sandlock`` and ``auto`` (B1 fix round 2: auto is not allowed to run
  unconfined either), while ``local`` -- the operator's explicit choice -- is
  left untouched and never even probes.
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


def _expected_unusable(mode: str, detail: str) -> str:
    """The exact fail-closed text, written out rather than imported."""
    return (
        f"E2B_EXECUTOR={mode} cannot run: the sandlock package is installed but "
        f"unusable ({detail}); refusing to fall back to the LOCAL executor, "
        "which applies no sandbox confinement. Reinstall the matching sandlock "
        "wheel (or rebuild libsandlock_ffi.so) and restage the worker image."
    )


@pytest.mark.parametrize("mode", ["sandlock", "auto"])
def test_broken_package_refuses_for_every_sandlock_capable_mode(
    monkeypatch, mode
) -> None:
    """``sandlock`` **and** ``auto`` fail closed on an unusable install.

    ``auto`` may fall back only when the package is *absent*: the fallback is
    LocalExecutor, which applies no confinement at all (B1 fix round 2).
    """
    broken = ImportError(
        "sandlock_create_with_err is missing from the loaded sandlock library"
    )
    # The dev host is macOS; the probe is Linux-only, so pin it here.
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: broken)

    with pytest.raises(RuntimeError) as info:
        _create(_settings(mode))

    assert type(info.value) is RuntimeError
    assert str(info.value) == _expected_unusable(
        mode,
        "ImportError: sandlock_create_with_err is missing from the loaded "
        "sandlock library",
    )


def test_missing_package_keeps_the_quiet_fallback(monkeypatch, caplog) -> None:
    """A package that is simply absent is the documented auto-mode fallback."""
    missing = ModuleNotFoundError("No module named 'sandlock'")
    # The dev host is macOS; the probe is Linux-only, so pin it here.
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: missing)
    monkeypatch.setattr(factory_mod, "_landlock_ok", lambda: True)

    with caplog.at_level(logging.INFO, logger="envd_service.executors.factory"):
        executor = _create(_settings("auto"))

    assert isinstance(executor, LocalExecutor)
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_explicit_local_mode_never_probes_the_package(monkeypatch, caplog) -> None:
    """``E2B_EXECUTOR=local`` is the operator's choice: no probe, no error.

    A broken install must not make the explicitly-selected local executor
    noisy or fatal -- but it must not be *chosen* by auto either, which the
    case above pins.
    """
    probes: list[int] = []

    def _probe():  # noqa: ANN202 - records the call
        probes.append(1)
        return ImportError("broken")

    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", _probe)

    with caplog.at_level(logging.INFO, logger="envd_service.executors.factory"):
        executor = _create(_settings("local"))

    assert isinstance(executor, LocalExecutor)
    assert probes == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_auto_mode_still_selects_sandlock_with_a_healthy_package(
    monkeypatch,
) -> None:
    """Guard the guard: a healthy install keeps the auto-mode fast path."""
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: None)
    monkeypatch.setattr(factory_mod, "_landlock_ok", lambda: True)

    executor = _create(_settings("auto"))

    assert type(executor).__name__ == "SandlockExecutor"


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
