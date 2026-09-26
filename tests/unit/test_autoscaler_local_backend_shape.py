"""The local Docker pool *declares* the fleet's net-isolation shape.

`autoscaler/backends/local.py` ran every pooled worker behind
`--sysctl net.ipv4.ip_unprivileged_port_start=0` while
`E2B_ENABLE_NET_ISOLATION` stayed off -- the shared-netns + non-root shape the
fleet left on 2026-09-16 (`deploy/stack/docker-compose.prod.yml:209-210`). The
k8s backend only scales replicas of the StatefulSet (`autoscaler/backends/k8s.py:94-101`),
so this file is the *only* pool that needs the pair. `E2B_AS_WORKER_ENV` is
carried in the compose file too, so an operator sees the shape without reading
Python.

Declared, and gated. The pair is read by the *sandlock* executor only
(`envd_service/executors/factory.py:210`), so what makes it take effect is the
pool's own `E2B_EXECUTOR`: `auto` since N38 (2026-09-26), and an operator who
sets it back to `local` gets the worker's shared netns with the pair inert.
That rollback is not free -- the wildcard-DNS `:53` bind needs the low-port
window there and the pool no longer declares one
(`docs/superpowers/plans/2026-09-26-decisions.md`).

Text assertions for the two declarations, plus a stubbed-`_run` argv probe for
the one thing text cannot pin: which value a spawned worker actually carries
after the override seam. Running `docker` is the only thing the module does,
and the repo pins manifests by their text
(`tests/unit/test_worker_manifest_permissions.py`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autoscaler.backends import local as local_backend

REPO = Path(__file__).resolve().parent.parent.parent
LOCAL_BACKEND = (REPO / "autoscaler" / "backends" / "local.py").read_text(
    encoding="utf-8"
)
AUTOSCALE_COMPOSE = (
    REPO / "deploy" / "compose" / "docker-compose.autoscale.yml"
).read_text(encoding="utf-8")

NET_ISOLATION_KEY = '\n            "E2B_ENABLE_NET_ISOLATION": "true",\n'
OVERRIDE_SEAM = "\n            **dict(worker_env or {}),\n"


def test_local_pool_no_longer_declares_a_low_port_window() -> None:
    assert "net.ipv4.ip_unprivileged_port_start" not in LOCAL_BACKEND
    assert '"--sysctl"' not in LOCAL_BACKEND


def test_local_pool_spawns_the_paired_net_isolation_switches() -> None:
    assert NET_ISOLATION_KEY in LOCAL_BACKEND
    assert '\n            "E2B_FD_INJECT_CONNECT": "true",\n' in LOCAL_BACKEND
    # Still `seccomp=unconfined` + E2B_REQUIRE_SECCOMP_FILTER=0: the pool is the
    # permissive local shape on purpose. The pair above is what makes the
    # sandbox (not the worker) the one that binds :53 -- in the sandlock
    # executor, i.e. while `E2B_EXECUTOR` is `auto` or `sandlock`; `local`
    # ignores it (N38).
    assert '\n                "seccomp=unconfined",\n' in LOCAL_BACKEND
    assert '\n                "E2B_REQUIRE_SECCOMP_FILTER=0",\n' in LOCAL_BACKEND


def test_local_pool_declares_the_pair_above_the_override_seam() -> None:
    """Order, not membership: the pair must sit before the override seam.

    `**dict(worker_env or {})` is the single place an operator's
    `E2B_AS_WORKER_ENV` enters the dictionary, i.e. the rollback lever of N38
    (`"E2B_ENABLE_NET_ISOLATION": "false"`). Move the pair below the seam and
    the worker still gets `E2B_ENABLE_NET_ISOLATION=true` -- the membership
    assertion above stays green while the lever is quietly gone.
    """
    assert LOCAL_BACKEND.index(NET_ISOLATION_KEY) < LOCAL_BACKEND.index(OVERRIDE_SEAM)


def _worker_env() -> dict[str, str]:
    """The JSON the autoscaler hands each spawned worker (`:116`)."""
    lines = [
        line
        for line in AUTOSCALE_COMPOSE.splitlines()
        if line.strip().startswith("E2B_AS_WORKER_ENV:")
    ]
    assert len(lines) == 1
    payload = lines[0].split(":", 1)[1].strip()
    # The compose value is single-quoted JSON precisely so `${...}` inside it
    # reaches the worker unexpanded.
    assert payload[0] == "'"
    assert payload[-1] == "'"
    return json.loads(payload[1:-1])


def test_autoscale_compose_worker_env_line_declares_the_gated_shape() -> None:
    """Anchored to the `E2B_AS_WORKER_ENV` line, not to "somewhere in the file".

    A bare `in` over the whole file passes even if the pair drifted into a
    service that never reads it. The keys are one JSON object on one line, so
    parse that line and compare values.
    """
    env = _worker_env()
    assert env["E2B_EXECUTOR"] == "${E2B_EXECUTOR:-auto}"
    assert env["E2B_ENABLE_NET_ISOLATION"] == "true"
    assert env["E2B_FD_INJECT_CONNECT"] == "true"


def _env_from_argv(argv: list[str]) -> dict[str, str]:
    """The `-e KEY=VALUE` pairs docker would hand the container."""
    env: dict[str, str] = {}
    for index, token in enumerate(argv):
        if token != "-e":
            continue
        key, value = argv[index + 1].split("=", 1)
        env[key] = value
    return env


def _spawned_argv(monkeypatch, **kwargs) -> list[str]:
    """Run one `scale_to(1)` against a stubbed `docker` and return its argv."""
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        # `scale_to` asks `current()` first and the empty answer means "spawn
        # one", which is the only `docker run` in the recorded calls.
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(local_backend, "_run", fake_run)
    backend = local_backend.DockerPoolBackend(
        image="registry.test/e2b-sandlock-worker:test",
        network="sandlock",
        workspace_volume="sandbox-shared",
        control_plane_url="http://control-plane:3000",
        internal_api_key="internal-key",
        **kwargs,
    )
    backend.scale_to(1)
    runs = [call for call in calls if call and call[0] == "run"]
    assert len(runs) == 1
    return list(runs[0])


def test_spawned_worker_argv_carries_the_pair_without_a_low_port_window(monkeypatch) -> None:
    """The direction the text cannot see: what the container is actually run with."""
    argv = _spawned_argv(monkeypatch)
    assert "--sysctl" not in argv
    env = _env_from_argv(argv)
    assert env["E2B_ENABLE_NET_ISOLATION"] == "true"
    assert env["E2B_FD_INJECT_CONNECT"] == "true"
    assert env["E2B_REQUIRE_SECCOMP_FILTER"] == "0"


def test_spawned_worker_argv_lets_the_operator_override_the_pair(monkeypatch) -> None:
    """N38's rollback lever, end to end through the spawn argv."""
    argv = _spawned_argv(
        monkeypatch,
        worker_env={
            "E2B_ENABLE_NET_ISOLATION": "false",
            "E2B_FD_INJECT_CONNECT": "false",
        },
    )
    env = _env_from_argv(argv)
    assert env["E2B_ENABLE_NET_ISOLATION"] == "false"
    assert env["E2B_FD_INJECT_CONNECT"] == "false"
    assert [token for token in argv if token.startswith("E2B_ENABLE_NET_ISOLATION=")] == [
        "E2B_ENABLE_NET_ISOLATION=false"
    ]
