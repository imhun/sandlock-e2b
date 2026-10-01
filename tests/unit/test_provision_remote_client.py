"""The create path's control-plane → worker POST reuses one connection.

``_provision_remote`` used to build an ``httpx.AsyncClient`` per create, which
is a fresh TCP connection plus a DNS lookup of the worker's address *inside*
the create's critical path (measured 2026-10-01: the control plane's own share
of a create is ~30 ms, and this was part of it). The app now owns one
keep-alive client (``app.state.remote_http``, built in the lifespan) and the
provisioning call goes through it.

Two shapes have to keep working, and this lane pins both: the deployed one
(shared client present -- no client is constructed per call) and the embedder
one (no ``remote_http`` on the app, e.g. a test-built app or an older caller --
still works, with its own client, and hangs up when it is done).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from control_plane.api import sandboxes

WORKER_URL = "http://10.244.140.31:49983"


def _record() -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_id="sbx_0123456789abcdef",
        envd_access_token="tok_0123456789abcdef",
        env_vars={"A": "1"},
        base_image="registry.example/python:3.11-slim",
        memory_mb=512,
        cpu_count=1,
        disk_size_mb=1024,
        max_processes=100,
        host_uid=10000,
        allow_internet_access=False,
        network=None,
        mcp=None,
        iam_tokens=None,
        workspace_dir="/tmp/whatever",
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(internal_api_key="internal-key", max_command_timeout=300)


def _node() -> SimpleNamespace:
    return SimpleNamespace(node_id="e2b-worker-0", address=WORKER_URL)


def _request(**state) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _dialed_with(monkeypatch, handler) -> list:
    """Every client ``_provision_remote`` builds itself, pointed at ``handler``.

    A counting factory rather than a stub: the class it returns is the real
    ``httpx.AsyncClient`` (so ``is_closed`` means what it says), and the
    transport is replaced only so the test never leaves the process.
    """
    made: list = []
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        client = real_client(*args, **kwargs)
        made.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return made


def test_the_provisioning_post_travels_on_the_apps_shared_client(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={})

    # Built before the counter is installed: the shared client is not the
    # thing under test, the per-call ones are.
    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    made = _dialed_with(monkeypatch, handler)

    asyncio.run(
        sandboxes._provision_remote(
            _request(remote_http=shared, volumes={}),
            _record(),
            _node(),
            _settings(),
            None,
            [],
        )
    )

    assert made == [], (
        "a create built its own AsyncClient although the app has a shared one"
    )
    assert len(seen) == 1
    assert seen[0].url.path == "/agent/sandboxes"
    assert str(seen[0].url.host) == "10.244.140.31"
    assert seen[0].headers["X-Internal-Key"] == "internal-key"
    asyncio.run(shared.aclose())


def test_without_a_shared_client_the_call_dials_its_own_and_hangs_up(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={})

    made = _dialed_with(monkeypatch, handler)

    asyncio.run(
        sandboxes._provision_remote(
            _request(volumes={}),
            _record(),
            _node(),
            _settings(),
            None,
            [],
        )
    )

    assert len(made) == 1
    assert made[0].is_closed, (
        "the per-call client was left open: a create must not leak a socket"
    )
