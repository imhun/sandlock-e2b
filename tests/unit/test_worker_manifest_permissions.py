"""A6/fix-1: the manifests must not ask the worker for SYS_ADMIN.

The privilege moved to the ``quota-agent`` service (it runs ``xfs_quota -x``
server-side). Neither stack declares the low-port window any more: both run
per-sandbox netns, where the wildcard-DNS ``:53`` bind happens inside the
sandbox's own netns and is covered by the guest's root-in-userns
(``CAP_NET_BIND_SERVICE``). The k8s manifest was the last holder of a pod-level
``net.ipv4.ip_unprivileged_port_start=0`` window, which was needed only while it
shared the pod netns -- and it was a hole unrelated to sandboxes (every process
in the pod could bind low ports). N5/N10 closed on 2026-09-17; the tests below
pin the new shape so neither the window nor a half-switch can come back.

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
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
STACK_COMPOSE = (REPO / "deploy" / "stack" / "docker-compose.prod.yml").read_text(
    encoding="utf-8"
)
K8S_WORKER = (REPO / "deploy" / "k8s" / "worker.yaml").read_text(encoding="utf-8")
K8S_CONTROL_PLANE = (REPO / "deploy" / "k8s" / "control-plane.yaml").read_text(
    encoding="utf-8"
)
BUILDKIT_CONFIG = (REPO / "deploy" / "stack" / "buildkitd.toml").read_text(encoding="utf-8")
K8S_BUILDKIT = (REPO / "deploy" / "k8s" / "buildkit.yaml").read_text(encoding="utf-8")
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
    # pid_ns shape (2026-09-16): on in the anchor every worker inherits (the
    # worker-2 canary graduated to the fleet). The per-node lever lives in
    # worker-2's block and is pinned by its own test below, because this slice
    # stops at the `worker-2:` key. Unlike the netns pair there is no pairing
    # guard to keep honest: pid_ns cannot put a sandbox offline.
    assert "\n      E2B_PID_NS: ${E2B_PID_NS:-true}\n" in worker
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


def test_stack_worker2_carries_the_pid_ns_canary() -> None:
    """Worker-2 keeps a per-node pid_ns lever after the fleet-wide rollout.

    The switch is process-level per worker (`E2B_PID_NS` -> `Settings.pid_ns`),
    so a per-service override is the only way to take one node back to the
    shared pid namespace without touching the fleet -- the same shape the netns
    canary used, now kept as the rollback lever (the canary ran on this node
    first: docs §2.4.10.3). Pinning it keeps a future edit from silently
    dropping the lever, and pins that no `*_WORKER1` variant exists: worker-1
    can only follow the shared anchor.
    """
    worker2 = STACK_COMPOSE.split("\n  worker-2:", 1)[1]
    assert "\n      E2B_PID_NS: ${E2B_PID_NS_WORKER2:-true}\n" in worker2
    assert "E2B_PID_NS_WORKER1" not in STACK_COMPOSE
    # The anchor is the fleet-wide writer, and it is on (asserted by the test
    # above); this lever only ever subtracts.
    assert "\n      E2B_PID_NS: ${E2B_PID_NS:-true}\n" in STACK_COMPOSE


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


def test_no_low_port_window_survives_anywhere() -> None:
    """N5: with per-sandbox netns the window has no user left, in either manifest.

    The compose stack dropped its copy on 2026-09-16; the k8s pod held one until
    2026-09-17. A half-revert (sysctl back without the netns pair, or the pair
    without removing the window) is exactly what these two assertions catch.
    """
    # Structural, not textual: both files still *mention* the sysctl in the
    # comments that explain why it is gone, and those comments are the point.
    assert POD_SYSCTL not in K8S_WORKER
    assert "\n        sysctls:\n" not in K8S_WORKER  # pod-level PodSecurityContext
    assert "\n    sysctls:\n" not in STACK_COMPOSE  # compose service-level


def test_k8s_runs_the_same_namespace_shape_as_the_stack() -> None:
    """N5/N10: the two shipped shapes must not drift apart.

    A per-sandbox netns is only meaningful with the paired fd injection
    (`create_app` refuses `net_isolation` without it, because the single-switch
    shape is a sandbox with no network at all), and a shared pid namespace
    leaves `kill(pid, 0)` as a liveness oracle for the pod's other processes.
    """
    assert 'name: E2B_ENABLE_NET_ISOLATION' in K8S_WORKER
    assert 'name: E2B_FD_INJECT_CONNECT' in K8S_WORKER
    assert 'name: E2B_PID_NS' in K8S_WORKER
    # ...and the stack still runs it, so the assertion above is about parity
    # rather than about k8s alone.
    assert 'E2B_ENABLE_NET_ISOLATION' in STACK_COMPOSE
    assert 'E2B_PID_NS' in STACK_COMPOSE


def test_k8s_stays_single_replica_until_n13_is_closed() -> None:
    """N13: the multi-replica shape is unverified, so nothing may reach it.

    The uid allocator itself is cross-process safe on a shared base
    (`uid_pool.acquire` flocks `<base>/.uid_pool.lock` and recomputes the free
    set from every `sandbox.json` plus the reservation markers), but every
    worker on one shared base also reconciles and GCs a tree set it does not
    own, and that is not established. Until it is, the autoscaler may not raise
    the replica count -- which is what this pins.
    """
    autoscaler = (REPO / "deploy" / "k8s" / "autoscaler.yaml").read_text(
        encoding="utf-8"
    )
    marker = "name: E2B_AS_MAX_REPLICAS"
    assert marker in autoscaler
    # The value sits a few comment lines below the name, so read forward rather
    # than requiring the two to be adjacent.
    following = autoscaler[autoscaler.index(marker):][:600]
    assert 'value: "1"' in following
    assert 'value: "16"' not in following
    assert "\n  replicas: 1\n" in K8S_WORKER


def test_k8s_worker_never_surges_a_second_replica() -> None:

    """`replicas: 1` alone is not enough: a rolling update would surge a second pod.

    Two workers over one shared workspace base is the unverified N13 shape, and
    the repo locks it off by pinning the worker to one replica and the autoscaler
    to MAX=1 -- but the default rolling-update strategy creates a replacement pod
    *before* deleting the old one, so every upgrade would briefly run the exact
    shape those two pins exist to prevent. It also cannot schedule on a 4-core
    node (the worker requests 2 CPU): measured 2026-09-17 on k0s, the rollout
    stalled with `0/2 nodes are available: 1 Insufficient cpu`.
    """
    assert "  strategy:\n    type: RollingUpdate\n    rollingUpdate:\n      maxSurge: 0\n" in K8S_WORKER


def test_k8s_control_plane_stays_single_replica_until_node_registry_is_shared() -> None:
    """Two control-plane replicas disagree about which nodes are healthy.

    The node registry is per-process (`NodeRegistry._nodes` is an in-memory dict;
    Redis carries only the quota ledger and the sandbox records), while a
    worker's registration and heartbeats stick to whichever replica its HTTP
    connection reaches. The replica that misses them ages the node past the
    15-second `heartbeat_timeout` and marks it unhealthy. Measured on k0s on
    2026-09-17: replica A said `fxf2j: unhealthy` while replica B said
    `fxf2j: healthy`, for 12 consecutive samples -- which produced `502 Node ...
    unavailable` route lookups, uneven placement, and `reap_unhealthy` treating
    live sandboxes as orphans (a `404 Sandbox ... not found` in the smoke).
    """
    assert "\n  replicas: 1\n" in K8S_CONTROL_PLANE
    assert "\n  replicas: 2\n" not in K8S_CONTROL_PLANE


def _buildkit_configmap_payload() -> str:
    """The buildkitd config exactly as the ConfigMap carries it (dedented)."""
    marker = "  buildkitd.toml: |-\n"
    assert marker in K8S_BUILDKIT, "buildkit ConfigMap must carry the file as a block scalar"
    body = K8S_BUILDKIT.split(marker, 1)[1]
    lines = []
    for line in body.split("\n"):
        if line.startswith("    "):
            lines.append(line[4:])
        else:
            lines.append(line)
    return "\n".join(lines).rstrip("\n") + "\n"


def test_k8s_buildkit_config_is_the_compose_stack_config() -> None:
    """The k8s builder must use the same config as the compose one.

    Byte-exact, because the part that actually matters is the docker.io mirror
    chain: this deployment host has Docker Hub closed, so a drifted (or missing)
    mirror list turns every template build into a pull failure. Same pattern as
    the seccomp profile's ConfigMap copy.
    """
    assert _buildkit_configmap_payload() == BUILDKIT_CONFIG


def test_k8s_control_plane_is_paired_with_the_buildkit_sidecar() -> None:
    """`Template.build` has no builder without this pair.

    Measured on k0s 2026-09-17: with no buildkit anywhere in the manifest set,
    the smoke's template phase ends in
    `BuildException: buildkit build exited with code 1`. The sidecar shares its
    socket volume with the control plane (an emptyDir is pod-scoped), which is
    why the two are containers of one pod rather than two Deployments.
    """
    assert "E2B_BUILDKIT_ADDR\n              value: unix:///run/buildkit/buildkitd.sock\n" in K8S_CONTROL_PLANE
    assert "\n        - name: buildkit\n" in K8S_CONTROL_PLANE
    assert "      securityContext:\n        fsGroup: 1000\n" in K8S_CONTROL_PLANE
    # The builder keeps the full syscall surface; the *worker* profile is the one
    # that must stay narrowed.
    assert "seccompProfile:\n              type: Unconfined\n" in K8S_CONTROL_PLANE


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
    # The kubelet resolves a Localhost profile against <its --root-dir>/seccomp,
    # so the write target is a variable: the default is the kubeadm/managed-cluster
    # layout, and deploy/k8s/selfhosted/ patches it (k0s: /var/lib/k0s/kubelet).
    assert '              root="${E2B_SECCOMP_ROOT:-/var/lib/kubelet/seccomp}"\n' in SECCOMP_INSTALLER
    assert "            - name: E2B_SECCOMP_ROOT\n              value: /var/lib/kubelet/seccomp\n" in SECCOMP_INSTALLER
    # Atomic replace: write a temp file in the same directory, then rename.
    assert 'tmp="$root/.sandlock-worker.json.$$"' in SECCOMP_INSTALLER
    assert 'mv "$tmp" "$dst"' in SECCOMP_INSTALLER
    # The ConfigMap is mounted read-only and never written back to.
    assert "              mountPath: /config\n              readOnly: true\n" in SECCOMP_INSTALLER
    # kubelet reads the file per container creation, so no restart is involved;
    # the 5-minute re-check is the backstop for a missed rollout.
    assert "sleep 300" in SECCOMP_INSTALLER


def _installer_seccomp_root(text: str) -> str:
    """The directory the installer targets, read out of its E2B_SECCOMP_ROOT."""
    # Indentation differs between the baseline file and a rendered overlay, so
    # match on the item and then read the first following "value:" line.
    marker = "- name: E2B_SECCOMP_ROOT\n"
    assert marker in text, "installer must declare E2B_SECCOMP_ROOT"
    for line in text.split(marker, 1)[1].split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        assert stripped.startswith("value: "), stripped
        root = stripped[len("value: ") :]
        assert root.startswith("/"), root
        return root
    raise AssertionError("no value line follows E2B_SECCOMP_ROOT")


def test_seccomp_installer_root_is_the_same_in_all_three_places() -> None:
    """E2B_SECCOMP_ROOT, the mountPath and the hostPath must name one directory.

    They are three independent literals in the manifest, and the installer is
    useless (silently, on every node) if they drift: the script would write to a
    directory the pod never mounted, or mount a directory the kubelet never reads.
    Measured on k0s 2026-09-17: with the paths pointed at the wrong root the
    worker pod simply never starts, because its Localhost profile cannot load.
    """
    root = _installer_seccomp_root(SECCOMP_INSTALLER)
    # The kubeadm/managed-cluster layout is the baseline default.
    assert root == "/var/lib/kubelet/seccomp"
    assert f"mountPath: {root}" in SECCOMP_INSTALLER
    assert f"path: {root}" in SECCOMP_INSTALLER


KUBECTL = shutil.which("kubectl")


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_k0s_overlay_moves_the_seccomp_root_to_the_k0s_kubelet_dir() -> None:
    """The k0s overlay must retarget all three places, not just the script.

    The overlay exists because the kubelet's seccomp root is derived from its
    ``--root-dir``: kubeadm uses ``/var/lib/kubelet``, k0s uses
    ``/var/lib/k0s/kubelet``. Rendering it here (rather than asserting on the
    patch file by hand) is what keeps the index-based JSON patch honest -- if the
    baseline's container/volume order changes, this fails instead of silently
    mounting the wrong directory on a live cluster.
    """
    rendered = subprocess.run(
        [KUBECTL, "kustomize", str(REPO / "deploy" / "k8s-k0s")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    out = rendered.stdout
    assert _installer_seccomp_root(out) == "/var/lib/k0s/kubelet/seccomp"
    assert "mountPath: /var/lib/k0s/kubelet/seccomp" in out
    assert "path: /var/lib/k0s/kubelet/seccomp" in out
    # No leftovers pointed at the kubeadm root: a second mount of the same volume
    # at the old path is exactly the half-switch this test exists to catch.
    assert "mountPath: /var/lib/kubelet/seccomp" not in out
    assert "path: /var/lib/kubelet/seccomp" not in out
    # The shared-storage PV rides along with the overlay.
    assert "name: sandlock-shared-nas" in out
    # The overlay also makes the worker root: a network filesystem authorizes a
    # chown by the AUTH_SYS uid, not by the client's capabilities, so the non-root
    # file-capability broker cannot hand a sandbox tree to its pooled uid there.
    assert "runAsUser: 0" in out


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


def test_seccomp_installer_stays_off_virtual_nodes() -> None:
    """The installer tolerates every taint (it must reach control-plane nodes),
    which is exactly what made it schedule onto ACK's virtual-kubelet nodes and
    sit in `NotSupport` forever -- 5/7 ready, measured 2026-09-17 -- so a
    DaemonSet rollout never converged and §2's "wait for every node" hung.
    """
    assert "operator: Exists" in SECCOMP_INSTALLER  # still reaches every real node
    assert "nodeAffinity:" in SECCOMP_INSTALLER
    assert "key: type" in SECCOMP_INSTALLER
    assert "operator: NotIn" in SECCOMP_INSTALLER
    assert "virtual-kubelet" in SECCOMP_INSTALLER
    # The workload manifests already stay off them: they declare no tolerations,
    # so the virtual nodes' taints keep them out. Pin that, because adding a
    # blanket toleration there would put a sandbox worker on a node that cannot
    # run one.
    assert "tolerations:" not in K8S_WORKER
    assert "tolerations:" not in (REPO / "deploy" / "k8s" / "control-plane.yaml").read_text(
        encoding="utf-8"
    )


def test_worker_manifest_points_at_the_installer() -> None:
    """The Deployment keeps Localhost and names the component that installs it."""
    assert "            seccompProfile:\n              type: Localhost\n" in K8S_WORKER
    assert "              localhostProfile: sandlock-worker.json\n" in K8S_WORKER
    assert "seccomp-installer.yaml" in K8S_WORKER
