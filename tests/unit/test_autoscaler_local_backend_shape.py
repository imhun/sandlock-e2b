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
FLEET_STACK = (REPO / "deploy" / "stack" / "docker-compose.prod.yml").read_text(
    encoding="utf-8"
)
FLEET_K8S = (REPO / "deploy" / "k8s" / "worker.yaml").read_text(encoding="utf-8")

NET_ISOLATION_KEY = '\n            "E2B_ENABLE_NET_ISOLATION": "true",\n'
OVERRIDE_SEAM = "\n            **dict(worker_env or {}),\n"

#: The two keys the pool was missing, and the fleet manifest each value comes
#: from: without `E2B_ENABLE_NETWORK` the worker builds an empty network policy
#: (every connect fails, loopback included), and without
#: `E2B_ROUTE_B_TMP_ROOT` a current worker exits 1 before it ever listens (N38
#: connected items 1 and 3; N39).
FLEET_KEYS = ("E2B_ENABLE_NETWORK", "E2B_ROUTE_B_TMP_ROOT")

#: The keys each fleet manifest actually names, pinned: both carry both now.
#: The k8s pod template used to be the odd one out here -- it never named
#: `E2B_ENABLE_NETWORK`, and that silence was N42 (a create request's
#: `allowInternetAccess` accepted and ignored). The ruling of 2026-09-26 turned
#: egress on everywhere and the manifest now carries the flag, so the two
#: fleet files agree again. Both are compared, so a later edit that drops a key
#: from either one fails here instead of leaving "pool == fleet" half-checked.
FLEET_MANIFEST_KEYS = {
    "deploy/stack/docker-compose.prod.yml": FLEET_KEYS,
    "deploy/k8s/worker.yaml": FLEET_KEYS,
}


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
    """The JSON the autoscaler hands each spawned worker (`E2B_AS_WORKER_ENV`)."""
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


def test_the_pool_worker_env_carries_the_two_fleet_keys() -> None:
    """N38 connected items 1 and 3, spelled out with the fleet's own values."""
    env = _worker_env()
    assert env["E2B_ENABLE_NETWORK"] == "true"
    assert env["E2B_ROUTE_B_TMP_ROOT"] == "/var/lib/e2b-sandboxes/.route-b"


def _compose_fleet_values() -> dict[str, str]:
    """The two keys as `deploy/stack/docker-compose.prod.yml` spells them."""
    found: dict[str, str] = {}
    for line in FLEET_STACK.splitlines():
        stripped = line.strip()
        for key in FLEET_KEYS:
            if stripped.startswith(f"{key}:"):
                found[key] = stripped.split(":", 1)[1].strip().strip('"')
    return found


