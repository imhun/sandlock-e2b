"""E7.2: worker-level net-isolation configuration (Settings env parsing and
executor factory passthrough). Defaults stay off for zero regression."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

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


def test_create_executor_passes_net_isolation_flags(monkeypatch) -> None:
    """The factory forwards the worker switches into SandlockExecutor
    construction."""
    import envd_service.executors.factory as factory_mod

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
