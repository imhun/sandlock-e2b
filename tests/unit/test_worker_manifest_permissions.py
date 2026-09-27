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
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
STACK_COMPOSE = (REPO / "deploy" / "stack" / "docker-compose.prod.yml").read_text(
    encoding="utf-8"
)
COMPOSE_PROD = (
    REPO / "deploy" / "compose" / "docker-compose.prod.yml"
).read_text(encoding="utf-8")
COMPOSE_MULTINODE = (
    REPO / "deploy" / "compose" / "docker-compose.multinode.yml"
).read_text(encoding="utf-8")
K8S_WORKER = (REPO / "deploy" / "k8s" / "worker.yaml").read_text(encoding="utf-8")
# C1 (wave 2): the k0s overlay's root broker DaemonSet. It is not part of the
# baseline manifest set -- a network filesystem is what forces the privileged
# actions out of the pod *and* leaves them needing root (see the file's own
# header), so it lands in the overlay that already carries the NAS PV.
K0S_PRIV_BROKER = (
    REPO / "deploy" / "k8s-k0s" / "priv-broker.yaml"
).read_text(encoding="utf-8")
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
    # crates/sandlock-core/src/context.rs). The k8s manifest dropped its
    # pod-level copy on 2026-09-17 (N5, same reason) and is asserted below.
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


#: The two entries every process in the worker container gets regardless of its
#: capabilities: the upstream default profile's own allowlist, and the N35
#: real-root pair (see the test below).
def _unconditional_allowlists() -> list[set[str]]:
    entries = [
        entry
        for entry in WORKER_SECCOMP["syscalls"]
        if entry["action"] == "SCMP_ACT_ALLOW"
        and not entry.get("includes")
        and not entry.get("args")
    ]
    assert len(entries) == 2, "expected the default allowlist plus the N35 pair"
    return [set(entry["names"]) for entry in entries]


def test_worker_seccomp_profile_is_the_default_plus_two_syscalls() -> None:
    """The profile may only relax `pidfd_getfd` and `unshare` off the default.

    Anything else here is a syscall surface the worker did not have before
    2026-09-15, so it must be a conscious edit to this test as well. (The N35
    real-root additions are the other unconditional entry and have their own
    test: `test_worker_seccomp_profile_admits_the_pivot_pair_without_the_cap_gate`.)
    """
    assert WORKER_SECCOMP["defaultAction"] == "SCMP_ACT_ERRNO"
    lists = _unconditional_allowlists()
    allowed = max(lists, key=len)
    assert {"pidfd_getfd", "unshare"} <= allowed
    # ...and the other unconditional entry is exactly the N35 pair, nothing more.
    assert min(lists, key=len) == {"pivot_root", "umount2"}
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


def test_worker_seccomp_profile_admits_the_pivot_pair_without_the_cap_gate() -> None:
    """N35: the sandbox builds its own root, so the profile must let it.

    Both halves of this shape were forced by measurement:

    * No ``CAP_SYS_ADMIN`` gate on either entry. The gate is resolved against
      the *container's* capability set when the container starts
      (``includes.caps``), while the capability that authorises the call
      belongs to the sandbox's own user namespace -- so a gated rule is dropped
      in exactly the production shape that needs it (``PROD_DROP_CAPS=SYS_ADMIN``
      is the pinned shape: measured, real-root sandboxes came up with these two
      entries and nothing else).
    * ``mount`` carries an argument filter (index 2, the filesystem type, must
      be NULL) because a bind is all a real root ever does. Without it the rule
      would hand every process in the container the ability to mount tmpfs,
      procfs and overlayfs; with it, those stay ``EPERM``. ``umount2`` and
      ``pivot_root`` have no filter -- their argument lists are too short for
      one (an index past the end reads whatever the caller left in the
      register, and libseccomp rejects that rule) and need none: the kernel
      only lets a process in a user namespace touch mounts that namespace owns.

    The workload is not in this picture: the sandbox's own filter denies the
    three calls, and ``CAP_SYS_ADMIN`` is dropped before it starts
    (``docs/chroot-workspace-exec.md`` §7 and §9.5).
    """
    gated = {
        name
        for entry in WORKER_SECCOMP["syscalls"]
        if entry.get("includes", {}).get("caps")
        for name in entry["names"]
    }
    # The pre-N35 rule is still there for anyone who does carry the capability.
    assert {"mount", "umount", "umount2", "open_tree", "move_mount"} <= gated

    ungated = {
        name: entry
        for entry in WORKER_SECCOMP["syscalls"]
        if entry["action"] == "SCMP_ACT_ALLOW"
        and not entry.get("includes")
        for name in entry["names"]
    }
    assert set(ungated) >= {"mount", "umount2", "pivot_root"}
    assert ungated["mount"]["args"] == [
        {"index": 2, "value": 0, "op": "SCMP_CMP_EQ"}
    ]
    assert ungated["umount2"]["args"] == []
    assert ungated["pivot_root"]["args"] == []


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


def test_k8s_runs_the_verified_multi_replica_worker_shape() -> None:
    """N13 closed 2026-09-17: >1 worker on one shared base is the supported shape.

    `deploy/scripts/multiworker_interference.py` is the evidence: two workers, four
    sandboxes spread across them, disjoint pooled host uids, and then a worker
    restart, after which all four trees were still on the shared base (none deleted
    by the reconciling worker), its summaries showed `protected_elsewhere=4`, the
    surviving worker's sandboxes still ran, and both workers' reservations returned
    to zero. What this pins is that the manifests actually run that shape and do not
    creep back to a single replica with an autoscaler that cannot follow.
    """
    autoscaler = (REPO / "deploy" / "k8s" / "autoscaler.yaml").read_text(
        encoding="utf-8"
    )
    marker = "name: E2B_AS_MAX_REPLICAS"
    assert marker in autoscaler
    # The value sits a few comment lines below the name, so read forward rather
    # than requiring the two to be adjacent.
    following = autoscaler[autoscaler.index(marker):][:1200]
    assert 'value: "16"' in following
    assert "\n  replicas: 2\n" in K8S_WORKER
    # ...and the floor matches the compose stack's two always-on workers, so the
    # autoscaler does not drain half the fleet whenever the queue is empty.
    assert 'name: E2B_AS_MIN_REPLICAS' in autoscaler
    min_section = autoscaler[autoscaler.index("name: E2B_AS_MIN_REPLICAS"):][:600]
    assert 'value: "2"' in min_section
    # The precondition that makes the shape safe has to stay written down where the
    # value is: locks that reach across nodes. NFSv3 + `nolock` would let two
    # replicas hand out the same host uid.
    assert "nolock" in following


def test_k8s_worker_is_a_statefulset_so_its_node_ids_survive_a_restart() -> None:
    """N20: the worker's node id is its pod name, so the pod name has to be stable.

    A Deployment gives every restart a brand-new pod name, i.e. a brand-new node id.
    The control plane keeps the previous incarnation's sandbox records under the old
    id, cannot route to them until they age out (`Node unavailable: All connection
    attempts failed`, measured), and the old node lingers as a zombie in the fleet
    view. A StatefulSet's ordinals remove that class outright -- and they are what
    the compose stack has always had (`E2B_NODE_ID: worker-1` / `worker-2`), so this
    is the k8s half of an existing property rather than a new one.

    The service reference and the rollout strategy are pinned with it: `serviceName`
    is required for the stable per-pod DNS that makes the ordinals real, and
    StatefulSet updates are already delete-then-create, one pod at a time. That last
    part is also why there is no `maxSurge: 0` to look for any more -- this workload
    declares no `requests`, so Kubernetes copies the 2-CPU limit into the request and
    a surge pod cannot fit a 4-core node (measured 2026-09-17: a Deployment rollout
    stalled with `0/2 nodes are available: 1 Insufficient cpu`).
    """
    assert K8S_WORKER.startswith("apiVersion: apps/v1\nkind: StatefulSet\n")
    assert "  serviceName: worker-headless\n" in K8S_WORKER
    assert "  podManagementPolicy: Parallel\n" in K8S_WORKER
    assert "  updateStrategy:\n    type: RollingUpdate\n" in K8S_WORKER
    # No Deployment-only leftovers: `strategy.maxSurge` is silently ignored on a
    # StatefulSet, so leaving it behind would read as a guard that is not there.
    assert "    rollingUpdate:\n      maxSurge:" not in K8S_WORKER
    assert "\n  strategy:\n" not in K8S_WORKER
    # The autoscaler has to scale the same kind, or it would 404 on every tick.
    autoscaler = (REPO / "deploy" / "k8s" / "autoscaler.yaml").read_text(
        encoding="utf-8"
    )
    assert "name: E2B_AS_K8S_KIND\n" in autoscaler
    assert "value: statefulset\n" in autoscaler


