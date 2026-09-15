"""A6/fix-1: the manifests must not ask the worker for SYS_ADMIN.

The privilege moved to the ``quota-agent`` service (it runs ``xfs_quota -x``
server-side) and the low-port window is declared in the container spec. For the
k8s pod the sysctl has to be **pod-level**: the worker image is ``USER 65534``
and the manifest does not override it, so ``NET_BIND_SERVICE`` is inert
(containerd grants no ambient caps to a non-root process) and the wildcard-DNS
gateway's ``:53`` bind would fail with the kernel-default
``ip_unprivileged_port_start=1024``.

Track F (Task F1) adds the other half: a non-root worker gets per-sandbox host
uids and route-B slots from the two file-capability brokers, and the kernel
refuses to exec them unless their capabilities are inside the container's
*bounding* set. So the manifests now declare exactly those four caps (SETUID,
SETGID, CHOWN, DAC_OVERRIDE) -- ``capabilities.add`` grants a non-root process
nothing effective, so this is a BND declaration, not a privilege grant, and it
must never be paired with ``no-new-privileges`` (NNP=1 disables file caps).

Text assertions rather than a YAML parse: the repo does not depend on PyYAML.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
STACK_COMPOSE = (REPO / "deploy" / "stack" / "docker-compose.prod.yml").read_text(
    encoding="utf-8"
)
K8S_WORKER = (REPO / "deploy" / "k8s" / "worker.yaml").read_text(encoding="utf-8")
WORKER_SECCOMP = json.loads(
    (REPO / "deploy" / "seccomp" / "sandlock-worker.json").read_text(encoding="utf-8")
)

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
    # No SYS_ADMIN (A6), and only the four file-capability-broker caps (Track
    # F): the BND has to contain them for the brokers' file xattrs to survive
    # exec, while a non-root worker's own CapEff stays 0.
    assert "\n      - SYS_ADMIN\n" not in worker
    assert (
        "\n    cap_drop:\n"
        "      - ALL\n"
        "    cap_add:\n"
        "      - SETUID      # e2b-slot-spawn: setuid(X) on the pooled host uid\n"
        "      - SETGID      # e2b-slot-spawn: setgroups([]) + setgid(X)\n"
        "      - CHOWN       # e2b-maint: workspace/slice ownership + slot documents\n"
        "      - DAC_OVERRIDE  # e2b-maint: rm/walk fallback for trees group access misses\n"
        in worker
    )
    # Negative form: no NNP directive can be added to the security_opt list.
    assert "\n      - no-new-privileges" not in worker
    # The worker's syscall filter is the shipped profile, not `unconfined`
    # (2026-09-15): it is Docker's default profile plus exactly the two syscalls
    # the sandbox-create path needs. The path is env-overridable because a host
    # that keeps only this compose file has no `../seccomp/`.
    assert "\n      - seccomp=${E2B_SECCOMP_PROFILE:-../seccomp/sandlock-worker.json}\n" in worker
    # The directive, not the prose: the comment above the line names the old
    # value on purpose.
    assert "\n      - seccomp=unconfined\n" not in worker
    assert "\n    sysctls:\n" in worker
    assert "\n      - net.ipv4.ip_unprivileged_port_start=0\n" in worker


def _unconditional_allowlist() -> set[str]:
    entries = [
        entry
        for entry in WORKER_SECCOMP["syscalls"]
        if entry["action"] == "SCMP_ACT_ALLOW"
        and not entry.get("includes")
        and not entry.get("args")
    ]
    assert len(entries) == 1, "expected one unconditional allowlist entry"
    return set(entries[0]["names"])


def test_worker_seccomp_profile_is_the_default_plus_two_syscalls() -> None:
    """The profile may only relax `pidfd_getfd` and `unshare` off the default.

    Anything else here is a syscall surface the worker did not have before
    2026-09-15, so it must be a conscious edit to this test as well.
    """
    assert WORKER_SECCOMP["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = _unconditional_allowlist()
    assert {"pidfd_getfd", "unshare"} <= allowed
    # Everything that was capability-gated in the upstream default profile must
    # stay gated: a deployment that *does* carry the capability keeps the access
    # the kernel would grant it, and one that does not stays denied.
    gated = {
        name
        for entry in WORKER_SECCOMP["syscalls"]
        if entry.get("includes", {}).get("caps")
        for name in entry["names"]
    }
    assert not ({"pidfd_getfd", "unshare"} & gated)
    for still_gated in ("mount", "setns", "bpf", "open_tree", "perf_event_open"):
        assert still_gated in gated


def test_k8s_worker_uses_a_localhost_seccomp_profile() -> None:
    """`Unconfined` violates Pod Security baseline; `Localhost` does not."""
    assert "            seccompProfile:\n              type: Localhost\n" in K8S_WORKER
    assert "localhostProfile: sandlock-worker.json\n" in K8S_WORKER
    assert "type: Unconfined" not in K8S_WORKER


def test_stack_quota_agent_owns_the_capability_behind_a_profile() -> None:
    agent = STACK_COMPOSE.split("\n  quota-agent:", 1)[1]
    assert '    profiles: ["quota"]\n' in agent
    assert "    cap_add:\n      - SYS_ADMIN\n" in agent
    assert "    image: ${QUOTA_AGENT_IMAGE:-e2b-sandlock-quota-agent:latest}\n" in agent


def test_k8s_worker_drops_sys_admin_and_declares_the_broker_caps() -> None:
    assert 'add: ["SYS_ADMIN"' not in K8S_WORKER
    assert "\n                - SYS_ADMIN\n" not in K8S_WORKER
    assert (
        "              add:\n"
        "                - NET_BIND_SERVICE\n"
        "                - SETUID\n"
        "                - SETGID\n"
        "                - CHOWN\n"
        "                - DAC_OVERRIDE\n"
        in K8S_WORKER
    )
    # NNP=1 (either spelling) makes the kernel ignore file capabilities, which
    # would silently turn the brokers back into unprivileged binaries.
    assert "allowPrivilegeEscalation: false" not in K8S_WORKER
    assert "no-new-privileges" not in K8S_WORKER


def test_k8s_low_port_window_is_pod_level_not_container_level() -> None:
    pod_spec = K8S_WORKER.split("\n    spec:\n", 1)[1]
    pod_part, containers_part = pod_spec.split("\n      containers:\n", 1)
    assert POD_SYSCTL in pod_part
    assert "sysctls:" not in containers_part
