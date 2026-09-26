"""The local Docker pool spawns workers in the fleet's net-isolation shape.

`autoscaler/backends/local.py` ran every pooled worker behind
`--sysctl net.ipv4.ip_unprivileged_port_start=0` while
`E2B_ENABLE_NET_ISOLATION` stayed off -- the shared-netns + non-root shape the
fleet left on 2026-09-16 (`deploy/stack/docker-compose.prod.yml:209-210`). The
k8s backend only scales replicas of the StatefulSet (`autoscaler/backends/k8s.py:94-101`),
so this file is the *only* pool that needs the pair. `E2B_AS_WORKER_ENV` is
carried in the compose file too, so an operator sees the shape without reading
Python.

Text assertions rather than importing the backend: running `docker` is the
only thing this module does, and the repo pins manifests by their text
(`tests/unit/test_worker_manifest_permissions.py`).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LOCAL_BACKEND = (REPO / "autoscaler" / "backends" / "local.py").read_text(
    encoding="utf-8"
)
AUTOSCALE_COMPOSE = (
    REPO / "deploy" / "compose" / "docker-compose.autoscale.yml"
).read_text(encoding="utf-8")


def test_local_pool_no_longer_declares_a_low_port_window() -> None:
    assert "net.ipv4.ip_unprivileged_port_start" not in LOCAL_BACKEND
    assert '"--sysctl"' not in LOCAL_BACKEND


def test_local_pool_spawns_the_paired_net_isolation_switches() -> None:
    assert '\n            "E2B_ENABLE_NET_ISOLATION": "true",\n' in LOCAL_BACKEND
    assert '\n            "E2B_FD_INJECT_CONNECT": "true",\n' in LOCAL_BACKEND
    # Still `seccomp=unconfined` + E2B_REQUIRE_SECCOMP_FILTER=0: the pool is the
    # permissive local shape on purpose, and the pair above is what makes the
    # sandbox (not the worker) the one that binds :53.
    assert '\n                "seccomp=unconfined",\n' in LOCAL_BACKEND
    assert '\n                "E2B_REQUIRE_SECCOMP_FILTER=0",\n' in LOCAL_BACKEND


def test_autoscale_compose_carries_the_same_pair() -> None:
    assert '"E2B_ENABLE_NET_ISOLATION": "true"' in AUTOSCALE_COMPOSE
    assert '"E2B_FD_INJECT_CONNECT": "true"' in AUTOSCALE_COMPOSE
