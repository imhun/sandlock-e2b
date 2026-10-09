"""Task 3 of the control-plane uid relocation: the control plane moves to 65533.

The control plane ran as uid **65534** -- the workers' uid and the per-node
agent face A's uid -- in both shipped shapes, so a compromised agent could
ptrace it (`docs/security-audit/c3-agent-hostpid-2026-10-09.md` §5). Task 2 took
the shared `_images/_oci/` tar reclaim away from the worker, which is what made
`_images` read-only for the worker and let the control plane keep its own uid.
Task 3 is the manifest half of that move
(`docs/security-audit/control-plane-uid-2026-10-09.md`):

* the k8s control-plane pod's main container runs as 65533 (the workers and the
  agent stay 65534; the per-sandbox uid pool is untouched);
* the compose lane's control plane runs as 65533 -- and, because that lane has
  **two** volumes and its root `image-cache-init` one-shot *asserts* the owner
  of everything it hands over, its hand-over targets are split by volume (the
  module `Rule R4` note below);
* the worker manifest says, where the OCI directory is named, that `_images` is
  read-only for the worker and that the two OCI knobs must stay unset.

Rule R4, which this file also pins: `deploy/compose/docker-compose.multinode.yml`
mounts `control-data` into the `control-plane` service alone (and into the init
at `/cache/control`), and `worker-data` into `worker-1/2/3` and the root
`c3-agent-maint` (the init sees it at `/cache/worker`). The init's hand-over
targets and their `owner ==` assertions therefore have to follow the **volume**
they live on: the control plane's at 65533, the workers' at 65534. A target
under neither root is refused by name rather than guessed at.

A comment cannot buy a capability or a uid back, so the k8s half is asserted on
the parsed manifest (and the compose half on the parsed YAML plus the script's
own behaviour), exactly like the neighbouring manifest tests.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
K8S = DEPLOY / "k8s"
CONTROL_PLANE_MANIFEST = K8S / "control-plane.yaml"
WORKER_MANIFEST = K8S / "worker.yaml"
AGENT_MANIFEST = K8S / "c3-agent.yaml"
COMPOSE_MULTINODE = DEPLOY / "compose" / "docker-compose.multinode.yml"

#: The new uid, in both shipped shapes. 65533 is deliberately adjacent to the
#: workers' 65534 -- both are "nobody-shaped" -- but it is a *different* uid,
#: which is the whole property: the agent face A (65534) can no longer ptrace
#: the control plane, and neither can a worker.
CONTROL_PLANE_UID = 65533
WORKER_UID = 65534

SHARED_ROOT = "/var/lib/e2b-sandboxes"
#: The worker's own rootfs cache: a node-local hostPath, and it stays the
#: worker's (the control plane never writes it -- see §4.1 of the spec).
NODE_LOCAL_CACHE = "/var/lib/e2b-images"

#: The two OCI-reclaim knobs. Unset in both shipped shapes, and they must stay
#: that way: the control plane's reclaim reads `<platform_root>/_images/...`
#: (Task 2's `reclaim_oci_artifacts`), so a shape that opened the knobs would
#: give the worker a write position in `_oci/` again -- and the worker-side
#: fallback prune no longer exists.
OCI_KNOBS = ("E2B_IMAGE_CACHE_OCI_MAX_BYTES", "E2B_IMAGE_CACHE_OCI_STALE_S")

#: The Task 2 precondition, spelled the same in both manifests, next to the
#: variable that can break it. Pinned as the whole stripped comment block, in
#: order: this is documentation, but it is the only place that says why
#: `E2B_IMAGE_OCI_DIR` is load-bearing, and a paragraph that quietly lost its
#: "silent no-op" half would read like a style note.
OCI_PRECONDITION_BLOCK = (
    "Task 3 precondition: this is the OCI tar store, and it is",
    "read-only for the worker (0755 directories, 0644 tars, read +",
    "traverse, no shared group). Task 2's reclaim derives",
    "`<platform_root>/_images` from the export root, so a shape that",
    "points E2B_IMAGE_OCI_DIR anywhere else turns that reclaim into",
    "a silent no-op -- the worker-side fallback prune is gone.",
    "E2B_IMAGE_CACHE_OCI_MAX_BYTES / E2B_IMAGE_CACHE_OCI_STALE_S stay",
    "unset in both manifests: either one needs the worker to write",
    "`_oci/` again, which is exactly the shape Task 2 removed.",
)

#: The migration the k8s comment above the uid is supposed to point at.
UID_MIGRATION_DOC = "docs/security-audit/control-plane-uid-2026-10-09.md"


def _load_all(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _only(docs: list[dict], kind: str, name: str) -> dict:
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == kind and doc["metadata"]["name"] == name
    ]
    assert len(matches) == 1, (kind, name, [d["metadata"]["name"] for d in matches])
    return matches[0]


def _containers(workload: dict) -> dict[str, dict]:
    return {c["name"]: c for c in workload["spec"]["template"]["spec"]["containers"]}


def _init_containers(workload: dict) -> dict[str, dict]:
    return {
        c["name"]: c
        for c in (workload["spec"]["template"]["spec"].get("initContainers") or [])
    }


def _env(container: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in (container.get("env") or [])}


def _commands(container: dict) -> list[str]:
    """The container's shell body, one stripped non-comment line each."""
    body = container["command"][2]
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _agent_daemonset() -> dict:
    return _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _compose_env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return {k: v for k, v in (e.split("=", 1) for e in env if "=" in e)}
    return env