def test_k8s_control_plane_replicas_come_with_the_shape_that_makes_them_safe() -> None:
    """Two replicas are legal now -- but only with the shape that keeps them so.

    This guard used to say "one replica": the node registry was per-process
    (`NodeRegistry._nodes` being an in-memory dict, with Redis carrying only the
    quota ledger and the sandbox records), so a worker's registration and
    heartbeats stuck to whichever replica its HTTP connection reached and the
    other aged the node past its heartbeat timeout. Measured on k0s 2026-09-17:
    replica A said `fxf2j: unhealthy` while replica B said `fxf2j: healthy`, for
    12 consecutive samples -- which produced `502 Node ... unavailable` route
    lookups, uneven placement, and `reap_unhealthy` treating live sandboxes as
    orphans (a `404 Sandbox ... not found` in the smoke).

    F11 (2026-09-26) made the view shared, so the replica count flipped -- and
    these four lines are the part that is *not* tuning. `maxSurge: 0` keeps the
    rollout from asking a two-node cluster for a third pod (the default
    `25%` rounds up, and the pod then sits `Pending` forever against the
    required anti-affinity below). The anti-affinity is what makes "two
    replicas" survive a node loss instead of being a single point of failure
    that looks like HA. The PDB is what stops a drain from evicting both at
    once. `docs/control-plane-multi-replica.md` §6 is the list of what was made
    shared (and of the two things deliberately left per-replica).
    """
    assert "\n  replicas: 2\n" in K8S_CONTROL_PLANE
    assert "      maxSurge: 0\n" in K8S_CONTROL_PLANE
    assert "              topologyKey: kubernetes.io/hostname\n" in K8S_CONTROL_PLANE
    assert "kind: PodDisruptionBudget" in K8S_CONTROL_PLANE


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


def test_worker_keeps_the_extracted_rootfs_off_the_shared_volume() -> None:
    """The tars stay shared; the unpack stays on the node's own disk.

    Unpacking a rootfs onto the shared volume costs 240x what it costs locally
    (measured 2026-09-17 on Aliyun NAS: 61.4s versus 0.26s for the same 2111-file
    python-slim rootfs), and that unpack happens while the sandbox's first command
    waits -- which on a real cluster produced minutes-long node silences and let
    E6.1 reap live sandboxes (docs/task-backlog.md N18). With the split, the smoke
    that used to hang now completes end to end in ~20s.
    """
    assert "            - name: E2B_IMAGE_CACHE_DIR\n              value: /var/lib/e2b-images\n" in K8S_WORKER
    assert (
        "            - name: E2B_IMAGE_OCI_DIR\n"
        "              value: /var/lib/e2b-sandboxes/_images\n" in K8S_WORKER
    )
    # The control plane still exports its tars into the shared cache, so the
    # worker's producer directory has to be that same path.
    assert "E2B_IMAGE_CACHE_DIR\n              value: /var/lib/e2b-sandboxes/_images" not in K8S_WORKER
    # Node-local, and NOT the RWX claim: an unpack into the shared PVC is the
    # thing this test exists to prevent.
    assert "        - name: image-cache\n          hostPath:\n            path: /var/lib/e2b-images\n" in K8S_WORKER
    assert (
        "        - name: image-cache\n"
        "          hostPath:\n"
        "            path: /var/lib/e2b-images\n"
        "            type: DirectoryOrCreate\n" in K8S_WORKER
    )
    # ...while the workspaces keep arriving through the RWX claim.
    assert "          persistentVolumeClaim:\n            claimName: sandbox-shared\n" in K8S_WORKER
    # Both caches are prepared (created + handed to the worker uid) by the init
    # container, in the order the resolver uses them -- and as of C1 that
    # container lives in the root broker DaemonSet, not in the worker pod (the
    # worker has no root left to chown a cache with). Byte-exact on the
    # ordered list, because the resolver and the init must agree on which
    # directory comes first.
    assert "value: /var/lib/e2b-images /var/lib/e2b-sandboxes/_images\n" in K0S_PRIV_BROKER


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
    """The profile exactly as the ConfigMap carries it (block scalar dedented).

    The marker is ``"|"`` (keep) and deliberately not ``"|-"`` (strip): the
    profile file ends with a newline, so a stripped scalar would hand the node
    a payload one byte shorter than the file every test here compares against.
    Matching on ``"|\\n"`` is strict -- the ``|-`` form has ``-`` where this
    marker wants the newline, so it stops matching rather than matching loosely.
    """
    marker = "  sandlock-worker.json: |\n"
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


def test_seccomp_installer_scalar_keeps_the_trailing_newline() -> None:
    """`|`, never `|-` -- the applied payload has to be the file, byte for byte.

    `|-` strips the block scalar's final newline, and the profile file has one,
    so a stripped scalar makes the value the ConfigMap carries (and the node
    writes) one byte short of the file this suite, the in-file comment and the
    `checksum/profile` annotation all name. Measured 2026-09-25 against the
    rendered stack: the applied value hashed to `e79a1c6a…` while the annotation
    said `071486c0…`. Nothing broke (JSON and the kubelet both tolerate it) --
    which is exactly why it needs pinning rather than trusting.
    """
    assert "  sandlock-worker.json: |\n" in SECCOMP_INSTALLER
    assert "  sandlock-worker.json: |-\n" not in SECCOMP_INSTALLER
    payload = _installer_configmap_payload()
    assert payload.endswith("\n"), (
        "the profile file ends with a newline; a scalar that strips it silently "
        "changes what the node is given"
    )


