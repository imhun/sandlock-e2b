"""A6/fix-1: the manifests must not ask the worker for SYS_ADMIN.

The privilege moved to the ``quota-agent`` service (it runs ``xfs_quota -x``
server-side) and the low-port window is declared in the container spec. For the
k8s pod the sysctl has to be **pod-level**: the worker image is ``USER 65534``
and the manifest does not override it, so ``NET_BIND_SERVICE`` is inert
(containerd grants no ambient caps to a non-root process) and the wildcard-DNS
gateway's ``:53`` bind would fail with the kernel-default
``ip_unprivileged_port_start=1024``.

Text assertions rather than a YAML parse: the repo does not depend on PyYAML.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
STACK_COMPOSE = (REPO / "deploy" / "stack" / "docker-compose.prod.yml").read_text(
    encoding="utf-8"
)
K8S_WORKER = (REPO / "deploy" / "k8s" / "worker.yaml").read_text(encoding="utf-8")

POD_SYSCTL = (
    "      securityContext:\n"
    "        sysctls:\n"
    "          - name: net.ipv4.ip_unprivileged_port_start\n"
    '            value: "0"\n'
)


def test_stack_worker_has_no_cap_add_and_declares_the_low_port_window() -> None:
    worker = STACK_COMPOSE.split("\n  worker-1: &worker", 1)[1].split(
        "\n  worker-2:", 1
    )[0]
    assert "cap_add:" not in worker
    assert "\n    sysctls:\n" in worker
    assert "\n      - net.ipv4.ip_unprivileged_port_start=0\n" in worker


def test_stack_quota_agent_owns_the_capability_behind_a_profile() -> None:
    agent = STACK_COMPOSE.split("\n  quota-agent:", 1)[1]
    assert '    profiles: ["quota"]\n' in agent
    assert "    cap_add:\n      - SYS_ADMIN\n" in agent
    assert "    image: ${QUOTA_AGENT_IMAGE:-e2b-sandlock-quota-agent:latest}\n" in agent


def test_k8s_worker_drops_sys_admin_and_keeps_net_bind_service() -> None:
    assert 'add: ["SYS_ADMIN"' not in K8S_WORKER
    assert 'add: ["NET_BIND_SERVICE"]\n' in K8S_WORKER


def test_k8s_low_port_window_is_pod_level_not_container_level() -> None:
    pod_spec = K8S_WORKER.split("\n    spec:\n", 1)[1]
    pod_part, containers_part = pod_spec.split("\n      containers:\n", 1)
    assert POD_SYSCTL in pod_part
    assert "sysctls:" not in containers_part
