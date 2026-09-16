"""A6/fix-1: the manifests must not ask the worker for SYS_ADMIN.

The privilege moved to the ``quota-agent`` service (it runs ``xfs_quota -x``
server-side). The compose stack no longer declares the low-port window at all
(both workers run per-sandbox netns, where the wildcard-DNS `:53` bind is
covered by the sandbox's own userns); for the k8s pod, which is still
shared-netns, the sysctl has to be **pod-level**: the worker image is ``USER 65534``
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
    # Netns shape: the anchor every worker inherits, and worker-2's own
    # override (kept so a single node can still be reverted). The pairing guard
    # in create_app refuses one switch without the other, so both lines or
    # neither.
    assert "\n      E2B_ENABLE_NET_ISOLATION: ${E2B_ENABLE_NET_ISOLATION:-true}\n" in worker
    assert "\n      E2B_FD_INJECT_CONNECT: ${E2B_FD_INJECT_CONNECT:-true}\n" in worker
    # The directive, not the prose: the comment above the line names the old
    # value on purpose.
    assert "\n      - seccomp=unconfined\n" not in worker
    # Since both workers run per-sandbox netns (2026-09-16) the container-level
    # low-port window is gone: the wildcard-DNS `:53` bind happens inside the
    # sandbox's own netns, where root-in-userns covers port 53 (fork:
    # crates/sandlock-core/src/context.rs). The k8s manifest keeps its pod-level
    # copy -- that shape is still shared-netns -- and is asserted below.
    assert "\n    sysctls:\n" not in worker
    # The directive, not the prose: the comment above explains why it is gone.
    assert "\n      - net.ipv4.ip_unprivileged_port_start=0\n" not in worker


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


# ----------------------------------------------------------------------------
# seccomp profile installation (deploy/k8s/seccomp-installer.yaml)
# ----------------------------------------------------------------------------

SECCOMP_INSTALLER = (REPO / "deploy" / "k8s" / "seccomp-installer.yaml").read_text(
    encoding="utf-8"
)
WORKER_SECCOMP_TEXT = (REPO / "deploy" / "seccomp" / "sandlock-worker.json").read_text(
    encoding="utf-8"
)


def _installer_configmap_payload() -> str:
    """The profile exactly as the ConfigMap carries it (block scalar dedented)."""
    marker = "  sandlock-worker.json: |-\n"
    assert marker in SECCOMP_INSTALLER, "ConfigMap must carry the profile as a block scalar"
    body = SECCOMP_INSTALLER.split(marker, 1)[1].split("\n---\napiVersion: apps/v1", 1)[0]
    lines = []
    for line in body.split("\n"):
        if line.startswith("    "):
            lines.append(line[4:])
        else:
            lines.append(line)
    return "\n".join(lines)


def test_seccomp_installer_ships_the_exact_shipped_profile() -> None:
    """The ConfigMap payload is the file the deployment verifies against.

    Byte-exact, not "parses to the same JSON": the node's file is what the
    runtime applies, and a silent reformat would make the two drift apart from
    the artifact the tests and the docs describe.
    """
    payload = _installer_configmap_payload()
    assert payload == WORKER_SECCOMP_TEXT
    assert json.loads(payload) == json.loads(WORKER_SECCOMP_TEXT)


def test_seccomp_installer_rolls_when_the_profile_changes() -> None:
    """`checksum/profile` is the sha256 of the payload.

    The annotation sits on the pod template, so a profile edit that updates it
    rolls the DaemonSet immediately; the test fails until the two agree, which
    makes "I edited the profile but forgot the annotation" a build error rather
    than a node that keeps the old filter.
    """
    import hashlib

    digest = hashlib.sha256(WORKER_SECCOMP_TEXT.encode()).hexdigest()
    assert f'        checksum/profile: "{digest}"\n' in SECCOMP_INSTALLER


def test_seccomp_installer_writes_the_kubelet_seccomp_root() -> None:
    """It targets the kubelet's own seccomp root, atomically."""
    assert "            path: /var/lib/kubelet/seccomp\n" in SECCOMP_INSTALLER
    assert "            type: DirectoryOrCreate\n" in SECCOMP_INSTALLER
    # Atomic replace: write a temp file in the same directory, then rename.
    assert 'tmp="$root/.sandlock-worker.json.$$"' in SECCOMP_INSTALLER
    assert 'mv "$tmp" "$dst"' in SECCOMP_INSTALLER
    # The ConfigMap is mounted read-only and never written back to.
    assert "              mountPath: /config\n              readOnly: true\n" in SECCOMP_INSTALLER
    # kubelet reads the file per container creation, so no restart is involved;
    # the 5-minute re-check is the backstop for a missed rollout.
    assert "sleep 300" in SECCOMP_INSTALLER


def test_seccomp_installer_asks_for_the_minimum() -> None:
    """root to write the node path, and nothing else."""
    assert "privileged: true" not in SECCOMP_INSTALLER
    assert "hostNetwork: true" not in SECCOMP_INSTALLER
    assert "hostPID: true" not in SECCOMP_INSTALLER
    assert (
        "            runAsUser: 0\n"
        "            runAsGroup: 0\n"
        "            allowPrivilegeEscalation: false\n"
        "            readOnlyRootFilesystem: true\n"
        "            capabilities:\n"
        "              drop:\n"
        "                - ALL\n"
        in SECCOMP_INSTALLER
    )
    # Every node, tainted control-plane nodes included -- a node without the
    # profile cannot run the worker pod at all.
    assert "      tolerations:\n        - operator: Exists\n" in SECCOMP_INSTALLER


def test_worker_manifest_points_at_the_installer() -> None:
    """The Deployment keeps Localhost and names the component that installs it."""
    assert "            seccompProfile:\n              type: Localhost\n" in K8S_WORKER
    assert "              localhostProfile: sandlock-worker.json\n" in K8S_WORKER
    assert "seccomp-installer.yaml" in K8S_WORKER