def _prose_lines(path: Path) -> list[str]:
    """The file's lines, stripped, with a leading comment marker removed.

    So a pinned block is the *prose* the manifest carries, not its indentation
    or the `#` a comment happens to wear (the two manifests indent differently).
    """
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        lines.append(stripped)
    return lines


def _multinode_init() -> dict:
    return _compose(COMPOSE_MULTINODE)["services"]["image-cache-init"]


def _multinode_init_lines() -> list[str]:
    """The init's executable lines, compose's `$$` unescaped, comments dropped."""
    command = _multinode_init()["command"]
    assert isinstance(command, list) and len(command) == 1, command
    return [
        line.strip()
        for line in command[0].replace("$$", "$").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# ------------------------------------------------------------- the k8s half


def test_the_control_plane_container_runs_as_its_own_uid() -> None:
    """The deliverable: `runAsUser`/`runAsGroup` 65533 in the CP manifest.

    Both halves are one decision: `runAsGroup` 65534 (or 0) would have every
    record the control plane writes carry the workers' group, which is a
    different filesystem identity even when the uid is right.
    """
    deployment = _only(
        _load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane"
    )
    security = _containers(deployment)["control-plane"]["securityContext"]
    assert security["runAsUser"] == CONTROL_PLANE_UID
    assert security["runAsGroup"] == CONTROL_PLANE_UID


def test_the_worker_container_keeps_the_worker_uid() -> None:
    """...and the two uids really are two: the worker stays 65534.

    Without this half, "the control plane is 65533" would also hold in a shape
    where somebody had moved the worker instead, and the ptrace exposure the
    task exists for would be unchanged.
    """
    worker = _only(_load_all(WORKER_MANIFEST), "StatefulSet", "e2b-worker")
    security = _containers(worker)["worker"]["securityContext"]
    assert security["runAsUser"] == WORKER_UID
    assert CONTROL_PLANE_UID != WORKER_UID


def test_the_control_plane_container_declares_no_capability() -> None:
    """CAP-EFF-0: moving off 65534 must not buy the access back with caps.

    A non-root container process with no added capability set is the shape the
    spec's §7 closing criterion measures (`CapEff` stays 0). The pin is on the
    parsed `securityContext` having no `capabilities` block at all: that covers
    "do not add `cap_add`", and it cannot be satisfied by adding a `cap_drop`
    that leaves one verb behind.
    """
    deployment = _only(
        _load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane"
    )
    security = _containers(deployment)["control-plane"]["securityContext"]
    assert "capabilities" not in security
    assert "cap_add" not in security
    assert security.get("privileged") is None


def test_the_worker_manifest_never_turns_the_two_oci_knobs_on() -> None:
    """The precondition `_images`-read-only rests on, in both manifests.

    Task 2 deleted the worker's `_oci` prune, so the knobs no longer have a
    consumer that could even honour them: a shape that set them would look like
    bounded growth while the directory grew without one. Unset is the value.
    """
    worker_env = _env(
        _containers(_only(_load_all(WORKER_MANIFEST), "StatefulSet", "e2b-worker"))[
            "worker"
        ]
    )
    deployment = _only(
        _load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane"
    )
    control_env = _env(_containers(deployment)["control-plane"])
    for knob in OCI_KNOBS:
        assert knob not in worker_env
        assert knob not in control_env


def test_the_worker_keeps_the_node_local_cache_and_the_shared_oci_dir() -> None:
    """The split itself, at the exact values: cache local, tars shared.

    `E2B_IMAGE_CACHE_MAX_BYTES` is the node-local cache's own bound and stays
    set (this manifest set ships no quota-agent, so it is the only bound the
    cache has); the *knobs two lines above* are the ones that stay unset.
    """
    worker_env = _env(
        _containers(_only(_load_all(WORKER_MANIFEST), "StatefulSet", "e2b-worker"))[
            "worker"
        ]
    )
    assert worker_env["E2B_IMAGE_CACHE_DIR"]["value"] == NODE_LOCAL_CACHE
    assert worker_env["E2B_IMAGE_OCI_DIR"]["value"] == f"{SHARED_ROOT}/_images"
    assert worker_env["E2B_IMAGE_CACHE_MAX_BYTES"]["value"] == "4294967296"


def test_both_manifests_spell_the_task_2_precondition() -> None:
    """The comment the brief asks for, pinned as one block in both files.

    It is text, not data -- but it is the one place a future editor can learn
    that `E2B_IMAGE_OCI_DIR` is load-bearing for Task 2's reclaim, so it is
    pinned the way the compose scripts' own lines are: exact, stripped, and in
    order (the block has to be one contiguous run of comment lines).
    """
    for path in (WORKER_MANIFEST, CONTROL_PLANE_MANIFEST):
        lines = _prose_lines(path)
        runs = [
            tuple(lines[index : index + len(OCI_PRECONDITION_BLOCK)])
            for index in range(len(lines))
        ]
        assert OCI_PRECONDITION_BLOCK in runs, path.name


def test_the_control_plane_uid_comment_points_at_the_migration() -> None:
    """The stale "the price is real" paragraph becomes "the price is paid".

    `deploy/k8s/control-plane.yaml` used to say the CP/worker uid coupling was
    the price of D24 and that buying it back needs a dedicated uid *and* a
    second migration package. This task is that purchase, so the paragraph has
    to name the new uid and the migration document -- a comment still selling
    the old trade is worse than no comment. Two lines, pinned stripped (the
    prose around them wraps freely): the sentence that carries the decision and
    the one that carries the pointer.
    """
    lines = _prose_lines(CONTROL_PLANE_MANIFEST)
    assert (
        "Task 3 (2026-10-09): the price is paid -- the control plane runs as 65533."
        in lines
    )
    assert f"{UID_MIGRATION_DOC} (Task 4)." in lines


# ------------------------------------------------- the k8s hand-over (R13)


def test_the_k8s_hand_over_gives_the_shared_root_to_the_control_plane() -> None:
    """R13: the shared export root is the control plane's, the node-local half the worker's.

    `c3-agent.yaml`'s `storage-init` is the root one-shot that prepares both
    roots, so the split is read off the **values the script is handed** and the
    uid each target's chown uses: the node-local rootfs cache keeps the worker's
    65534, while `_images` (Task 2: the worker only reads it now) and the
    control plane's own namespaces take 65533.
    """
    inits = _init_containers(_agent_daemonset())
    storage = _env(inits["storage-init"])
    assert storage["WORKER_UID"]["value"] == "65534"
    assert storage["CP_UID"]["value"] == "65533"
    assert storage["WORKER_CACHE_DIRS"]["value"].split() == [NODE_LOCAL_CACHE]
    assert storage["CP_CACHE_DIRS"]["value"].split() == [f"{SHARED_ROOT}/_images"]
    assert storage["CP_DIRS"]["value"].split() == [
        f"{SHARED_ROOT}/_builds",
        f"{SHARED_ROOT}/_secrets",
        f"{SHARED_ROOT}/_templates",
    ]
    assert storage["SHARED_ROOT"]["value"] == SHARED_ROOT
    lines = _commands(inits["storage-init"])
    for expected in (
        "for dir in $WORKER_CACHE_DIRS; do",
        'chown $WORKER_UID:$WORKER_UID "$dir" 2>/dev/null ||',
        'if [ "$owner" != "$WORKER_UID" ]; then',
        "for dir in $CP_CACHE_DIRS; do",
        'chown -R $CP_UID:$CP_UID "$dir/_oci" 2>/dev/null ||',
        'if [ "$owner" != "$CP_UID" ]; then',
        # The volume store is the control plane's, and its uid comes from
        # `$CP_UID` at the call site rather than from a literal.
        'hand_over "$volume_store" "$CP_UID" || exit 1',
        'hand_over "$volume_store/_meta" "$CP_UID" || exit 1',
        # ...and the node-local `secrets/` handling stays on the worker.
        'find "$dir/secrets" -mindepth 1 -maxdepth 2 -type d -exec chown $WORKER_UID:$WORKER_UID {} + 2>/dev/null || echo "storage-init: chown refused ($dir/secrets -mindepth 1 -maxdepth 2, NFS root_squash?) -- the per-sandbox secret dirs must belong to uid $WORKER_UID"',
    ):
        assert expected in lines, expected


def test_the_k8s_state_base_stays_the_workers() -> None:
    """`state` is the worker's, and only `state` (R13/R15).

    `state` keeps its old judgement -- owner 65534, or `1777` as the fallback
    for a storage that refuses a chown -- and it is now the *only* target of that
    block: `_migrate` left this container entirely (it is a two-identity
    directory, handed over with a group and an explicit mode by `storage-init`).
    """
    inits = _init_containers(_agent_daemonset())
    workspace = _env(inits["workspace-root-init"])
    assert workspace["WORKER_UID"]["value"] == "65534"
    assert workspace["CP_UID"]["value"] == "65533"
    lines = _commands(inits["workspace-root-init"])
    assert 'target="$state"' in lines
    assert 'uid="$WORKER_UID"' in lines
    assert 'chmod 1777 "$target" 2>/dev/null ||' in lines
    # Nothing here names `_migrate` any more: its hand-over (group + mode) is
    # `storage-init`'s, and a 1777-only-on-refusal chmod here is exactly the bug
    # R15 records (a fresh volume stayed 0755 and the worker's write got EACCES).
    assert [line for line in lines if "_migrate" in line] == []
    # `_snapshots`' ownership is *not* re-imposed here any more: storage-init is
    # the one container that hands it over (a 65534 chown here would take the
    # control plane's group back off).
    assert not [line for line in lines if line.startswith('chown 65534:65534 "$shared/_snapshots"')]


def test_the_k8s_migrate_staging_is_shared_the_same_way() -> None:
    """R15: `_migrate` is dual-written too, so it gets the group and an explicit mode.

    The control plane writes `_migrate/control-plane/<id>.tar.gz`
    (`gateway_common/paths.py::migrate_transfer_path`) and the **worker** writes
    `_migrate/<id>.tar.gz` in-process -- `envd_service/agent.py`'s export route
    (`mkdir` + `os.open(..., O_WRONLY|O_CREAT|O_TRUNC, 0o600)`, and the
    `unlink` in its streaming `finally`) and its import route do the same. That
    needs *write on the directory*, so the owner is the control plane and the
    worker's group gets the write bit -- which it has, because the worker is a
    member of 65533 (`supplementalGroups`).

    The mode is set **unconditionally**: the old shape chmodded `1777` only
    inside the "the chown was refused" branch, so a *fresh* volume (where the
    root `mkdir` had just made it 0755 and the chown succeeded) left the worker
    with EACCES on its first export. `2775` is preferred to `1777` because the
    latter would let any pooled sandbox uid plant or remove migration payloads.
    """
    inits = _init_containers(_agent_daemonset())
    storage = _env(inits["storage-init"])
    assert storage["MIGRATE_DIR"]["value"] == f"{SHARED_ROOT}/_migrate"
    assert storage["MIGRATE_OWNER"]["value"] == "65533:65533"
    assert storage["MIGRATE_MODE"]["value"] == "2775"
    lines = _commands(inits["storage-init"])
    for expected in (
        'hand_over_shared "$MIGRATE_DIR" "$MIGRATE_OWNER" "$MIGRATE_MODE" "the control plane writes control-plane/<id>.tar.gz and the worker writes <id>.tar.gz"',
        'mkdir -p "$dir"',
        'chown "$owner" "$dir" 2>/dev/null ||',
        'chmod "$mode" "$dir" 2>/dev/null ||',
        'if [ "$shared_owner:$shared_group" != "$owner" ] || [ "$shared_mode" != "$mode" ]; then',
    ):
        assert expected in lines, expected
    # The mode is not inside "the chown was refused" branch: it is applied on
    # every run, whatever the directory looked like before.
    chmod_index = lines.index('chmod "$mode" "$dir" 2>/dev/null ||')
    chown_index = lines.index('chown "$owner" "$dir" 2>/dev/null ||')
    assert chown_index < chmod_index
    assert 'chmod 1777 "$MIGRATE_DIR"' not in "\n".join(lines)


def test_the_k8s_snapshot_store_is_shared_and_its_old_children_are_swept() -> None:
    """`_snapshots` = `65534:65533` mode `2775`, and the pre-existing ids too.

    The worker writes `<id>/fs.tar` and the control plane writes
    `<id>/snapshot.json` **inside the same directory**, so this target keeps the
    worker as its owner, takes the control plane's group, and carries the
    group-write + setgid bits that the two creators then reproduce on each
    `<id>/`. The mode is set here because `migrate-state-owner.sh` is pinned to
    chown-only.

    Fix round 3 (finding 2): the `<id>/` directories that predate this are
    `65534:65534 0755`, and a 0755 directory the control plane does not own makes
    its `SnapshotRegistry.delete()` a silent no-op (`rmtree(..., ignore_errors=True)`
    cannot unlink inside it, so the `snapshot.json` survives and the id keeps
    being listed). The same hand-over therefore sweeps the store's **direct
    children** -- owner, group *and* mode -- and it must not reach further: the
    payload files below belong to whoever wrote them.
    """
    inits = _init_containers(_agent_daemonset())
    storage = _env(inits["storage-init"])
    assert storage["SNAPSHOT_DIR"]["value"] == f"{SHARED_ROOT}/_snapshots"
    assert storage["SNAPSHOT_OWNER"]["value"] == "65534:65533"
    assert storage["SNAPSHOT_MODE"]["value"] == "2775"
    lines = _commands(inits["storage-init"])
    for expected in (
        'hand_over_shared "$SNAPSHOT_DIR" "$SNAPSHOT_OWNER" "$SNAPSHOT_MODE" "the worker writes <id>/fs.tar and the control plane writes <id>/snapshot.json inside the same directory"',
        # The sweep: direct children only, one level, owner+group+mode.
        'for child in "$SNAPSHOT_DIR"/*; do',
        '[ -d "$child" ] || continue',
        'chown "$SNAPSHOT_OWNER" "$child" 2>/dev/null ||',
        'chmod "$SNAPSHOT_MODE" "$child" 2>/dev/null ||',
        'if [ "$child_owner:$child_group" != "$SNAPSHOT_OWNER" ] || [ "$child_mode" != "$SNAPSHOT_MODE" ]; then',
    ):
        assert expected in lines, expected
    script = "\n".join(lines)
    # One level: nothing recursive into the payloads below the `<id>` children.
    assert 'chown -R "$SNAPSHOT_OWNER"' not in script
    assert 'chmod -R "$SNAPSHOT_MODE"' not in script


def test_the_k8s_worker_pod_joins_the_control_planes_group() -> None:
    """The worker must be a *member* of 65533 to write where the CP made a dir.

    `supplementalGroups` is the pod-level k8s spelling (the spec's §5.4 names
    it). The worker's own uid/gid stay 65534/65534 -- the control plane's
    trusted identity source reads exactly those, and a pod-level `runAsUser`
    would override the container's -- and the control plane needs no entry of
    its own, because 65533 is already its `runAsGroup`.
    """
    worker = _only(_load_all(WORKER_MANIFEST), "StatefulSet", "e2b-worker")
    pod = worker["spec"]["template"]["spec"]
    assert pod["securityContext"]["supplementalGroups"] == [65533]
    security = _containers(worker)["worker"]["securityContext"]
    assert security["runAsUser"] == 65534
    assert security["runAsGroup"] == 65534
    assert "runAsUser" not in pod["securityContext"]
    assert "runAsGroup" not in pod["securityContext"]
    control_plane = _only(
        _load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane"
    )
    cp_security = _containers(control_plane)["control-plane"]["securityContext"]
    assert cp_security["runAsGroup"] == 65533


# --------------------------------------------------------- the compose half


def test_the_compose_control_plane_runs_as_its_own_uid() -> None:
    """`user: "65533:65533"`, with every other identity where it was.

    The three workers and agent face A stay 65534 (the file-capability and
    `uid_map` rules are theirs, not the control plane's), and face B stays 0:0
    (NFS AUTH_SYS only honours root for `chown`).
    """
    services = _compose(COMPOSE_MULTINODE)["services"]
    assert services["control-plane"]["user"] == "65533:65533"
    assert services["c3-agent"]["user"] == "65534:65534"
    for worker in ("worker-1", "worker-2", "worker-3"):
        assert services[worker]["user"] == "65534:65534"
    assert services["c3-agent-maint"]["user"] == "0:0"
    assert services["image-cache-init"]["user"] == "0:0"
    assert "cap_add" not in services["control-plane"]


#: The two compose stacks that put the control plane and the workers on **one**
#: volume (`sandbox-shared`), and therefore keep the control plane on the
#: workers' 65534 (`docs/open-issues.md` N94). The reason is one directory, and
#: it is pinned below rather than only described in the comment: the control
#: plane's cache/OCI store and the worker's rootfs cache are the same path on
#: the same volume, and *both* write it.
SINGLE_VOLUME_STACKS = (
    REPO / "deploy" / "compose" / "docker-compose.prod.yml",
    REPO / "deploy" / "stack" / "docker-compose.prod.yml",
)

SHARED_MOUNT = "sandbox-shared:/var/lib/e2b-sandboxes"


@pytest.mark.parametrize("path", SINGLE_VOLUME_STACKS, ids=lambda p: p.parent.name)
def test_a_single_volume_compose_stack_keeps_the_control_plane_on_65534(
    path: Path,
) -> None:
    """The ruled disposition for the other two compose shapes (N94).

    Characterization pin, not a RED->GREEN one: it asserts facts that already
    hold, and it exists so the *reason* written next to `user:` and in N94
    cannot go silently stale. If a later change moves the worker's rootfs cache
    off this volume (the k8s split) or gives the two identities a shared group,
    this goes red -- which is exactly the moment the control plane here can be
    moved off 65534.
    """
    services = _compose(path)["services"]
    control_env = _compose_env(services["control-plane"])
    worker_env = _compose_env(services["worker-1"])
    assert services["control-plane"]["user"] == "65534:65534"
    # One directory, written by both identities:
    assert control_env["E2B_IMAGE_CACHE_DIR"] == "/var/lib/e2b-sandboxes/_images"
    assert worker_env["E2B_IMAGE_CACHE_DIR"] == control_env["E2B_IMAGE_CACHE_DIR"]
    # ...and the same directory is the control plane's OCI store here, because
    # neither service names a separate one.
    assert "E2B_IMAGE_OCI_DIR" not in control_env
    assert "E2B_IMAGE_OCI_DIR" not in worker_env
    # ...on one volume, mounted into both.
    assert SHARED_MOUNT in services["control-plane"]["volumes"]
    assert SHARED_MOUNT in services["worker-1"]["volumes"]


def test_the_compose_hand_over_declares_one_uid_per_volume() -> None:
    """R4's shape: the two volumes, and the uid each one's writer runs as.

    The uid a target is handed to comes from the **volume** it lives on, and
    the init's own declaration says which volume is which: `control-data` (the
    only volume the `control-plane` service mounts, seen here as
    `/cache/control`) is the control plane's 65533, `worker-data` (worker-1/2/3
    and the root `c3-agent-maint`, seen here as `/cache/worker`) is the
    workers' 65534.
    """
    env = _compose_env(_multinode_init())
    assert env["CONTROL_ROOT"] == "/cache/control"
    assert env["CONTROL_UID"] == "65533"
    assert env["WORKER_ROOT"] == "/cache/worker"
    assert env["WORKER_UID"] == "65534"
    # ...and the target lists keep exactly the paths they had: the split is a
    # *uid* question, not a re-arrangement of what gets prepared.
    assert env["CACHE_DIRS"].split() == [
        "/cache/control/_images",
        "/cache/worker/_images",
    ]
    assert env["OWNED_DIRS"].split() == [
        "/cache/control/_builds",
        "/cache/control/_secrets",
        "/cache/control/_snapshots",
        "/cache/control/_templates",
        "/cache/control/_migrate",
        "/cache/worker/_snapshots",
        "/cache/worker/_migrate",
    ]
    assert env["WRITABLE_ROOTS"].split() == ["/cache/control", "/cache/worker"]
    assert env["VOLUME_STORES"].split() == ["/cache/control/_volumes"]


def test_the_compose_hand_over_reads_the_uid_from_the_volume() -> None:
    """The script half of R4, pinned as exact lines.

    Every target's uid comes from `uid_of`, which maps the volume root to its
    uid; the `case`'s third arm is the refusal (a path under neither root is
    never handed to a guessed uid), and the assertions that follow stat the
    target and compare it against that uid -- not against a literal.
    """
    lines = _multinode_init_lines()
    for expected in (
        "uid_of() {",
        '"$CONTROL_ROOT"|"$CONTROL_ROOT"/*) uid="$CONTROL_UID" ;;',
        '"$WORKER_ROOT"|"$WORKER_ROOT"/*) uid="$WORKER_UID" ;;',
        '*) uid="" ;;',
        'refuse_foreign() {',
        'uid_of "$dir"',
        '[ -n "$uid" ] || refuse_foreign "$dir"',
        'chown "$uid:$uid" "$dir" 2>/dev/null ||',
        'chown -R "$uid:$uid" "$dir/_oci" 2>/dev/null ||',
        'if [ "$owner" != "$uid" ]; then',
        'uid_of "$target"',
        '[ -n "$uid" ] || refuse_foreign "$target"',
        'chown "$uid:$uid" "$target" 2>/dev/null ||',
    ):
        assert expected in lines, expected
    # The literal may not come back at a hand-over site: a target that still
    # says 65534 is exactly the regression R4 is about.
    for line in lines:
        assert "chown 65534" not in line, line


def test_the_compose_hand_over_hands_each_volume_to_its_own_uid(
    tmp_path: Path,
) -> None:
    """R4, behaviourally: the two volumes part company in one run.

    The script is run for real against a two-tree probe (the control plane's
    volume and the workers'), with `stat`/`chown` shims because its own tools
    are Linux-shaped and macOS's `stat` has no `-c`. The ownership model is the
    one the deployment has: everything under `/cache/control` (here the probe's
    control tree) is 65533, everything under `/cache/worker` is 65534 -- and,
    because the script re-stats every target after the attempt, a run that
    handed one of them to the other uid cannot print these lines.
    """
    result = _run_multinode_init(tmp_path)

    control = tmp_path / "control"
    worker = tmp_path / "worker"
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        f"image-cache-init: {control}/_images is owned by uid 65533",
        f"image-cache-init: {worker}/_images is owned by uid 65534",
        f"image-cache-init: {control}/_secrets is owned by uid 65533",
        f"image-cache-init: {worker}/_snapshots is owned by uid 65534",
        f"image-cache-init: {control} is writable by uid 65533 (owner=65533 mode=755)",
        f"image-cache-init: {worker} is writable by uid 65534 (owner=65534 mode=755)",
        f"image-cache-init: {control}/_volumes already belongs to uid 65533 "
        "(mode 755) -- nothing to do",
        f"image-cache-init: {control}/_volumes/_meta already belongs to uid 65533 "
        "(mode 755) -- nothing to do",
        f"image-cache-init: {control}/_volumes is owned by uid 65533 (the volume "
        "data directories below it are left alone)",
    ]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("root", "expected_uid"),
    (("control", "65533"), ("worker", "65534")),
)
def test_a_refused_cache_dir_is_a_fatal_that_names_the_path_and_uid(
    tmp_path: Path, root: str, expected_uid: str
) -> None:
    """The gate survives the split, for **both** sides.

    A target must still be verified after the chown, and a refusal must still
    stop the deployment by name. Both arms matter: the control plane's target
    is the one that moved, and the workers' is the one that must not have.
    Each arm refuses *its own* root's cache directory, so the uid in the FATAL
    is the one that volume's writer runs as.
    """
    result = _run_multinode_init(tmp_path, refuse=f"{root}/_images")

    refused = tmp_path / root / "_images"
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        f"image-cache-init: FATAL: {refused} is owned by uid 0, not uid "
        f"{expected_uid} (the uid of the volume it lives on): a {expected_uid} "
        "writer cannot create its lock files or staging trees there, so every "
        "image resolve would fail",
        "image-cache-init: fix it once (as root on the node, or on the NFS "
        f"server): chown {expected_uid}:{expected_uid} {refused} && chown -R "
        f"{expected_uid}:{expected_uid} {refused}/_oci",
    ]
    # The refused chown says so (on stdout, where the loop's own line goes) and
    # no success line for that directory exists anywhere.
    assert result.stdout.splitlines()[-1] == (
        f"image-cache-init: chown refused ({refused}, NFS root_squash?) -- "
        "verifying the owner instead"
    )
    for line in result.stdout.splitlines():
        assert not line.startswith(f"image-cache-init: {refused} is owned by"), line