def _k8s_fleet_values() -> dict[str, str]:
    """The same keys as `deploy/k8s/worker.yaml` spells them."""
    found: dict[str, str] = {}
    lines = [line.strip() for line in FLEET_K8S.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("- name: "):
            continue
        key = line[len("- name: ") :].strip()
        if key not in FLEET_KEYS:
            continue
        value_line = lines[index + 1]
        assert value_line.startswith("value: "), value_line
        found[key] = value_line[len("value: ") :].strip().strip('"')
    return found


def _k8s_env(key: str) -> str:
    """One k8s worker env value by name, in the same dialect as above.

    N27 needs a key outside `FLEET_KEYS` here: the route-B root is no longer a
    fleet-wide literal, it is *derived* -- the k8s manifest sinks the tree root
    and puts the platform's own files (`.route-b` among them) under
    `E2B_STATE_BASE`, while the compose stacks this pool spawns keep the
    one-base layout. Reading the base from the same manifest is what keeps the
    assertion a comparison between two declarations instead of a third copy of
    a literal.
    """
    lines = [line.strip() for line in FLEET_K8S.splitlines()]
    marker = f"- name: {key}"
    hits = [index for index, line in enumerate(lines) if line == marker]
    assert len(hits) == 1, f"expected exactly one {key!r} env entry: {hits}"
    value_line = lines[hits[0] + 1]
    assert value_line.startswith("value: "), value_line
    return value_line[len("value: ") :].strip().strip('"')


def _fleet_manifest_values() -> dict[str, dict[str, str]]:
    """Every fleet manifest's values for the two keys, keyed by manifest path.

    Which manifest carries which key is pinned here too (see
    `FLEET_MANIFEST_KEYS`): "pool == fleet" has to hold against *both* fleet
    manifests, not just the compose one.
    """
    manifests = {
        "deploy/stack/docker-compose.prod.yml": _compose_fleet_values(),
        "deploy/k8s/worker.yaml": _k8s_fleet_values(),
    }
    # Sorted, not file order: the two dialects list the keys differently (the
    # k8s env list names the route-B root first, the compose env block the
    # egress flag first) and the order an env list is written in is not a fact
    # this pin is about.
    assert {
        path: tuple(sorted(values)) for path, values in manifests.items()
    } == FLEET_MANIFEST_KEYS
    return manifests


def test_the_pool_worker_env_matches_every_fleet_manifest_for_those_keys() -> None:
    """"Pool == fleet" pinned against the fleet files, not a third copy of them.

    Both fleet manifests are read: `deploy/stack/docker-compose.prod.yml` and
    `deploy/k8s/worker.yaml`. Reading only the first would stay green while the
    two fleet manifests drifted apart from each other.

    N27 (2026-09-26) split one of the two values into two *shapes* -- this pool
    spawns compose-shaped workers (one base), the k8s manifest sinks the tree
    root and moves `.route-b` under `E2B_STATE_BASE` -- so the k8s file is
    compared against its own bases here, and "pool == fleet" is asserted against
    the shape the pool actually spawns. The egress flag is shape-independent and
    still has to match both.
    """
    manifests = _fleet_manifest_values()
    assert manifests["deploy/stack/docker-compose.prod.yml"] == {
        "E2B_ENABLE_NETWORK": "true",
        "E2B_ROUTE_B_TMP_ROOT": "/var/lib/e2b-sandboxes/.route-b",
    }
    assert manifests["deploy/k8s/worker.yaml"] == {
        "E2B_ENABLE_NETWORK": "true",
        "E2B_ROUTE_B_TMP_ROOT": f"{_k8s_env('E2B_STATE_BASE')}/.route-b",
    }
    env = _worker_env()
    pool_shape = manifests["deploy/stack/docker-compose.prod.yml"]
    assert {key: env[key] for key in pool_shape} == pool_shape
    for values in manifests.values():
        assert env["E2B_ENABLE_NETWORK"] == values["E2B_ENABLE_NETWORK"]


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


def test_spawned_worker_argv_carries_the_two_fleet_keys(monkeypatch) -> None:
    """The base dictionary is self-sufficient without any `worker_env`.

    `E2B_AS_WORKER_ENV` is the compose file's entry point, not the only one:
    `DockerPoolBackend()` is also built by hand (this file, and any embedder),
    and a current image exits 1 without `E2B_ROUTE_B_TMP_ROOT` before it ever
    listens (N39). Same reason the netns pair sits in the dictionary (N38).
    """
    argv = _spawned_argv(monkeypatch)
    env = _env_from_argv(argv)
    assert env["E2B_ENABLE_NETWORK"] == "true"
    assert env["E2B_ROUTE_B_TMP_ROOT"] == "/var/lib/e2b-sandboxes/.route-b"
    # The base dictionary is a second place the pool declares these values, so
    # it is pinned against the fleet manifest of the shape it spawns (compose,
    # one base -- see the N27 split above) the same way the compose JSON is; the
    # egress flag is shape-independent and matches both.
    manifests = _fleet_manifest_values()
    pool_shape = manifests["deploy/stack/docker-compose.prod.yml"]
    assert {key: env[key] for key in pool_shape} == pool_shape
    for values in manifests.values():
        assert env["E2B_ENABLE_NETWORK"] == values["E2B_ENABLE_NETWORK"]


def test_spawned_worker_argv_lets_the_operator_override_the_fleet_keys(monkeypatch) -> None:
    """`E2B_AS_WORKER_ENV` stays the override entry point (last writer wins)."""
    argv = _spawned_argv(
        monkeypatch,
        worker_env={
            "E2B_ENABLE_NETWORK": "false",
            "E2B_ROUTE_B_TMP_ROOT": "/elsewhere/.route-b",
        },
    )
    env = _env_from_argv(argv)
    assert env["E2B_ENABLE_NETWORK"] == "false"
    assert env["E2B_ROUTE_B_TMP_ROOT"] == "/elsewhere/.route-b"
    assert [token for token in argv if token.startswith("E2B_ROUTE_B_TMP_ROOT=")] == [
        "E2B_ROUTE_B_TMP_ROOT=/elsewhere/.route-b"
    ]
