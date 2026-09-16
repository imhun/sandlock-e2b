"""E7.2: worker-level net-isolation configuration (Settings env parsing and
executor factory passthrough). Defaults stay off for zero regression."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from envd_service.config import Settings
from envd_service.executors.factory import create_executor


def test_net_isolation_settings_default_off(monkeypatch) -> None:
    monkeypatch.delenv("E2B_ENABLE_NET_ISOLATION", raising=False)
    monkeypatch.delenv("E2B_FD_INJECT_CONNECT", raising=False)
    monkeypatch.delenv("E2B_PORT_MAPPINGS", raising=False)
    settings = Settings(executor="local")
    assert settings.enable_net_isolation is False
    assert settings.fd_inject_connect is False
    assert settings.port_mappings == {}


def test_net_isolation_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("E2B_ENABLE_NET_ISOLATION", "true")
    monkeypatch.setenv("E2B_FD_INJECT_CONNECT", "1")
    monkeypatch.setenv("E2B_PORT_MAPPINGS", '{"50006": "8080"}')
    settings = Settings(executor="local")
    assert settings.enable_net_isolation is True
    assert settings.fd_inject_connect is True
    assert settings.port_mappings == {"50006": "8080"}


def test_net_isolation_pairing_guard(monkeypatch) -> None:
    """`net_isolation` without `fd_inject_connect` refuses to start (2026-09-16).

    That shape gives every sandbox a loopback-only netns: outbound connects
    fail inside the kernel and user code only sees timeouts, with nothing in
    the worker log. The guard names both switches; the intentional no-egress
    shape has to say so with the ack variable.
    """
    from envd_service.config import (
        NET_ISOLATION_PAIRING_ERROR,
        check_net_isolation_pairing,
    )

    def _settings(netns, inject, ack):
        return SimpleNamespace(
            enable_net_isolation=netns, fd_inject_connect=inject, allow_loopback_only=ack
        )

    # The refused shape, and its exact message.
    with pytest.raises(RuntimeError) as excinfo:
        check_net_isolation_pairing(_settings(True, False, False))
    assert str(excinfo.value) == NET_ISOLATION_PAIRING_ERROR

    # Every other combination is fine: off, paired, or explicitly acknowledged.
    assert check_net_isolation_pairing(_settings(False, False, False)) is None
    assert check_net_isolation_pairing(_settings(False, True, False)) is None
    assert check_net_isolation_pairing(_settings(True, True, False)) is None
    assert check_net_isolation_pairing(_settings(True, False, True)) is None

    monkeypatch.delenv("E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY", raising=False)
    assert Settings(executor="local").allow_loopback_only is False
    monkeypatch.setenv("E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY", "1")
    assert Settings(executor="local").allow_loopback_only is True


def test_create_app_refuses_the_unpaired_switch() -> None:
    """The guard runs before anything else in create_app, so a misconfigured
    worker crash-loops with the reason instead of serving sandboxes whose
    network is silently dead."""
    from envd_service.app import create_app
    from envd_service.config import NET_ISOLATION_PAIRING_ERROR

    settings = SimpleNamespace(
        enable_net_isolation=True, fd_inject_connect=False, allow_loopback_only=False
    )
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings=settings)
    assert str(excinfo.value) == NET_ISOLATION_PAIRING_ERROR



def test_create_executor_passes_net_isolation_flags(monkeypatch) -> None:
    """The factory forwards the worker switches into SandlockExecutor
    construction."""
    import envd_service.executors.factory as factory_mod

    # Stub the environment probes this case is not about: the point here is the
    # flag passthrough (B1 fix round 3 probes the real import, so a dev host
    # without sandlock would otherwise fail the explicit-sandlock branch).
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: None)
    monkeypatch.setattr(factory_mod, "_sandlock_available", lambda: True)
    monkeypatch.setattr(factory_mod, "_landlock_ok", lambda: True)
    settings = SimpleNamespace(
        executor="sandlock",
        enable_network=True,
        enable_netns=False,
        enable_net_isolation=True,
        fd_inject_connect=True,
        port_mappings={"50006": "8080"},
        network_deny_cidrs=(),
        sandbox_notify_rate_limit=0,
        iam_signing_key="k",
        image_cache_dir=Path("tmp/cache"),
    )
    executor = create_executor(
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
    assert executor._enable_net_isolation is True
    assert executor._fd_inject_connect is True
    assert executor._port_mappings == {50006: 8080}