def test_a_refused_volume_store_is_a_fatal_that_names_the_path_and_uid(
    tmp_path: Path,
) -> None:
    """...and the D24 volume store's own gate is still there, on the CP's uid.

    The store is the target the old script's one-uid form handed to 65534; in
    this shape it lives on `control-data`, and it is the target the control
    plane cannot work without.
    """
    result = _run_multinode_init(tmp_path, refuse="control/_volumes/_meta")

    refused = tmp_path / "control" / "_volumes" / "_meta"
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        f"image-cache-init: chown refused ({refused}, NFS root_squash?) -- the "
        "control plane (uid 65533) cannot create or record a volume until this "
        f"is done once: chown 65533:65533 {refused}",
        f"image-cache-init: FATAL: {refused} is owned by uid 0, not the "
        "control-plane uid 65533: the control plane creates every volume as "
        "<store>/<volume_id> and writes <store>/_meta/<volume_id>.json, so the "
        "first volume create would fail with EACCES",
        "image-cache-init: fix it once (as root on the node, or on the NFS "
        f"server): chown 65533:65533 {refused}  (non-recursive: the volume data "
        "directories below it belong to pooled sandbox uids)",
    ]
    for line in result.stdout.splitlines():
        assert not line.startswith(f"image-cache-init: {refused} -> uid"), line


