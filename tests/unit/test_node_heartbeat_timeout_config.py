"""``E2B_NODE_HEARTBEAT_TIMEOUT``: the node-liveness window is configurable.

It used to be a hard-coded 15s on ``NodeRegistry``. That is only survivable when
every node's image rootfs is on local disk: the worker resolves a fresh rootfs on
its *event loop*, and on a network filesystem that unpack is minutes, not
milliseconds (measured on Aliyun NAS 2026-09-17: 61s for a python-slim rootfs vs
0.26s on the local overlay -- the same 2111 files). While that runs the worker
cannot heartbeat, so a healthy node is marked unhealthy and E6.1 reaps its live
sandboxes. The default must stay 15s so no existing deployment changes behavior;
the deployments that need a wider window set the env var.
"""

from __future__ import annotations

from control_plane.config import Settings


def test_default_heartbeat_timeout_is_unchanged(monkeypatch) -> None:
    monkeypatch.delenv("E2B_NODE_HEARTBEAT_TIMEOUT", raising=False)
    assert Settings().node_heartbeat_timeout_s == 15.0


def test_heartbeat_timeout_reads_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("E2B_NODE_HEARTBEAT_TIMEOUT", "300")
    assert Settings().node_heartbeat_timeout_s == 300.0


def test_heartbeat_timeout_reaches_the_node_registry(monkeypatch) -> None:
    """The setting must be *used*: a knob nobody wires up is the same as absent."""
    from control_plane import app as app_module

    monkeypatch.setenv("E2B_NODE_HEARTBEAT_TIMEOUT", "123")
    monkeypatch.delenv("E2B_REDIS_URL", raising=False)
    app = app_module.create_app(settings=Settings(api_keys=("local-key",)))
    assert app.state.nodes._heartbeat_timeout == 123.0  # noqa: SLF001 - the point