def test_seccomp_installer_comment_names_the_payload_hash() -> None:
    """The comment above the payload carries that payload's sha256, pinned.

    The comment is documentation, not mechanism -- which is exactly why it
    drifted: the N35 resync bumped the payload and the `checksum/profile`
    annotation but left this line on the pre-N35 hash (`0e07967a…`), so the one
    line a human would read named a profile no manifest shipped. Measured
    2026-09-25: the deployed ConfigMap carried `0e07967a…` while the repo's
    payload was `071486c0…`, and the stale comment made the live-vs-repo
    comparison read as agreement at a glance.
    """
    import hashlib

    payload = _installer_configmap_payload()
    digest = hashlib.sha256(payload.encode()).hexdigest()
    assert (
        f"# tests/unit/test_worker_manifest_permissions.py); sha256 {digest}\n"
        in SECCOMP_INSTALLER
    )


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
    # ...and so does the cluster's own fixed entry point. Pinned because it is
    # the only stable address remote clients have (there is no LB here), and a
    # changed or missing nodePort silently breaks every bastion forward, CI job
    # and operator script that was told to use it.
    assert "name: gateway-nodeport" in out
    assert "  type: NodePort\n" in out
    assert "    nodePort: 31907\n" in out
    assert "    port: 49983\n" in out
    assert "    targetPort: 3000\n" in out
    # The overlay also makes the worker root: a network filesystem authorizes a
    # chown by the AUTH_SYS uid, not by the client's capabilities, so the non-root
    # file-capability broker cannot hand a sandbox tree to its pooled uid there.
    #
    # Read it off the *worker container*, not the rendered text: `runAsUser: 0`
    # appears in other objects (control-plane, the seccomp installer) and in the
    # baseline's init containers, so a substring match passes even when this patch
    # misses its target entirely. That is not hypothetical -- moving the worker from
    # Deployment to StatefulSet without updating the patch's `target.kind` left the
    # container running as the image's 65534, and every create then failed with
    # `Permission denied: <base>/.uid_pool.lock` (a root-owned 0600 file the NAS
    # will not let 65534 open).
    worker = _rendered_workload(out, "StatefulSet", "e2b-worker")
    security = worker["spec"]["template"]["spec"]["containers"][0]["securityContext"]
    assert security["runAsUser"] == 0
    assert security["runAsGroup"] == 65534


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_k0s_overlay_sinks_the_tree_root_and_keeps_state_as_a_sibling() -> None:
    """N27: the sandbox trees sink one level, the platform's state moves beside them.

    Read off the *rendered worker container* rather than the manifest text: the
    same three variable names occur in the compose stacks and in the baseline
    manifest, so a text assertion cannot tell which object it matched, and the
    overlay is what the cluster actually runs.
    """
    rendered = subprocess.run(
        [KUBECTL, "kustomize", str(REPO / "deploy" / "k8s-k0s")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    worker = _rendered_workload(rendered, "StatefulSet", "e2b-worker")
    env = {
        e["name"]: e.get("value")
        for e in worker["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["E2B_WORKSPACE_BASE"] == "/var/lib/e2b-sandboxes/workspaces"
    assert env["E2B_STATE_BASE"] == "/var/lib/e2b-sandboxes/state"
    assert env["E2B_ROUTE_B_TMP_ROOT"] == "/var/lib/e2b-sandboxes/state/.route-b"
    # N27 (Task 5 follow-up): the export root is named as the *third* broker
    # root, because the tree root no longer is it -- `_volumes`/`_images` would
    # otherwise fall outside the whitelist a non-root worker's brokers enforce.
    assert env["E2B_SHARED_VOLUME_ROOT"] == "/var/lib/e2b-sandboxes"
    # The image cache is *not* platform state (the control plane exports the OCI
    # layout tars into it and every worker resolves them from there), so it stays
    # on the shared export root.
    assert env["E2B_IMAGE_OCI_DIR"] == "/var/lib/e2b-sandboxes/_images"
    # 反例：state 不得落在树根之下，否则沙箱的 `..` 又会到它
    assert not env["E2B_STATE_BASE"].startswith(env["E2B_WORKSPACE_BASE"])


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_k0s_overlay_control_plane_writes_only_the_migration_staging_and_state() -> None:
    """The control plane's writes have to land on writable subPaths (OBS-9).

    The whole volume stays read-only, so a directory the control plane writes to
    is only writable if it is mounted back on top as a `subPath`. After N27
    there are two more of those: ``state`` (the platform's own files -- without
    it every record write is EROFS) and ``workspaces/_migrate`` (the migration
    staging, where a remote node's export tar is written).

    `workspaces/_migrate` and **not** `workspaces`: OBS-9 measured "the control
    plane writing a sandbox tree is EROFS" (2026-09-18) and that property is
    kept -- mounting the tree root writable would hand it back. Missing either
    mount leaves the pod in ``ContainerCreating`` (no subPath source) or the
    write EROFS.
    """
    rendered = subprocess.run(
        [KUBECTL, "kustomize", str(REPO / "deploy" / "k8s-k0s")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    plane = _rendered_workload(rendered, "Deployment", "control-plane")
    container = plane["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["E2B_SHARED_WORKSPACE_ROOT"] == "/var/lib/e2b-sandboxes"
    assert env["E2B_WORKSPACE_BASE"] == "/var/lib/e2b-sandboxes/workspaces"
    assert env["E2B_STATE_BASE"] == "/var/lib/e2b-sandboxes/state"
    shared = [m for m in container["volumeMounts"] if m["name"] == "shared"]
    # The whole volume, read-only, and nothing else on that volume without a
    # subPath -- the complete shape, not a "these two are present" check.
    assert [m for m in shared if "subPath" not in m] == [
        {"name": "shared", "mountPath": "/var/lib/e2b-sandboxes", "readOnly": True}
    ]
    assert {m["subPath"]: m for m in shared if "subPath" in m} == {
        "_builds": {
            "name": "shared",
            "mountPath": "/var/lib/e2b-sandboxes/_builds",
            "subPath": "_builds",
        },
        "_images": {
            "name": "shared",
            "mountPath": "/var/lib/e2b-sandboxes/_images",
            "subPath": "_images",
        },
        "_secrets": {
            "name": "shared",
            "mountPath": "/var/lib/e2b-sandboxes/_secrets",
            "subPath": "_secrets",
        },
        "_snapshots": {
            "name": "shared",
            "mountPath": "/var/lib/e2b-sandboxes/_snapshots",
            "subPath": "_snapshots",
        },
        "_templates": {
            "name": "shared",
            "mountPath": "/var/lib/e2b-sandboxes/_templates",
            "subPath": "_templates",
        },
        "_volumes": {
            "name": "shared",
            "mountPath": "/var/lib/e2b-sandboxes/_volumes",
            "subPath": "_volumes",
        },
        # The staging directory, *not* the tree root: sandbox trees stay EROFS
        # for this pod (OBS-9). The key is the subPath, so a future edit that
        # widened it to `workspaces` fails here.
        "workspaces/_migrate": {
            "name": "shared",
            "mountPath": "/var/lib/e2b-sandboxes/workspaces/_migrate",
            "subPath": "workspaces/_migrate",
        },
        "state": {
            "name": "shared",
            "mountPath": "/var/lib/e2b-sandboxes/state",
            "subPath": "state",
        },
    }


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_k0s_overlay_broker_init_creates_both_roots_and_the_checkpoint_gate() -> None:
    """The two roots and the checkpoint store's gate are an init container's job.

    Three failures this pins, all measured elsewhere in N27:

    * ``<state>`` missing -- ``uid_pool.acquire`` opens ``<state>/.uid_pool.lock``
      with ``O_CREAT`` under the base it is handed, so the *first*
      ``Sandbox.create()`` would die with ENOENT instead of handing out a uid;
    * ``.checkpoints`` at the wrong mode -- the store's gate has to be
      traversable by the pooled sandbox uid (the slot is what writes the image)
      and listable by nobody, which is ``0711``; ``0700`` there is the
      measured EACCES that killed the first checkpoint capture (2026-09-25).
    * ``<workspaces>/_migrate`` missing -- it is the *source* of the control
      plane's one writable subPath under the tree root, and a subPath whose
      source does not exist keeps that pod in ``ContainerCreating``.

    C1 moved the container: it has to run as root to chown the volume roots,
    and the worker pod is no longer allowed a root container of any kind, so
    the DaemonSet carries it (the worker keeps no copy -- pinned below, because
    a leftover root init in the pod is exactly the half-switch this move has to
    rule out).

    The environment dictionary is compared whole and the script is matched line
    by line, on stripped lines: a rewritten line has to be re-read here rather
    than pass on a substring of the old one.
    """
    rendered = subprocess.run(
        [KUBECTL, "kustomize", str(REPO / "deploy" / "k8s-k0s")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    worker = _rendered_workload(rendered, "StatefulSet", "e2b-worker")
    broker = _rendered_workload(rendered, "DaemonSet", "e2b-priv-broker")
    inits = {
        c["name"]: c for c in broker["spec"]["template"]["spec"]["initContainers"]
    }
    init = inits["workspace-root-init"]
    # It is the broker's init, and the worker pod does not carry a second
    # (root) copy of it.
    assert (
        "workspace-root-init"
        not in {
            c["name"]
            for c in worker["spec"]["template"]["spec"].get("initContainers", [])
        }
    )
    assert {e["name"]: e.get("value") for e in init["env"]} == {
        "SHARED_ROOT": "/var/lib/e2b-sandboxes",
        "WORKSPACE_BASE": "/var/lib/e2b-sandboxes/workspaces",
        "STATE_BASE": "/var/lib/e2b-sandboxes/state",
    }
    assert init["volumeMounts"] == [
        {"name": "shared", "mountPath": "/var/lib/e2b-sandboxes"}
    ]
    lines = [line.strip() for line in init["command"][2].splitlines()]
    for expected in (
        'shared="$SHARED_ROOT"',
        'base="$WORKSPACE_BASE"',
        'state="$STATE_BASE"',
        # The platform's own namespaces stay under the shared export root.
        "for dir in _builds _images _secrets _templates _snapshots _volumes; do",
        'mkdir -p "$shared/$dir"',
        # ...and the two new roots are created (they are the subPath sources the
        # control plane mounts, and the state base the uid pool needs).
        'for dir in "$base" "$state"; do',
        'mkdir -p "$dir"',
        # ...plus the one directory inside the tree root the control plane is
        # allowed to write (its migration staging).
        'mkdir -p "$base/_migrate"',
        'mkdir -p "$state/_runtime/.checkpoints"',
        'chmod 0711 "$state/_runtime" "$state/_runtime/.checkpoints" 2>/dev/null ||',
        # base, state and the migration staging get the same "writable by the
        # worker" judgement (the worker stages a tar in `_migrate` too).
        'for target in "$base" "$state" "$base/_migrate"; do',
        'owner="$(stat -c %u "$target")"',
    ):
        assert expected in lines, expected


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_k0s_overlay_runs_the_root_broker_daemonset_with_the_workers_identity() -> None:
    """C1: the privileged actions leave the pod; a per-node root broker does them.

    What is pinned, and why each half matters:

    * the command is ``e2b-maint serve --socket …``: the daemon only execs
      itself, so the argv (and the socket path the worker dials) is the
      contract between the two manifests;
    * ``runAsUser: 0`` + exactly ``CHOWN, DAC_OVERRIDE, FOWNER``: a network
      filesystem authorizes a chown by the AUTH_SYS uid, not by the client's
      capabilities (measured on this NAS, ``worker-root.patch.yaml``), so the
      privileged hand-over needs root -- and only root can remove or walk an
      arbitrarily-owned sandbox tree;
    * ``E2B_BROKER_PEER_UID/GID == 65534``: the daemon answers ``hello`` with
      the identity it authenticated, and the worker's Python startup asserts
      ``peer_uid/peer_gid == its own euid/egid`` (a mismatch is a refusal with
      the pair named). 65534 is the worker image's ``USER 65534:65534``; the
      same group is what the socket directory and the socket file itself carry
      (``socket-dir-init`` chowns to ``0:65534``, ``serve`` creates the socket
      ``0660 root:<peer gid>``), so the worker's group can reach it and a
      sandbox uid cannot;
    * the two owner inits moved here from the worker manifest, and the socket
      directory is prepared as ``0710`` -- root creates the socket inside,
      the worker group may traverse but not list, and nobody else may enter.

    Read off the rendered object rather than the file text: the same keys
    (``runAsUser: 0``, the capability names, the env names) occur in other
    workloads, so only the parsed object says which pod the values belong to.
    """
    rendered = subprocess.run(
        [KUBECTL, "kustomize", str(REPO / "deploy" / "k8s-k0s")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    broker = _rendered_workload(rendered, "DaemonSet", "e2b-priv-broker")
    pod = broker["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["name"] == "broker"
    assert container["command"] == [
        "/var/lib/e2b-priv/e2b-maint",
        "serve",
        "--socket",
        "/run/e2b-broker/broker.sock",
    ]
    security = container["securityContext"]
    assert security["runAsUser"] == 0
    assert set(security["capabilities"]["add"]) == {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
    }
    env = {e["name"]: e.get("value") for e in container["env"]}
    # The identity trio (worker pod == peer env == socket group) is 65534.
    assert env["E2B_BROKER_PEER_UID"] == "65534"
    assert env["E2B_BROKER_PEER_GID"] == "65534"
    # ...and the daemon's whitelist is the same four roots the worker resolves:
    # the two bases, the export root and the node-local image cache. A missing
    # or reordered root fails the hello handshake at worker startup.
    assert env["E2B_WORKSPACE_BASE"] == "/var/lib/e2b-sandboxes/workspaces"
    assert env["E2B_STATE_BASE"] == "/var/lib/e2b-sandboxes/state"
    assert env["E2B_SHARED_VOLUME_ROOT"] == "/var/lib/e2b-sandboxes"
    assert env["E2B_IMAGE_CACHE_DIR"] == "/var/lib/e2b-images"
    assert env["E2B_ROUTE_B_TMP_ROOT"] == "/var/lib/e2b-sandboxes/state/.route-b"
    # The pool is part of the same handshake and has to equal the worker's
    # (the k8s worker follows the code default, 10000/1000).
    assert env["E2B_UID_POOL_START"] == "10000"
    assert env["E2B_UID_POOL_SIZE"] == "1000"
    for probe in ("livenessProbe", "readinessProbe"):
        assert container[probe]["exec"]["command"] == [
            "/var/lib/e2b-priv/e2b-maint",
            "ping",
            "--socket",
            "/run/e2b-broker/broker.sock",
        ]
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["shared"]["mountPath"] == "/var/lib/e2b-sandboxes"
    assert mounts["image-cache"]["mountPath"] == "/var/lib/e2b-images"
    assert mounts["broker-socket"]["mountPath"] == "/run/e2b-broker"
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["shared"]["persistentVolumeClaim"] == {
        "claimName": "sandbox-shared"
    }
    assert volumes["image-cache"]["hostPath"] == {
        "path": "/var/lib/e2b-images",
        "type": "DirectoryOrCreate",
    }
    assert volumes["broker-socket"]["hostPath"] == {
        "path": "/run/e2b-broker",
        "type": "DirectoryOrCreate",
    }
    inits = {c["name"]: c for c in pod["initContainers"]}
    # All three: the socket directory plus the two owner inits that used to be
    # root containers inside the worker pod.
    assert set(inits) == {"socket-dir-init", "image-cache-init", "workspace-root-init"}
    socket_init = inits["socket-dir-init"]
    assert socket_init["securityContext"]["runAsUser"] == 0
    assert socket_init["volumeMounts"] == [
        {"name": "broker-socket", "mountPath": "/run/e2b-broker"}
    ]
    socket_lines = [line.strip() for line in socket_init["command"][2].splitlines()]
    for expected in (
        "mkdir -p /run/e2b-broker",
        "chown 0:65534 /run/e2b-broker",
        "chmod 0710 /run/e2b-broker",
    ):
        assert expected in socket_lines, expected


def _rendered_workload(rendered: str, kind: str, name: str) -> dict:
    """The one rendered object of ``kind``/``name``.

    Parsing beats grepping for anything about a pod's security context: the same
    keys appear in every workload, so text assertions cannot tell which object
    they matched.
    """
    matches = [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict)
        and doc.get("kind") == kind
        and doc.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected exactly one {kind}/{name}: {len(matches)}"
    return matches[0]


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_k0s_overlay_sets_a_node_liveness_window_wider_than_placement() -> None:
    """The overlay must widen the orphan window, and only in the safe direction.

    Two windows read the same "how long since this node last spoke" fact and mean
    opposite things:

    * ``PLACEMENT_MAX_HEARTBEAT_AGE_S`` (15s, in code) stops *new work* going to a
      node that went quiet. Being wrong here costs the caller a 502.
    * ``E2B_NODE_HEARTBEAT_TIMEOUT`` declares the node *gone*, and reaping marks
      its live sandboxes orphaned -- their route-B slots go with them. Being wrong
      here loses sandboxes that were fine (the N18 failure).

    So the overlay's window has to be the wider one, and it has to stay wide enough
    for a worker whose heartbeat is late because it is running a reconcile round
    (heartbeats and the round share one coroutine: gap = 5s + round). The value was
    300s while the worker unpacked cold images on its event loop; that path is
    asynchronous now, so 60s was measured in (docs/k8s-deployment.md §14). This
    asserts the ordering, not the number: a future edit may retune it, but not
    invert it, and not drop it below a few heartbeat intervals (5s each).
    """
    from control_plane.registry.nodes import PLACEMENT_MAX_HEARTBEAT_AGE_S

    rendered = subprocess.run(
        [KUBECTL, "kustomize", str(REPO / "deploy" / "k8s-k0s")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    matches = re.findall(
        r"name: E2B_NODE_HEARTBEAT_TIMEOUT\s+value: \"?([0-9.]+)\"?", rendered.stdout
    )
    assert matches, "the k0s overlay must set E2B_NODE_HEARTBEAT_TIMEOUT"
    assert len(set(matches)) == 1, f"the overlay sets two different windows: {matches}"
    window = float(matches[0])

    intervals = window / 5.0  # the worker heartbeats every 5s
    assert intervals >= 3, f"a {window:g}s window is fewer than 3 heartbeat intervals"
    assert window > PLACEMENT_MAX_HEARTBEAT_AGE_S, (
        f"the orphan window ({window:g}s) must be wider than the placement window "
        f"({PLACEMENT_MAX_HEARTBEAT_AGE_S:g}s): stop giving work first, declare the "
        "node gone later"
    )


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


def test_compose_prod_example_runs_the_fleet_netns_shape() -> None:
    """N36: the single-host example follows the fleet, window and all.

    `deploy/compose/docker-compose.prod.yml` ran the shared-netns shape (uid
    65534) and paid for it with a container-level
    `net.ipv4.ip_unprivileged_port_start=0` window. It now carries the same
    paired switches the stack anchor does, so the wildcard-DNS `:53` bind
    happens inside each sandbox's own netns instead. A half-migration -- window
    back, or one switch without the other -- is what these assertions catch.

    Pinned here rather than in a new file because this module already owns the
    "the manifests must not ask the worker to bind a low port" family, and the
    slice trick below is the same one the stack assertions use.
    """
    worker = COMPOSE_PROD.split("\n  worker-1: &worker", 1)[1].split(
        "\n  worker-2:", 1
    )[0]
    # The directive, not the prose: the comment above the line names the old
    # value on purpose.
    assert "\n    sysctls:\n" not in COMPOSE_PROD
    assert "\n      - net.ipv4.ip_unprivileged_port_start=0\n" not in COMPOSE_PROD
    # The anchor every worker inherits (worker-2/worker-3 use `<<: *worker-env`).
    assert "\n      E2B_ENABLE_NET_ISOLATION: ${E2B_ENABLE_NET_ISOLATION:-true}\n" in worker
    assert "\n      E2B_FD_INJECT_CONNECT: ${E2B_FD_INJECT_CONNECT:-true}\n" in worker
    # The worker still runs the shipped seccomp profile, not `unconfined`.
    assert "\n      - seccomp=${E2B_SECCOMP_PROFILE:-../seccomp/sandlock-worker.json}\n" in worker


def _value_after_key(text: str, key: str, *, indent: str) -> str:
    """The single ``<indent><key>: <value>`` line's value, whitespace stripped."""
    marker = f"{indent}{key}:"
    hits = [line for line in text.splitlines() if line.startswith(marker)]
    assert len(hits) == 1, f"expected exactly one {key!r} line at that indent: {hits}"
    return hits[0][len(marker) :].strip().strip('"')


def _k8s_env_value(text: str, key: str) -> str:
    """The ``- name: <key>`` / ``value: <v>`` pair a k8s container env spells."""
    lines = [line.strip() for line in text.splitlines()]
    marker = f"- name: {key}"
    hits = [index for index, line in enumerate(lines) if line == marker]
    assert len(hits) == 1, f"expected exactly one {key!r} env entry: {hits}"
    value_line = lines[hits[0] + 1]
    assert value_line.startswith("value: "), value_line
    return value_line[len("value: ") :].strip().strip('"')


def _fleet_route_b_roots() -> dict[str, str]:
    """`E2B_ROUTE_B_TMP_ROOT` as each fleet manifest spells it.

    Both manifests are read, not just the compose one: the pool's alignment
    test learned that reading one leaves the other free to drift
    (`tests/unit/test_autoscaler_local_backend_shape.py`), and the k8s pod
    template is the manifest the cluster actually runs.
    """
    return {
        "deploy/stack/docker-compose.prod.yml": _value_after_key(
            STACK_COMPOSE, "E2B_ROUTE_B_TMP_ROOT", indent="      "
        ),
        "deploy/k8s/worker.yaml": _k8s_env_value(
            K8S_WORKER, "E2B_ROUTE_B_TMP_ROOT"
        ),
    }


def _compose_prod_worker_route_b_root() -> dict[str, str]:
    """The same key, read off the prod example's own worker anchor.

    Anchored to the slice every worker inherits (`worker-1: &worker` up to
    `worker-2:`) rather than to the file: a bare `in` over the whole file would
    pass even if the key drifted into a service that never reads it.
    """
    worker = COMPOSE_PROD.split("\n  worker-1: &worker", 1)[1].split(
        "\n  worker-2:", 1
    )[0]
    found: dict[str, str] = {}
    for line in worker.splitlines():
        stripped = line.strip()
        if stripped.startswith("E2B_ROUTE_B_TMP_ROOT:"):
            found["E2B_ROUTE_B_TMP_ROOT"] = stripped.split(":", 1)[1].strip().strip('"')
    return found


def test_compose_prod_worker_env_carries_the_fleets_route_b_root() -> None:
    """N39 at site ①: a from-tree worker refuses to start without this key.

    `E2B_ROUTE_B_TMP_ROOT` defaults to `/tmp/sandlock-route-b`, which is outside
    the roots the `e2b-maint` file-capability broker may touch, so
    `configure_priv_helpers` refuses the shape by name at startup -- the worker
    crash-loops before it ever listens (measured on this file 2026-09-26 with
    `up -d --build`: all three workers `Restarting (1)`). The pool hit the same
    wall (N39) and the fleet has always declared the key, so this file takes the
    fleet's value instead of inventing one: the assertion compares against the
    fleet manifests themselves, not against a third copy of the literal.

    N27 (2026-09-26) split that one value into two *shapes*: the k8s manifest
    sinks the tree root (`<export>/workspaces`) and moves the platform's own
    files under `E2B_STATE_BASE`, `.route-b` among them, while both compose
    stacks keep the one-base layout. The rule is the same in both -- the slot
    pool writes its documents under the base the platform's own files live in --
    so the pin below compares each manifest against *its own* bases instead of
    against a single fleet-wide literal.
    """
    fleet = _fleet_route_b_roots()
    # One base (workspace base == state base), so this file takes the path under
    # it that `deploy/stack/docker-compose.prod.yml:239` explains.
    assert _compose_prod_worker_route_b_root() == {
        "E2B_ROUTE_B_TMP_ROOT": fleet["deploy/stack/docker-compose.prod.yml"]
    }
    # Two bases in the k8s manifest: `.route-b` is platform state, so it follows
    # the state base -- and must *not* be under the tree root, which is the one
    # directory a sandbox reaches by walking `..` (these documents carry the
    # egress proxy's credentials).
    state_base = _k8s_env_value(K8S_WORKER, "E2B_STATE_BASE")
    tree_root = _k8s_env_value(K8S_WORKER, "E2B_WORKSPACE_BASE")
    assert fleet["deploy/k8s/worker.yaml"] == f"{state_base}/.route-b"
    assert not fleet["deploy/k8s/worker.yaml"].startswith(f"{tree_root}/")
    # ...and the two shapes really do differ, so copying either value into the
    # other manifest fails here.
    assert (
        fleet["deploy/k8s/worker.yaml"]
        != fleet["deploy/stack/docker-compose.prod.yml"]
    )


def _multinode_worker_block(name: str) -> str:
    tail = COMPOSE_MULTINODE.split(f"\n  {name}:", 1)[1]
    for marker in ("\n  worker-1:", "\n  worker-2:", "\n  worker-3:", "\nvolumes:"):
        if marker in tail:
            tail = tail.split(marker, 1)[0]
    return tail


def test_multinode_example_runs_the_fleet_netns_shape() -> None:
    """N36's fourth site: the file that was broken the other way round.

    `deploy/compose/docker-compose.multinode.yml` ran the shared-netns shape
    *and* declared no low-port window, so its own comment admitted wildcard
    `allowOut` rules would fail with `bind DNS gateway: Permission denied
    (os error 13)` (docs/open-issues.md N36). Aligning it with the fleet both
    removes the window question and fixes the wildcard path. There is no
    anchor in this file: each of the three workers repeats its whole env block,
    so all three are asserted separately.

    Measured 2026-09-26 on this file (uid 65534, OrbStack): the first sentence
    is N36's claim -- the one this file's own comment repeated -- and it does
    *not* reproduce. A Docker container's netns already reads
    `net.ipv4.ip_unprivileged_port_start=0` with no `sysctls:` declared, so the
    shared-netns shape bound `127.0.1.x:53` fine and resolved wildcards
    (`tmp/netns-unify-wildcard-before-b2.log`); forcing the window shut is what
    reproduces N36's `bind DNS gateway: Permission denied (os error 13)`
    (`tmp/netns-unify-wildcard-forced-window-shut.log`). So these assertions are
    the *alignment* with the fleet, not the repair of a wildcard bug: what this
    file is actually missing for wildcard `allowOut` is `E2B_ENABLE_NETWORK`
    (N42's shape -- no policy reaches the fork, so no gateway is created). That
    key and the seccomp directive were still open here at the time; both landed
    2026-09-26 and are pinned by the N42 tests at the bottom of this file.
    """
    assert "\n    sysctls:\n" not in COMPOSE_MULTINODE
    assert "\n      - net.ipv4.ip_unprivileged_port_start=0\n" not in COMPOSE_MULTINODE
    for name in ("worker-1", "worker-2", "worker-3"):
        block = _multinode_worker_block(name)
        assert '\n      E2B_ENABLE_NET_ISOLATION: "true"\n' in block, name
        assert '\n      E2B_FD_INJECT_CONNECT: "true"\n' in block, name
        assert f"\n      E2B_NODE_ID: {name}\n" in block, name


def _multinode_worker_route_b_root(name: str) -> str:
    """`E2B_ROUTE_B_TMP_ROOT` as one worker block spells it, quotes stripped."""
    block = _multinode_worker_block(name)
    found = [
        line.strip().split(":", 1)[1].strip().strip('"')
        for line in block.splitlines()
        if line.strip().startswith("E2B_ROUTE_B_TMP_ROOT:")
    ]
    assert len(found) == 1, f"expected exactly one E2B_ROUTE_B_TMP_ROOT: {found}"
    return found[0]


def test_multinode_worker_env_carries_the_fleets_route_b_root() -> None:
    """N39 at site ④: the same wall ① hit, measured on this file 2026-09-26.

    `E2B_ROUTE_B_TMP_ROOT` defaults to `/tmp/sandlock-route-b`, which is outside
    the roots the `e2b-maint` file-capability broker may touch, so
    `configure_priv_helpers` refuses the shape by name at startup and every
    worker crash-loops before it ever listens (measured here: all three
    `Restarting (1)` with `route-B scratch root /tmp/sandlock-route-b is outside
    the privileged helper roots (/var/lib/e2b-sandboxes)`; `docs/open-issues.md`
    N39 is the same wall). The value is taken from the fleet manifests, not
    written a fourth time -- the same comparison ①'s pin makes, and it is per
    worker because this file has no anchor.

    Same N27 split as ①: this is the one-base compose shape, so the value it has
    to carry is the compose stack's, not the k8s manifest's (which sinks the
    tree root and puts `.route-b` under `E2B_STATE_BASE`).
    """
    fleet = _fleet_route_b_roots()
    fleet_root = fleet["deploy/stack/docker-compose.prod.yml"]
    for name in ("worker-1", "worker-2", "worker-3"):
        assert _multinode_worker_route_b_root(name) == fleet_root, name


STACK_ENV_EXAMPLE = (REPO / "deploy" / "stack" / ".env.example").read_text(
    encoding="utf-8"
)


def test_no_comment_still_claims_a_pod_level_sysctl_exists() -> None:
    """The window was deleted on 2026-09-17 (N5); two comments missed it.

    `deploy/k8s/worker.yaml` explained the `:53` wildcard-DNS gateway and the
    `NET_BIND_SERVICE` cap with "the pod-level sysctl above" and "the sysctl
    above" -- but `:64-72` and `test_no_low_port_window_survives_anywhere`
    both say that block is gone. Pinned so the stale sentence cannot return.
    """
    assert "pod-level sysctl above" not in K8S_WORKER
    assert "which uses the sysctl above" not in K8S_WORKER


def test_stack_env_example_describes_the_rollback_lever_not_a_canary() -> None:
    """`worker-2` is a per-node rollback lever, not a canary (docs §2.4.7).

    The anchor turns the pair on for every worker since 2026-09-16; the
    `*_WORKER2` overrides exist so one node can be taken back alone. The env
    example still said "the canary turns it on for worker-2 only" and shipped
    `false`, which reads as "the fleet default is off" -- the opposite of the
    manifests.
    """
    assert "the canary turns it on for worker-2 only" not in STACK_ENV_EXAMPLE
    # The stale sentence was line-wrapped in the file (`... Off by default; the`
    # / `# canary turns it on for worker-2 only`), so the literal above never
    # matched it: measured against f8f09b6's copy, only these three fire.
    assert "canary turns it on for worker-2 only" not in STACK_ENV_EXAMPLE
    assert "Off by default" not in STACK_ENV_EXAMPLE
    assert "E2B_ENABLE_NET_ISOLATION=false" not in STACK_ENV_EXAMPLE
    assert "E2B_FD_INJECT_CONNECT=false" not in STACK_ENV_EXAMPLE


# --------------------------------------------------------------------------
# N42 (2026-09-26) + ④'s seccomp: the last two manifest gaps, both closed by
# taking the factory's own files as the reference instead of a third literal.
# --------------------------------------------------------------------------

COMPOSE_TEST = (REPO / "deploy" / "compose" / "docker-compose.test.yml").read_text(
    encoding="utf-8"
)
MULTINODE_WORKERS = ("worker-1", "worker-2", "worker-3")
STACK_COMPOSE_PATH = REPO / "deploy" / "stack" / "docker-compose.prod.yml"
COMPOSE_MULTINODE_PATH = REPO / "deploy" / "compose" / "docker-compose.multinode.yml"
SHIPPED_SECCOMP_PROFILE = REPO / "deploy" / "seccomp" / "sandlock-worker.json"


def _stack_worker_block() -> str:
    """The stack's `worker-1: &worker` anchor, up to `worker-2:`."""
    return STACK_COMPOSE.split("\n  worker-1: &worker", 1)[1].split(
        "\n  worker-2:", 1
    )[0]


def _seccomp_directive(text: str) -> str:
    """The single `- seccomp=...` line of a compose service, stripped."""
    hits = [
        line.strip() for line in text.splitlines() if line.strip().startswith("- seccomp=")
    ]
    assert len(hits) == 1, f"expected exactly one seccomp= line: {hits}"
    return hits[0]


def _compose_seccomp_profile_path(directive: str, compose_file: Path) -> Path:
    """The file a compose `seccomp=` directive resolves to, from *its* file's dir.

    The directive is the stack's fixed shape -- `E2B_SECCOMP_PROFILE` with the
    checkout-relative default -- so this only has to read the default out and
    anchor it at the compose file that carries the line. Both stack and
    multinode spell `../seccomp/sandlock-worker.json` from directories that sit
    one level under `deploy/`, which is exactly why the same default text is
    correct in both files.
    """
    match = re.fullmatch(r"- seccomp=\$\{E2B_SECCOMP_PROFILE:-([^}]+)\}", directive)
    assert match, directive
    return (compose_file.parent / match.group(1)).resolve()


def _require_seccomp_filter_value(text: str) -> str | None:
    """The file's `E2B_REQUIRE_SECCOMP_FILTER` value, or None when it is unset."""
    values = [
        line.strip().split(":", 1)[1].strip().strip('"')
        for line in text.splitlines()
        if line.strip().startswith("E2B_REQUIRE_SECCOMP_FILTER:")
    ]
    assert len(values) <= 1, values
    return values[0] if values else None


def test_multinode_workers_mount_the_stacks_seccomp_profile() -> None:
    """④'s seccomp: the last `seccomp=unconfined` in the tree, removed.

    `deploy/compose/docker-compose.multinode.yml` ran all three workers under
    `seccomp=unconfined` -- the shape the factory replaced on 2026-09-15
    (`deploy/stack/docker-compose.prod.yml:323-328`, rationale in
    `deploy/seccomp/README.md`). It is not a cosmetic difference: the worker
    checks its own filter as the first step of `create_app`
    (`envd_service/config.py::check_seccomp_filter`) and `Seccomp: 0` is
    `SECCOMP_FILTER_MISSING`, fatal unless `E2B_REQUIRE_SECCOMP_FILTER=0` --
    so this file's workers could not serve at all under the shipped default.

    The reference is parsed, not copied: each manifest's directive is resolved
    against *its own* directory and the two have to land on the same file on
    disk. A hand-copied literal would keep passing after a path stopped
    resolving to the shipped profile.
    """
    stack_directive = _seccomp_directive(_stack_worker_block())
    shipped = SHIPPED_SECCOMP_PROFILE.resolve()
    assert _compose_seccomp_profile_path(stack_directive, STACK_COMPOSE_PATH) == shipped
    for name in MULTINODE_WORKERS:
        directive = _seccomp_directive(_multinode_worker_block(name))
        assert directive == stack_directive, name
        assert (
            _compose_seccomp_profile_path(directive, COMPOSE_MULTINODE_PATH) == shipped
        ), name
    assert "- seccomp=unconfined" not in COMPOSE_MULTINODE


def test_multinode_workers_keep_the_stacks_seccomp_requirement() -> None:
    """`E2B_REQUIRE_SECCOMP_FILTER` is a product decision, and the stack makes it by silence.

    The stack never sets the key, so the envd default (`True`) applies: a worker
    that is not running the shipped filter refuses to serve instead of quietly
    serving unfiltered. Only the shapes that are unfiltered *on purpose* opt out
    (`deploy/compose/docker-compose.test.yml`: `"0"`, the test runner). ④ has no
    such reason, so it has to spell the same thing -- and "the same thing" is
    judged by comparing the two files' values, so a later factory change (both
    spelling `"1"`, say) stays honest instead of pinning today's literal.
    """
    # Positive control: the helper does see an opt-out where one exists.
    assert _require_seccomp_filter_value(COMPOSE_TEST) == "0"
    assert _require_seccomp_filter_value(COMPOSE_MULTINODE) == (
        _require_seccomp_filter_value(STACK_COMPOSE)
    )


def test_k8s_worker_carries_the_fleets_network_flag() -> None:
    """N42: `allowInternetAccess` was silently inert on the cluster.

    `envd_service/executors/sandlock.py` gates every net-allow rule on
    `self._allow_internet_access and self._enable_network`, and
    `E2B_ENABLE_NETWORK` defaults to false (`envd_service/config.py`) -- so the
    manifest that never declared it shipped a worker where the SDK's
    `allowInternetAccess=True` was accepted and ignored. Measured 2026-09-26 on
    the live cluster (`docs/open-issues.md` N42): `1.1.1.1:443` ->
    `PermissionError [Errno 13]`, `pypi.org:443` -> `gaierror [Errno -3]`.

    The value is read off the stack's worker anchor, and separately pinned to
    the decided posture ("全开", 2026-09-26): a fleet that flipped the stack to
    `"false"` would be a *different* ruling, and this test should stop rather
    than follow it silently. What the flag buys is exactly the compose stacks'
    semantics -- a sandbox egresses only where its own request allows it.
    """
    fleet_value = _value_after_key(
        _stack_worker_block(), "E2B_ENABLE_NETWORK", indent="      "
    )
    assert fleet_value == "true"
    assert _k8s_env_value(K8S_WORKER, "E2B_ENABLE_NETWORK") == fleet_value


def test_multinode_workers_carry_the_fleets_network_flag() -> None:
    """N42 at site ④: without the key the wildcard/allowOut rules never reach the fork.

    Same wall as the k8s worker's (its own comment names the missing key): no
    policy reaches the sandbox, no DNS gateway is created, and the sandbox
    resolves against the image's own resolver. There is no anchor in this file
    -- each worker repeats its env block -- so all three are asserted.
    """
    fleet_value = _value_after_key(
        _stack_worker_block(), "E2B_ENABLE_NETWORK", indent="      "
    )
    assert fleet_value == "true"
    for name in MULTINODE_WORKERS:
        assert (
            _value_after_key(
                _multinode_worker_block(name), "E2B_ENABLE_NETWORK", indent="      "
            )
            == fleet_value
        ), name


def test_k8s_control_plane_hosts_no_sandboxes_so_the_flag_is_left_out() -> None:
    """Why `deploy/k8s/control-plane.yaml` carries no `E2B_ENABLE_NETWORK`.

    The rule is "the key goes where it is read", and this pod reads neither
    copy: `control_plane/config.py` declares an `enable_network` field that
    nothing in `control_plane/` consumes (`rg enable_network control_plane/`
    finds the field and no reader -- it came along when the module was created
    whole on 2026-08-29), and the envd app that the merged image embeds is only
    reached through `create_gateway()`, which builds no `Settings` and creates
    no sandboxes. The envd copy is the one that gates the policy, and it lives
    in the worker pods (`envd_service/executors/factory.py`).

    `E2B_ENABLE_LOCAL_NODE=false` is what makes that safe to leave out: with a
    local node this pod would provision sandboxes itself and the missing key
    would be N42 again. Both facts are pinned together, so the day the first
    one flips this test fails and the author has to add the key.
    """
    assert _k8s_env_value(K8S_CONTROL_PLANE, "E2B_ENABLE_LOCAL_NODE") == "false"
    assert "\n            - name: E2B_ENABLE_NETWORK\n" not in K8S_CONTROL_PLANE


#: The three workloads that must carry the internal key's rotation window.
#: `control-plane` and the worker *verify* `X-Internal-Key` (the worker's
#: gateway and its agent both read the list); the autoscaler only *sends* one,
#: and it reads the single slot under its own `E2B_AS_*` name -- the pair is
#: asserted together so a copy of the secret key cannot be dropped silently.
INTERNAL_KEY_WORKLOADS = (
    ("Deployment", "control-plane", "E2B_INTERNAL_API_KEY"),
    ("StatefulSet", "e2b-worker", "E2B_INTERNAL_API_KEY"),
    ("Deployment", "autoscaler", "E2B_AS_INTERNAL_API_KEY"),
)


def _env_of(workload: dict) -> dict:
    """Every container's env by name (sidecars included), as written."""
    env: dict = {}
    for container in workload["spec"]["template"]["spec"]["containers"]:
        for entry in container.get("env") or []:
            env[entry["name"]] = entry
    return env


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_all_three_workloads_can_accept_an_old_and_a_new_internal_key() -> None:
    """O3 Task 3: the internal key rotates in two windows, so every reader of
    `X-Internal-Key` must carry `E2B_INTERNAL_API_KEYS` beside the single slot.

    Why this is machine-checked rather than left to the runbook: the window is
    what keeps a rotation from breaking the fleet. Rotate writes the old key
    into the list and the new one into `E2B_INTERNAL_API_KEY`; a workload that
    reads only the single slot would 401 the moment a *peer* rolled -- before
    its own restart -- which is exactly the outage the list exists to prevent.

    `optional: true` is deliberate: outside a rotation window the key is absent
    from the Secret, and a required `secretKeyRef` would leave the pod in
    `CreateContainerConfigError` (the kustomize render is the only thing that
    notices, since unit tests cannot ask a cluster). The single slot stays
    non-optional in the same object: with no window list, it *is* the key.
    """
    rendered = subprocess.run(
        [KUBECTL, "kustomize", str(REPO / "deploy" / "k8s-k0s")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    for kind, name, single in INTERNAL_KEY_WORKLOADS:
        env = _env_of(_rendered_workload(rendered.stdout, kind, name))
        assert "E2B_INTERNAL_API_KEYS" in env, f"{name} 缺双窗键位"
        assert env["E2B_INTERNAL_API_KEYS"] == {
            "name": "E2B_INTERNAL_API_KEYS",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "e2b-secrets",
                    "key": "E2B_INTERNAL_API_KEYS",
                    "optional": True,
                }
            },
        }, name
        assert env[single]["valueFrom"]["secretKeyRef"] == {
            "name": "e2b-secrets",
            "key": "E2B_INTERNAL_API_KEY",
        }, name


def _without_comment_lines(text: str) -> str:
    """The text minus every line whose first non-blank character is `#`.

    The N36 alignment left prose that *names* the knob (what was measured, and
    why the window is gone) in all three files this test reads. A manifest that
    only mentions the window inside a comment declares no window -- which is
    the thing being judged here -- and the declaration lines themselves are
    asserted by the shape tests above, so dropping comments cannot let one back
    in.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_only_the_arm_lane_keeps_a_low_port_window() -> None:
    """N36 closed 2026-09-26: the window survives in exactly one place.

    The three deployment-ish sites (compose prod example, local pool, compose
    multinode example) were aligned with the fleet; the aarch64 lane's
    `guest-prep.sh` keeps its one-shot window because the Rust suites still run
    the shared-netns shape as uid 501 and cannot drop it (measured; see that
    file's comment and docs/open-issues.md N36).

    The three aligned files are read comment-stripped: the 2026-09-26
    measurement is written down *in* those files, so the knob's name survives
    there as prose (and the raw substring would fail on nothing but comments).
    """
    lane = (REPO / "deploy" / "scripts" / "arm-lane" / "guest-prep.sh").read_text(
        encoding="utf-8"
    )
    pool = (REPO / "autoscaler" / "backends" / "local.py").read_text(encoding="utf-8")
    assert "net.ipv4.ip_unprivileged_port_start=0" in lane
    assert "ip_unprivileged_port_start" not in _without_comment_lines(COMPOSE_PROD)
    assert "ip_unprivileged_port_start" not in _without_comment_lines(COMPOSE_MULTINODE)
    assert "ip_unprivileged_port_start" not in _without_comment_lines(pool)
    # The k8s pod-level window is gone (N5) and must stay gone.
    assert POD_SYSCTL not in K8S_WORKER


# --------------------------------------------------------------------------
# O3 Task 4 (2026-09-26): the two credentials with NO rotation window --
# `E2B_REDIS_PASSWORD` and `E2B_QUOTA_AGENT_TOKEN`.
#
# The ruling that shapes both pins is in `docs/superpowers/plans/
# 2026-09-26-decisions.md` #5 plus the same-day O3 second-round addendum:
# redis keeps ONE `--requirepass` and the 10-30 s interruption is *accepted*
# (the ACL double-user shape stays an alternative in the runbook, not code),
# and the api/internal keys are a separate, already-two-windowed matter
# (Task 3, `8d4c83c`). `deploy/k8s/redis.yaml` therefore does not move -- what
# moves is that the interruption and the "auth comes from the Secret" shape
# are written down and pinned, because an edit that inlines a password (or
# drops the `secretKeyRef`) would make the runbook's rotation steps impossible
# while every other test in this file stayed green.
# --------------------------------------------------------------------------

K8S_REDIS = (REPO / "deploy" / "k8s" / "redis.yaml").read_text(encoding="utf-8")
RUNBOOK = (REPO / "docs" / "k8s-deployment.md").read_text(encoding="utf-8")

#: 表 3's two rows, verbatim from the runbook (single source: the doc is
#: asserted against these strings rather than against key phrases, so a
#: rewording cannot silently drop a step or the window).
REDIS_TABLE_ROW = (
    "| `E2B_REDIS_PASSWORD` | ① 排维护窗口 ② `deploy/k8s-k0s/secrets.sh --rotate "
    "E2B_REDIS_PASSWORD` ③ `kubectl -n sandlock rollout restart deploy/redis` "
    "④ `kubectl -n sandlock rollout restart deploy/control-plane deploy/autoscaler`"
    "（读 redis 的只有 control-plane；`autoscaler` 是控制面客户端，跟着滚是形状对齐）"
    "⑤ 从 Secret 里读新口令验收（见下） | **必然有 10–30 s 中断**：redis 带着新口令重启、到 "
    "control-plane 滚动完拿到新口令之间，共享后端（配额 / 节点视图 / 限流 / 单飞）不可用 ⇒ "
    "**建箱、路由、sandbox 记录查询全部失败**；沙箱进程本身不经过 redis，**不受影响**；"
    "`appendonly yes` ⇒ 重启从 AOF 装载，**数据不丢**。**窗口不可逆**：② 之后旧口令只活在仍"
    "在跑的 redis 进程内存里，要回去只能再轮换一次（表 3 没有 finalize 那种安全位） | "
    "**ACL 双用户**（2026-09-26 裁定**不采纳**，只作备选）：`ACL SETUSER` 建新用户 → "
    "control-plane 切到 `redis://<新用户>:<新口令>@...` → 滚动 → 删旧用户 ⇒ "
    "**零停机**。代价：要改 redis 的启动方式（`--aclfile` 或启动期 `ACL SETUSER`），且"
    "用户必须持久化，否则重启就丢 |"
)
QUOTA_TABLE_ROW = (
    "| `E2B_QUOTA_AGENT_TOKEN` | 同时更新 worker 与 agent 的 Secret；**先重启 agent、再滚 "
    "worker**（顺序反了 worker 找不到 agent，但 worker 侧是降级的） | 单 token、启动即 "
    "fail-fast（`deploy/quota_agent/__main__.py:15-19`），**没有双窗**；worker 重启 = "
    "**杀沙箱**（同表 2 第 3 步） | ⚠ **k8s 形态今天没有部署 quota-agent**"
    "（`docs/production-deployment-requirements.md` §2.4.4 W4）⇒ 现在**没有影响面**，本轮只"
    "记账。将来部署 agent 时必须**同时**设计双 token（列表 + 旧值窗口），别把这条留到上线"
    "当天 |"
)


def _k8s_redis_container() -> dict:
    """The redis Deployment's single container, parsed rather than grepped."""
    documents = [doc for doc in yaml.safe_load_all(K8S_REDIS) if doc]
    assert [doc["kind"] for doc in documents] == ["Deployment", "Service"]
    containers = documents[0]["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _k8s_control_plane_env() -> dict:
    """`control-plane`'s env by name, from the Deployment document only."""
    deployments = [
        doc
        for doc in yaml.safe_load_all(K8S_CONTROL_PLANE)
        if doc and doc.get("kind") == "Deployment"
    ]
    assert len(deployments) == 1
    return _env_of(deployments[0])


def test_k8s_redis_auth_comes_from_the_secret_not_a_literal() -> None:
    """O3 Task 4: redis is single-user, and that one user's secret is the Secret.

    The 2026-09-26 ruling accepted a 10-30 s interruption instead of the ACL
    double-user shape, so `deploy/k8s/redis.yaml` keeps exactly one
    `--requirepass`. What the rotation depends on is *where that value comes
    from*: the directive argument is `$(REDIS_PASSWORD)`, which kubelet can
    only expand from the container's own env, and that env is a
    `secretKeyRef` into `e2b-secrets/E2B_REDIS_PASSWORD` -- the same Secret
    object and the same key the control plane interpolates into
    `E2B_REDIS_URL`. So a rotation is one write to the Secret plus the two
    rollouts in the runbook's 表 3, and no manifest ever carries a plaintext
    password (writing one in would defeat both the rotation steps and the
    repo-wide "no credentials in manifests" rule).

    Adopting the runbook's ACL *alternative* is a re-decision: it changes this
    startup shape, and the two negative assertions at the end are the
    tripwire so it cannot arrive silently.
    """
    container = _k8s_redis_container()
    assert container["command"] == [
        "redis-server",
        "--appendonly",
        "yes",
        "--requirepass",
        "$(REDIS_PASSWORD)",
    ]
    # One directive, and its argument is a reference rather than a value.
    assert K8S_REDIS.count("--requirepass") == 1
    # The name in the command has to be this container's env -- kubelet
    # expands `$(NAME)` from the container's own environment and leaves an
    # unknown reference as literal text.
    assert container["env"] == [
        {
            "name": "REDIS_PASSWORD",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "e2b-secrets",
                    "key": "E2B_REDIS_PASSWORD",
                }
            },
        }
    ]
    # The reader side: one interpolation, from the same Secret key.
    cp_env = _k8s_control_plane_env()
    assert cp_env["E2B_REDIS_URL"] == {
        "name": "E2B_REDIS_URL",
        "value": "redis://:$(E2B_REDIS_PASSWORD)@redis:6379/0",
    }
    assert cp_env["E2B_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "e2b-secrets",
        "key": "E2B_REDIS_PASSWORD",
    }
    assert (
        container["env"][0]["valueFrom"]["secretKeyRef"]
        == cp_env["E2B_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]
    ), "redis 与 control-plane 必须读同一个 Secret 的同一个键"
    # No ACL shape has crept in: the ruling keeps this file untouched, and the
    # alternative lives in the runbook's 备选 column only.
    assert "--aclfile" not in K8S_REDIS
    assert "ACL SETUSER" not in K8S_REDIS


def test_the_runbook_carries_the_no_double_window_table() -> None:
    """O3 Task 4's deliverable: 表 3, where an operator reads the rotation.

    Tables 1 and 2 of `docs/k8s-deployment.md` §4.5 rotate in two windows
    because their consumers read "list ∪ single slot". These two credentials
    have no such list, so their table has to carry what the other two do not:
    the *interruption itself* (where it starts, who notices what, and that the
    window is not reversible), the `appendonly yes` evidence that no data is
    lost, and the two credentials' different sizes of blast radius -- the
    quota-agent token has none today because the k8s shape deploys no agent
    (`docs/production-deployment-requirements.md` §2.4.4 W4).
    """
    assert (
        "### 表 3：无双窗的凭据（`E2B_REDIS_PASSWORD` / `E2B_QUOTA_AGENT_TOKEN`）"
        in RUNBOOK
    )
    assert REDIS_TABLE_ROW in RUNBOOK
    assert QUOTA_TABLE_ROW in RUNBOOK
    # Table 3 stays with the other two tables, and the ruling it encodes is
    # spelled out (the ACL shape is the alternative, not the main path).
    assert RUNBOOK.index("### 表 1：") < RUNBOOK.index("### 表 2：")
    assert RUNBOOK.index("### 表 2：") < RUNBOOK.index("### 表 3：")
    assert RUNBOOK.index("### 表 3：") < RUNBOOK.index("### 4.5.1")
    assert (
        "**redis 接受 10–30 s 中断，不做 ACL 双用户**；ACL 版本只作**备选**记在表里，"
        "`deploy/k8s/redis.yaml` 不动。" in RUNBOOK
    )
    # The interruption, step by step: the operator has to know which rollout
    # must not be left hanging, and that the measured window is exactly this.
    assert (
        "- ② 之后、③ 之前：Secret 已是新口令、redis 进程内存里还是旧口令 —— **别在这时滚 "
        "CP**：新起的 pod 会拿着新口令连不上。②③ 连着做，不要停在中间。" in RUNBOOK
    )
    assert (
        "- ③ 之后、④ 滚完之前：redis 只认新口令，control-plane 内存里还是旧口令 ⇒ 共享后端"
        "认证失败，**这一段的时长就是那 10–30 s**（redis 重启 + control-plane 滚一轮；"
        "`autoscaler` 不读 redis，跟滚只是形状对齐）。" in RUNBOOK
    )
    assert (
        "- 这段窗口里谁会看到什么：`POST /sandboxes` 建箱失败、`GET /sandboxes/<id>` 等记录"
        "查询失败、路由查找失败（节点视图在 redis 里）、创建限流与单飞失效；沙箱**进程**本身"
        "照旧运行（不经过 redis），但控制面针对它的调用同样要等 redis 回来。" in RUNBOOK
    )
    # Acceptance reads the password out of the Secret instead of putting it on
    # a command line (the repo-wide "no plaintext in logs/reports" rule).
    assert (
        "    'redis-cli -a \"$REDIS_PASSWORD\" --no-auth-warning ping; redis-cli ping'"
        in RUNBOOK
    )
    # ...and the auth-from-Secret property is written down next to its pin.
    assert (
        "`tests/unit/test_worker_manifest_permissions.py::"
        "test_k8s_redis_auth_comes_from_the_secret_not_a_literal`" in RUNBOOK
    )
