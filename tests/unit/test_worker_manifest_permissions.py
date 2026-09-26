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
    # container, in the order the resolver uses them.
    assert "value: /var/lib/e2b-images /var/lib/e2b-sandboxes/_images\n" in K8S_WORKER


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