def _shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run_multinode_init(
    tmp_path: Path, *, refuse: str | None = None
) -> subprocess.CompletedProcess:
    """Run the multinode init body under `sh` against a two-volume probe.

    The two trees stand in for `control-data` and `worker-data`; the init's own
    environment lists are redirected into them, and the volume roots/uids are
    the manifest's real values (65533 / 65534), so the arm fails if the script
    ever derives a target's uid from anything but its volume. `refuse` is a
    path suffix whose `chown` fails *and* whose `stat` keeps answering uid 0 --
    the shape an NFS `root_squash` refusal has.
    """
    control = tmp_path / "control"
    worker = tmp_path / "worker"
    for root, names in (
        (control, ("_images", "_images/_oci", "_secrets", "_volumes", "_volumes/_meta")),
        (worker, ("_images", "_images/_oci", "_snapshots")),
    ):
        for name in names:
            (root / name).mkdir(parents=True)
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(
        shims,
        "stat",
        "exec python3 - \"$2\" \"$3\" <<'PY'\n"
        "import os, sys\n"
        "fmt, path = sys.argv[1], sys.argv[2]\n"
        "refused = os.environ.get('PROBE_REFUSE', '')\n"
        "if refused and path.endswith(refused):\n"
        "    uid = 0\n"
        "elif path.startswith(os.environ['PROBE_CONTROL']):\n"
        "    uid = 65533\n"
        "else:\n"
        "    uid = 65534\n"
        "print(uid if '%u' in fmt else '755')\n"
        "PY\n",
    )
    _shim(
        shims,
        "chown",
        "exec python3 - \"$@\" <<'PY'\n"
        "import os, sys\n"
        "refused = os.environ.get('PROBE_REFUSE', '')\n"
        "if refused and sys.argv[-1].endswith(refused):\n"
        "    sys.stderr.write('chown: Operation not permitted\\n')\n"
        "    sys.exit(1)\n"
        "PY\n",
    )
    script = tmp_path / "image-cache-init.sh"
    script.write_text(
        _multinode_init()["command"][0].replace("$$", "$"), encoding="utf-8"
    )
    environment = dict(os.environ)
    environment["PATH"] = f"{shims}{os.pathsep}{environment['PATH']}"
    environment["CACHE_DIRS"] = f"{control}/_images {worker}/_images"
    environment["OWNED_DIRS"] = f"{control}/_secrets {worker}/_snapshots"
    environment["WRITABLE_ROOTS"] = f"{control} {worker}"
    environment["VOLUME_STORES"] = f"{control}/_volumes"
    environment["CONTROL_ROOT"] = str(control)
    environment["CONTROL_UID"] = "65533"
    environment["WORKER_ROOT"] = str(worker)
    environment["WORKER_UID"] = "65534"
    environment["PROBE_CONTROL"] = str(control)
    environment["PROBE_REFUSE"] = refuse or ""
    return subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
