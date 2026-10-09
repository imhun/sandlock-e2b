"""C3 Task 5 (ruling **D24**): the control plane converges to no root.

Task 5's deliverable was "CP 主容器 65534、CP pod 无 root 容器、A 类动作在 CP 侧清零"
(`plan §3`) — the uid in that sentence is **65533** since Task 3, and `CP_UID`
below is the number the assertions read. The plan's §3.2 letter says the CP's
remaining A-class action — the `_volumes` volume-root `mkdir` + `chmod 1777` — is
*handed to the agent*. It is not, and this file pins the decision that replaced it.

**Why the letter changed (D24, 2026-09-29).** Recon for this task established
that `_volumes` is not the CP's only write there: `VolumeRegistry` also writes
`<_volumes>/_meta/<volume_id>.json`, and `_meta` sits inside the same
`0:0 755` root. So **both** routes need the ownership change — the plan's
"不需要磁盘迁移" is incomplete regardless of which route is taken. Once the
hand-over is unavoidable, the agent route's remaining delta is a new `mkdir`
verb plus a new node-addressing rule for a *shared-storage* op, and the new verb
would live in the **root** `e2b-maint`: it widens exactly the surface C3 exists
to shrink. Option (B) needs neither.

So the CP keeps its own `mkdir`/`chmod`, and they are ordinary **owner**
operations rather than privileged ones, because the volume store belongs to the
control plane's uid. §13.6①'s first option ("把 `_volumes` 根迁到 CP 的 uid 后由
CP 自己做") and §13.2 ("B 类只需要一个稳定的非 root uid") are the plan's own
alternatives; D24 picks them.

What that turns the brief's third criterion into: "`_volumes` 的 `mkdir` 不在 CP
代码路径里" becomes "**the CP owns `_volumes`, so its `mkdir`/`chmod` need no
privilege**". Pinned here, so the property cannot be dropped quietly:

* the manifest half — the CP pod's main container is uid 65533 (the workers' 65534
  until Task 3), nothing in that pod runs as uid 0, and the once-root
  `image-cache-init` has moved to the agent's own pod (§3.2 step 3);
* the hand-over half — the agent runs it once per node, **non-recursively** on
  the store (the per-sandbox volume data directories below it belong to pooled
  sandbox uids and must survive a platform rollout), idempotently, and it *names*
  what it did ("already belongs" / "handed over") instead of succeeding mutely;
* the code half — no privileged verb was added for it (`maint.c`'s vocabulary is
  unchanged), the volume registry names no agent/file-op, and a store it cannot
  write is a **named** refusal that carries the exact hand-over command rather
  than a bare `EACCES` at the first volume create.

Text and manifest assertions rather than behaviour, for the manifest half: the
properties are deployment facts, and a comment cannot smuggle a `chown -R` in
either (the recursive form is excluded *by name* below).
"""

from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
K8S = REPO / "deploy" / "k8s"
CONTROL_PLANE_MANIFEST = K8S / "control-plane.yaml"
AGENT_MANIFEST = K8S / "c3-agent.yaml"
VOLUMES_SOURCE = REPO / "control_plane" / "registry" / "volumes.py"
MAINT_C = REPO / "c3_agent" / "priv" / "maint.c"

#: The control plane's uid. C3 Task 5 picked 65534 (the workers' uid) because
#: §13.6's三条 findings collapsed to it: the platform's own directories were
#: already 65534, `.uid_pool.lock` is `65534:65534 0600`, and the image cache
#: owner was 65534. **Task 3 of the uid-relocation plan moved it to 65533** --
#: sharing the workers' uid meant a compromised worker or agent face A (also
#: 65534) could ptrace the control plane
#: (`docs/security-audit/c3-agent-hostpid-2026-10-09.md` §5). Task 2's move of
#: the `_oci` reclaim to the control plane is what made the split affordable:
#: `_images` is read-only for the worker, so no shared group is needed. The
#: migration that makes the platform directories belong to 65533 is Task 4
#: (`docs/security-audit/control-plane-uid-2026-10-09.md`).
CP_UID = 65533
#: The workers' uid, which the control plane used to share. Pinned separately
#: here because the whole point of Task 3 is that the two are *different*.
WORKER_UID = 65534
#: The volume store, as every manifest in this repo spells it.
SHARED_ROOT = "/var/lib/e2b-sandboxes"
VOLUME_STORE = f"{SHARED_ROOT}/_volumes"


def _load_all(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _only(docs: list[dict], kind: str, name: str) -> dict:
    matches = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name, [d["metadata"]["name"] for d in matches])
    return matches[0]


def _pod_spec(workload: dict) -> dict:
    return workload["spec"]["template"]["spec"]


def _containers(workload: dict) -> dict[str, dict]:
    return {c["name"]: c for c in _pod_spec(workload)["containers"]}


def _init_containers(workload: dict) -> dict[str, dict]:
    return {c["name"]: c for c in (_pod_spec(workload).get("initContainers") or [])}


def _script_lines(container: dict) -> list[str]:
    """The shell body of an init container, one stripped non-blank line each."""
    body = container["command"][2]
    return [line.strip() for line in body.splitlines() if line.strip()]


def _commands(container: dict) -> list[str]:
    """...and of those, the lines that actually run (comments removed)."""
    return [line for line in _script_lines(container) if not line.startswith("#")]


def _shim(directory: Path, name: str, body: str) -> None:
    """A one-file executable on ``PATH`` for the behavioural pins below."""
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run_storage_init(
    tmp_path: Path,
    *,
    refuse_uid: str | None,
    refuse_mode: str = "",
    refuse: str = "/_meta",
) -> subprocess.CompletedProcess:
    """Run the manifest's own `storage-init` script under `sh`, for real.

    Verifying the script by reading it is not enough for the two properties
    this pins (a gate exists *and* a refusal cannot print the success line), so
    the body is executed against a probe tree. It is made deterministic on any
    host with two shims, because the script's own tools are Linux-shaped:

    * ``stat -c %u|%g|%a <path>`` (macOS ``stat`` has no ``-c``) answers from a
      static model of the two identities Task 3 introduced: the node-local cache
      is ``65534:65534 0755``, everything on the shared export root is
      ``65533:65533 0755``, and ``_snapshots`` is ``65534:65533 2775``;
    * ``chown``/``chmod`` succeed (the model already answers the post-hand-over
      values), except in the refusal arms below.

    ``refuse`` is the path suffix that ``refuse_uid`` applies to: a uid there
    means "the chown is refused and the path keeps that uid" (the NFS
    ``root_squash`` shape). ``refuse_uid=None`` is the control arm.
    ``refuse_mode`` is a path suffix whose ``chmod`` is refused instead, so its
    mode stays ``0755``.

    The snapshot store's direct children are modelled *statefully*, because
    fix round 3 sweeps them: a child answers the pre-Task-3 shape
    (``65534:65534 0755``) until both a ``chown`` and a ``chmod`` have run
    against it, and each of those writes a marker file, so the test can read the
    sweep's exact scope off the filesystem (`PROBE_MARKERS`).
    """
    store = tmp_path / "probe" / "shared"
    node_cache = tmp_path / "probe" / "node-local" / "e2b-images"
    for name in ("_images/_oci", "_builds", "_secrets", "_templates", "_snapshots"):
        (store / name).mkdir(parents=True)
    # Two snapshot ids that predate the change: one empty, one with a payload
    # file inside it (the file must never be touched by the sweep).
    (store / "_snapshots" / "snap_old").mkdir()
    (store / "_snapshots" / "snap_new").mkdir()
    (store / "_snapshots" / "snap_new" / "fs.tar").write_bytes(b"tar")
    (store / "_volumes" / "_meta").mkdir(parents=True)
    (store / "_volumes" / "vol_abc").mkdir()
    node_cache.mkdir(parents=True)
    markers = tmp_path / "markers"
    markers.mkdir()
    shims = tmp_path / "shims"
    shims.mkdir()
    refused_uid = "-1" if refuse_uid is None else refuse_uid
    _shim(
        shims,
        "stat",
        'exec python3 - "$2" "$3" <<\'PY\'\n'
        "import os, sys\n"
        "fmt, path = sys.argv[1], sys.argv[2]\n"
        "refused = os.environ.get('PROBE_REFUSED_UID', '-1')\n"
        "suffix = os.environ.get('PROBE_REFUSED', '')\n"
        "shared = os.environ['PROBE_SHARED']\n"
        "snap = os.environ['PROBE_SNAPSHOT']\n"
        "migrate = os.environ['PROBE_MIGRATE']\n"
        "refuse_mode = os.environ.get('PROBE_REFUSED_MODE', '')\n"
        "markers = os.environ['PROBE_MARKERS']\n"
        "def swept(kind):\n"
        "    return os.path.exists(markers + '/' + kind + '-' + path.replace('/', '_'))\n"
        "if refused != '-1' and suffix and path.endswith(suffix):\n"
        "    uid, gid, mode = int(refused), int(refused), '755'\n"
        "elif path == snap:\n"
        "    uid, gid = 65534, 65533\n"
        "    mode = '755' if refuse_mode and path.endswith(refuse_mode) else '2775'\n"
        "elif path == migrate:\n"
        "    uid, gid = 65533, 65533\n"
        "    mode = '755' if refuse_mode and path.endswith(refuse_mode) else '2775'\n"
        "elif path.startswith(snap + '/'):\n"
        "    uid = 65534\n"
        "    gid = 65533 if swept('chown') else 65534\n"
        "    mode = '2775' if swept('chmod') else '755'\n"
        "elif path.startswith(shared):\n"
        "    uid, gid, mode = 65533, 65533, '755'\n"
        "else:\n"
        "    uid, gid, mode = 65534, 65534, '755'\n"
        "print(uid if '%u' in fmt else gid if '%g' in fmt else mode)\n"
        "PY\n",
    )
    _shim(
        shims,
        "chown",
        'if [ "$PROBE_REFUSED_UID" != "-1" ] && [ -n "$PROBE_REFUSED" ]; then\n'
        '  case "${2:-}" in *"$PROBE_REFUSED") exit 1;; esac\n'
        "fi\n"
        "shift\n"
        'for target in "$@"; do\n'
        '  case "$target" in\n'
        '    "$PROBE_SNAPSHOT"/*) : > "$PROBE_MARKERS/chown-$(printf %s "$target" | tr / _)" ;;\n'
        "  esac\n"
        "done\n"
        "exit 0\n",
    )
    _shim(
        shims,
        "chmod",
        'if [ -n "$PROBE_REFUSED_MODE" ]; then\n'
        '  case "${2:-}" in *"$PROBE_REFUSED_MODE") exit 1;; esac\n'
        "fi\n"
        "shift\n"
        'for target in "$@"; do\n'
        '  case "$target" in\n'
        '    "$PROBE_SNAPSHOT"/*) : > "$PROBE_MARKERS/chmod-$(printf %s "$target" | tr / _)" ;;\n'
        "  esac\n"
        "done\n"
        "exit 0\n",
    )
    script = tmp_path / "storage-init.sh"
    script.write_text(
        _init_containers(
            _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
        )["storage-init"]["command"][2],
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PATH"] = f"{shims}{os.pathsep}{environment['PATH']}"
    # Task 3: the script is handed one list per identity, and the probe answers
    # each target's uid/gid/mode accordingly.
    environment["WORKER_UID"] = "65534"
    environment["CP_UID"] = "65533"
    environment["WORKER_CACHE_DIRS"] = str(node_cache)
    environment["CP_CACHE_DIRS"] = str(store / "_images")
    environment["CP_DIRS"] = " ".join(
        str(store / name) for name in ("_builds", "_secrets", "_templates")
    )
    environment["SNAPSHOT_DIR"] = str(store / "_snapshots")
    environment["SNAPSHOT_OWNER"] = "65534:65533"
    environment["SNAPSHOT_MODE"] = "2775"
    environment["MIGRATE_DIR"] = str(store / "_migrate")
    environment["MIGRATE_OWNER"] = "65533:65533"
    environment["MIGRATE_MODE"] = "2775"
    environment["SHARED_ROOT"] = str(store)
    environment["PROBE_REFUSED_UID"] = refused_uid
    environment["PROBE_REFUSED"] = refuse
    environment["PROBE_REFUSED_MODE"] = refuse_mode
    environment["PROBE_SHARED"] = str(store)
    environment["PROBE_SNAPSHOT"] = str(store / "_snapshots")
    environment["PROBE_MIGRATE"] = str(store / "_migrate")
    environment["PROBE_MARKERS"] = str(markers)
    return subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


# ------------------------------------------------ the control-plane pod itself


def test_the_control_plane_container_runs_as_its_own_uid() -> None:
    """判据 8, first half: main container `runAsUser == CP_UID`.

    `runAsGroup` is pinned with it: the platform's own state was `65534:65534`
    and `.uid_pool.lock` is `0600`, so the uid is the load-bearing half — but a
    container that ran `CP_UID:0` would create every record with the root
    group, and the two are one decision. (Task 3 moved CP_UID off the workers'
    65534 onto its own 65533; see that constant.)
    """
    deployment = _only(_load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane")
    cp = _containers(deployment)["control-plane"]
    security = cp["securityContext"]
    assert security["runAsUser"] == CP_UID
    assert security["runAsGroup"] == CP_UID
    assert security["runAsUser"] != WORKER_UID


def test_no_container_in_the_control_plane_pod_is_root() -> None:
    """判据 8, second half: the pod has no root container, init or otherwise.

    This is what §3.2 step 3 buys: the once-root `image-cache-init` is gone
    from this pod (it lives in `deploy/k8s/c3-agent.yaml` now), so the CP pod
    is two unprivileged-by-default containers.
    """
    deployment = _only(_load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane")
    pod = _pod_spec(deployment)
    assert pod.get("initContainers") in (None, [])
    for container in pod["containers"]:
        security = container.get("securityContext") or {}
        run_as = security.get("runAsUser")
        assert run_as != 0, container["name"]
        assert not security.get("privileged"), container["name"]
        # The plan's禁项 (§2.3) are worker/face-A rules; the CP pod is held to
        # the "no root container" half of the same judgement.
        for forbidden in ("SYS_ADMIN", "SYS_PTRACE", "NET_RAW"):
            assert forbidden not in ((security.get("capabilities") or {}).get("add") or [])


def test_the_image_cache_init_moved_to_the_agent_pod() -> None:
    """§3.2 step 3: the root init that chowned `_images` is the agent's now.

    The old home verified the ownership *before the control plane started*; the
    new one verifies it before the **agent** starts. Both are root, both are on
    the same shared PVC, and the agent is the component that already carries the
    root file face (face B) — so this is the move the plan asked for and not a
    new privileged surface.
    """
    deployment = _only(_load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane")
    cp_pod = _pod_spec(deployment)
    assert "image-cache-init" not in _init_containers(deployment)
    assert "image-cache-init" not in {c["name"] for c in cp_pod["containers"]}

    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    storage_init = _init_containers(agent)["storage-init"]
    assert storage_init["securityContext"]["runAsUser"] == 0
    mounts = {m["name"]: m for m in storage_init["volumeMounts"]}
    assert mounts["shared"]["mountPath"] == SHARED_ROOT


def test_the_buildkit_sidecar_keeps_its_wide_seccomp_profile() -> None:
    """The named保留项 (§3.2): buildkit stays uid 1000 + `Unconfined`.

    Not root, so it does not break the口径 — but it is the one container left in
    this pod with a wide profile, and the plan requires it to be *named* rather
    than left implicit. The escalation-block half is pinned with it: setting
    `allowPrivilegeEscalation: false` makes the kubelet add `no_new_privs`, and
    rootlesskit then dies in `newuidmap`.
    """
    deployment = _only(_load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane")
    buildkit = _containers(deployment)["buildkit"]
    security = buildkit["securityContext"]
    assert security["seccompProfile"] == {"type": "Unconfined"}
    assert security.get("runAsUser") is None
    assert "allowPrivilegeEscalation" not in security


# ---------------------------------------------------------- the volume hand-over


def test_the_agent_hands_the_volume_store_over_non_recursively_and_says_so() -> None:
    """D24's manifest half, with the recursive form excluded by name.

    The store's *own* ownership moves; the directories below it are the
    sandbox volume data (`<store>/<id>/` plus the per-sandbox quota slices)
    and belong to pooled sandbox uids. A `chown -R` here would take them back
    on **every agent rollout** — and the documented upgrade step rolls the
    agent — so the recursive form is the regression this pin exists for.

    Idempotence and visibility are pinned too, because the hand-over runs on
    every node of a shared mount: the second node must find "already belongs"
    and say so, rather than re-chowning silently.
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    init = _init_containers(agent)["storage-init"]
    lines = _commands(init)
    script = "\n".join(_script_lines(init))

    # The store and its record directory, each handed over by the same helper.
    for expected in (
        # ...one directory at a time, never `-R` (the recursive form would take
        # the sandbox volume data directories back on every agent rollout).
        # Task 3: the uid is the caller's now -- the store is the control
        # plane's, the node-local cache list is the worker's.
        'target="$1"',
        'uid="$2"',
        'chown $uid:$uid "$target" 2>/dev/null ||',
        # The store is *created* here when it is missing: the control plane pod
        # mounts it as a subPath (a missing source keeps that pod in
        # ContainerCreating) and the creator used to be the broker's own init,
        # which Task 7 retires. Fresh-install order (agent before control plane)
        # therefore has to converge here.
        'volume_store="$SHARED_ROOT/_volumes"',
        'if [ ! -e "$volume_store" ]; then',
        'mkdir -p "$volume_store"',
        # The gate: the store's own ownership is *verified*, and a
        # root_squashed NFS stops the pod here instead of failing the first
        # volume create later, inside the control plane.
        'if [ "$owner" != "$uid" ]; then',
        'return 1',
        'hand_over "$volume_store" "$CP_UID" || exit 1',
        'hand_over "$volume_store/_meta" "$CP_UID" || exit 1',
    ):
        assert expected in lines, expected
    for uid in ("$CP_UID", "$WORKER_UID"):
        assert f'chown -R {uid}:{uid} "$SHARED_ROOT/_volumes' not in script
        assert f'chown -R {uid}:{uid} "$target' not in script
    # Visible, in both directions: the already-owned branch and the refusal.
    assert "already belongs to uid $uid" in script
    assert "chown refused" in script


def test_the_volume_store_hand_over_targets_the_path_the_control_plane_writes() -> None:
    """The two sides must name one directory (the Cp names it too).

    A hand-over of a path the control plane does not write is a no-op that
    looks like one; this pins the spelling, the way `test_c3_agent_manifest.py`
    pins the four whitelist roots across the deployment shapes.
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    init = _init_containers(agent)["storage-init"]
    env = {entry["name"]: entry["value"] for entry in init["env"]}
    assert env["SHARED_ROOT"] == SHARED_ROOT
    # The shared cache is one of the two caches this init prepares -- its own
    # list since Task 3, because the two now belong to two identities. (C3 Task 7
    # moved C1's broker `image-cache-init` in here too, which is why the other
    # list carries the node-local `/var/lib/e2b-images`; the volume-store
    # hand-over this test is about is unaffected.)
    assert env["CP_CACHE_DIRS"].split() == [f"{SHARED_ROOT}/_images"]
    assert env["WORKER_CACHE_DIRS"].split() == ["/var/lib/e2b-images"]
    assert env["CP_UID"] == "65533"
    assert env["WORKER_UID"] == "65534"
    # ...and the control plane's own store, from the app's wiring: the registry
    # is built on `<shared root>/_volumes` when no explicit root is configured
    # (`control_plane/app.py`). Pinned as the same literal.
    app_source = (REPO / "control_plane" / "app.py").read_text(encoding="utf-8")
    assert '"_volumes"' in app_source


def test_a_record_directory_that_hands_over_reports_success(tmp_path: Path) -> None:
    """The control arm for the two refusal pins below.

    With everything handing over, the script exits 0 and prints a success line
    for **both** halves -- the store root and the record directory D24's recon
    added. Without this arm the refusal assertions could pass vacuously (a
    script that prints no success line ever would satisfy them).
    """
    result = _run_storage_init(tmp_path, refuse_uid=None)

    store = tmp_path / "probe" / "shared"
    node_cache = tmp_path / "probe" / "node-local" / "e2b-images"
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        # 1a: the node-local cache, the worker's.
        f"storage-init: {node_cache} is owned by uid 65534",
        # 1b/1c: the shared export root, the control plane's.
        f"storage-init: {store}/_images is owned by uid 65533",
        f"storage-init: {store}/_builds is owned by uid 65533",
        f"storage-init: {store}/_secrets is owned by uid 65533",
        f"storage-init: {store}/_templates is owned by uid 65533",
        # 1d: the one cross-uid directory, group + mode included.
        f"storage-init: {store}/_snapshots is 65534:65533 mode 2775 (two writers: "
        "the worker writes <id>/fs.tar and the control plane writes "
        "<id>/snapshot.json inside the same directory)",
        # ...and the second one (R15): the migration landing zone.
        f"storage-init: {store}/_migrate is 65533:65533 mode 2775 (two writers: "
        "the control plane writes control-plane/<id>.tar.gz and the worker writes "
        "<id>.tar.gz)",
        # ...then the pre-existing snapshot ids are swept, one level only.
        f"storage-init: {store}/_snapshots/snap_new is 65534:65533 mode 2775 "
        "(a pre-existing snapshot directory, swept so the control plane can "
        "delete it)",
        f"storage-init: {store}/_snapshots/snap_old is 65534:65533 mode 2775 "
        "(a pre-existing snapshot directory, swept so the control plane can "
        "delete it)",
        # 2: the volume store, gated one target at a time.
        f"storage-init: {store}/_volumes already belongs to uid "
        "65533 (mode 755) -- nothing to do",
        f"storage-init: {store}/_volumes/_meta already belongs to "
        "uid 65533 (mode 755) -- nothing to do",
        f"storage-init: {store}/_volumes is owned by uid 65533 "
        "(the volume data directories below it are left alone)",
    ]
    assert result.stderr == ""
    # The sweep's exact scope, read off the markers its own `chown`/`chmod` wrote:
    # both direct children, owner *and* mode, and nothing below them (the payload
    # file inside `snap_new` is not touched).
    assert sorted(path.name for path in (tmp_path / "markers").iterdir()) == [
        "chmod-" + str(store / "_snapshots" / "snap_new").replace("/", "_"),
        "chmod-" + str(store / "_snapshots" / "snap_old").replace("/", "_"),
        "chown-" + str(store / "_snapshots" / "snap_new").replace("/", "_"),
        "chown-" + str(store / "_snapshots" / "snap_old").replace("/", "_"),
    ]


def test_a_refused_record_directory_cannot_read_as_a_hand_over(tmp_path: Path) -> None:
    """Review round 1 (Important): ``_meta`` is gated like the store root.

    ``hand_over "<store>/_meta"`` used to run ungated and the success echo
    printed unconditionally, so an NFS refusal on the record directory read as
    success *here* and then surfaced much later, un-named, on the second write
    D24's recon found -- ``_write_record`` → ``write_text_atomically``'s lazy
    ``mkdir``/``os.open``, which sits **outside** the control plane's own
    ``VolumeRootNotOwnedError`` wrapper. The operator is entitled to a FATAL at
    init instead, naming the path and the one-time command.

    The store root still hands over in this arm: the defect is specifically
    that the *second* target was unchecked, and a pin that refused both would
    not show it.
    """
    result = _run_storage_init(tmp_path, refuse_uid="0")

    store = tmp_path / "probe" / "shared"
    assert result.returncode == 1
    # ...the store root's own state is read and reported (here it already is the
    # control plane's uid, which is the shape a re-run on a healthy node has)...
    assert (
        f"storage-init: {store}/_volumes already belongs to uid "
        "65533 (mode 755) -- nothing to do" in result.stdout.splitlines()
    )
    # ...and the refused record directory printed **no** success line.
    for line in result.stdout.splitlines():
        assert not line.startswith(
            f"storage-init: {store}/_volumes/_meta -> uid"
        ), line
    # ...it is a FATAL on stderr that names the path and the exact one-time fix.
    assert result.stderr.splitlines() == [
        f"storage-init: chown refused ({store}/_volumes/_meta, "
        "NFS root_squash?) -- the control plane (uid 65533) cannot create or "
        "record a volume until this is done once: chown 65533:65533 "
        f"{store}/_volumes/_meta",
        f"storage-init: FATAL: {store}/_volumes/_meta is owned by "
        "uid 0, not the control-plane uid 65533: the control plane creates every "
        "volume as <store>/<volume_id> and writes <store>/_meta/<volume_id>.json, "
        "so the first volume create would fail with EACCES",
        "storage-init: fix it once (as root on the node, or on the NFS server): "
        f"chown 65533:65533 {store}/_volumes/_meta  "
        "(non-recursive: the volume data directories below it belong to pooled "
        "sandbox uids)",
    ]


def test_a_snapshot_store_at_the_wrong_mode_is_a_fatal(tmp_path: Path) -> None:
    """The one *mode* this init owns is verified too (Task 3 / R13).

    `_snapshots` is the single cross-uid directory: the worker writes the payload
    and the control plane writes the record inside the same `<id>/`, so a hand-over
    that chowns the pair but leaves the mode at 0755 (an NFS `root_squash` that
    eats the `chmod`, or an edit that drops it) is exactly the shape that fails
    later, un-named, at the first capture. It stops here instead.
    """
    result = _run_storage_init(tmp_path, refuse_uid=None, refuse_mode="_snapshots")

    store = tmp_path / "probe" / "shared"
    node_cache = tmp_path / "probe" / "node-local" / "e2b-images"
    assert result.returncode == 1
    # Everything before the snapshot block still reports, and the snapshot store
    # prints **no** success line.
    assert result.stdout.splitlines() == [
        f"storage-init: {node_cache} is owned by uid 65534",
        f"storage-init: {store}/_images is owned by uid 65533",
        f"storage-init: {store}/_builds is owned by uid 65533",
        f"storage-init: {store}/_secrets is owned by uid 65533",
        f"storage-init: {store}/_templates is owned by uid 65533",
        # The refused `chmod` says so where the script's own echo goes (stdout,
        # like the compose lane's twin), and the store prints no success line.
        f"storage-init: chmod refused ({store}/_snapshots, NFS root_squash?)",
    ]
    assert result.stderr.splitlines() == [
        f"storage-init: FATAL: {store}/_snapshots is 65534:65533 mode 755, not "
        "65534:65533 mode 2775: the worker writes <id>/fs.tar and the control "
        "plane writes <id>/snapshot.json inside the same directory, so the "
        "hand-over cannot be skipped",
        "storage-init: fix it once (as root on the node, or on the NFS server): "
        f"chown 65534:65533 {store}/_snapshots && chmod 2775 {store}/_snapshots",
    ]


def test_a_migrate_staging_at_the_wrong_mode_is_a_fatal(tmp_path: Path) -> None:
    """R15's gate: `_migrate`'s mode is verified, and it is set unconditionally.

    The worker writes `_migrate/<id>.tar.gz` in-process, so a hand-over that
    leaves the directory at 0755 (a fresh volume's `mkdir` mode, or an NFS that
    eats the `chmod`) makes the first cross-node export answer 500. The old shape
    only chmodded `1777` when the chown had been *refused*, which is exactly the
    fresh-volume case this arm pins shut.
    """
    result = _run_storage_init(tmp_path, refuse_uid=None, refuse_mode="_migrate")

    store = tmp_path / "probe" / "shared"
    node_cache = tmp_path / "probe" / "node-local" / "e2b-images"
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"storage-init: {node_cache} is owned by uid 65534",
        f"storage-init: {store}/_images is owned by uid 65533",
        f"storage-init: {store}/_builds is owned by uid 65533",
        f"storage-init: {store}/_secrets is owned by uid 65533",
        f"storage-init: {store}/_templates is owned by uid 65533",
        # The snapshot store hands over *before* the migration staging, and its
        # line is the only success line before the refusal.
        f"storage-init: {store}/_snapshots is 65534:65533 mode 2775 (two writers: "
        "the worker writes <id>/fs.tar and the control plane writes "
        "<id>/snapshot.json inside the same directory)",
        f"storage-init: chmod refused ({store}/_migrate, NFS root_squash?)",
    ]
    assert result.stderr.splitlines() == [
        f"storage-init: FATAL: {store}/_migrate is 65533:65533 mode 755, not "
        "65533:65533 mode 2775: the control plane writes "
        "control-plane/<id>.tar.gz and the worker writes <id>.tar.gz, so the "
        "hand-over cannot be skipped",
        "storage-init: fix it once (as root on the node, or on the NFS server): "
        f"chown 65533:65533 {store}/_migrate && chmod 2775 {store}/_migrate",
    ]


def test_a_snapshot_id_the_sweep_cannot_fix_is_a_warning_not_an_outage(
    tmp_path: Path,
) -> None:
    """A refused sweep names the id and lets the pod start (fix round 3).

    The sweep exists because the control plane's `SnapshotRegistry.delete()` is
    `rmtree(..., ignore_errors=True)`: on a 0755 `<id>/` it cannot unlink the
    `snapshot.json` inside, so `DELETE` answers 204 and removes nothing. A
    storage that refuses the `chmod` still has to be able to *start* -- stopping
    the agent pod would trade one old snapshot's deletion for a start-up outage
    -- so the failure is a WARN that names the id and the one-time fix.
    """
    result = _run_storage_init(
        tmp_path, refuse_uid=None, refuse_mode="snap_new"
    )

    store = tmp_path / "probe" / "shared"
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    # The other id is swept and says so; the refused one says which it is.
    assert (
        f"storage-init: {store}/_snapshots/snap_old is 65534:65533 mode 2775 "
        "(a pre-existing snapshot directory, swept so the control plane can "
        "delete it)" in lines
    )
    assert not [
        line for line in lines if line.startswith(f"storage-init: {store}/_snapshots/snap_new is")
    ]
    # The refused `chmod` says so on stdout (the script's own echo shape), and
    # the WARN is the part an operator has to act on.
    assert (
        f"storage-init: chmod refused ({store}/_snapshots/snap_new, NFS "
        "root_squash?) -- this old snapshot cannot be deleted until it is done "
        f"once: chmod 2775 {store}/_snapshots/snap_new" in lines
    )
    assert result.stderr.splitlines() == [
        f"storage-init: WARN: {store}/_snapshots/snap_new is 65534:65533 mode 755, "
        "not 65534:65533 mode 2775 -- deleting that snapshot would answer 204 "
        "and remove nothing; fix it once: chown 65534:65533 "
        f"{store}/_snapshots/snap_new && chmod 2775 {store}/_snapshots/snap_new",
    ]


def test_both_hand_overs_are_gated_in_the_script_text() -> None:
    """...and the gate cannot be dropped by an edit that still passes above.

    A text pin on the same property, so a rewrite that keeps the shape but
    loses the *gate* (e.g. ``hand_over "$volume_store/_meta"`` without its exit,
    or a helper that echoes before verifying) fails here as well as in the
    behavioural pins.
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    lines = _commands(_init_containers(agent)["storage-init"])
    for expected in (
        'hand_over "$volume_store" "$CP_UID" || exit 1',
        'hand_over "$volume_store/_meta" "$CP_UID" || exit 1',
    ):
        assert expected in lines, expected
    # Inside the helper: verify, then either FATAL+return 1 or the success echo.
    start = lines.index("hand_over() {")
    helper = lines[start : lines.index("}", start) + 1]
    verify = helper.index('if [ "$owner" != "$uid" ]; then')
    fatal = next(
        index for index, line in enumerate(helper) if line.startswith('echo "storage-init: FATAL:')
    )
    success = next(
        index for index, line in enumerate(helper) if "-> uid $owner mode $mode" in line
    )
    assert verify < fatal < success, helper


# ------------------------------------------------------- the CP code path (A3)


def test_the_privileged_verb_vocabulary_was_not_widened_for_a3() -> None:
    """D24's code half: no `mkdir`/`chmod` verb was added to `e2b-maint`.

    Adding one would have been the plan's letter (§3.2 step 2 hands A3 to the
    agent), and it is exactly what D24 rules out: a new verb in the **root**
    file face for an operation the CP can perform as the store's owner.
    """
    from c3_agent.fileops import FILE_OP_VERBS

    assert FILE_OP_VERBS == ("chown", "rm", "walk")
    maint = MAINT_C.read_text(encoding="utf-8")
    for verb in ("mkdir", "chmod"):
        assert f'strcmp(verb, "{verb}")' not in maint, verb


def test_the_volume_registry_names_no_privileged_op_and_no_second_whitelist() -> None:
    """The CP's volume path is an owner operation, not a delegated one.

    Deliberately a source pin: "no privileged op in this file" is a property of
    the module's imports, and a behavioural test would only prove the branch a
    mock happened to take.
    """
    source = VOLUMES_SOURCE.read_text(encoding="utf-8")
    for forbidden in (
        "agent_fileops",
        "c3_agent",
        "file_ops",
        "FileOpInstruction",
        "e2b-maint",
        "subprocess",
    ):
        assert forbidden not in source, forbidden
    # The named failure the pre-condition is expressed through (below) is the
    # only new thing this module learned.
    assert "VolumeRootNotOwnedError" in source


def test_a_volume_store_the_control_plane_does_not_own_is_a_named_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ownership pre-condition, made audible (D24).

    A store left root-owned (a fresh install, or a node whose agent init has not
    run yet) used to surface as a bare `PermissionError` from `Path.mkdir`
    inside the create handler. It is now a named refusal that carries the exact,
    non-recursive hand-over — so the operator is told what to run instead of
    reading an errno, and the pin fails if the pre-condition stops being named.
    """
    store = tmp_path / "_volumes"
    store.mkdir()
    from control_plane.registry import volumes as volumes_module
    from control_plane.registry.volumes import VolumeRootNotOwnedError

    # A deterministic id, so the refusal's text can be asserted exactly.
    monkeypatch.setattr(
        volumes_module, "sandbox_id", lambda: "sbx_" + "0" * 32
    )
    registry = volumes_module.VolumeRegistry(store)
    created = store / ("vol_" + "0" * 32)

    real_mkdir = Path.mkdir

    def refuse_only_the_volume_directory(self, *args, **kwargs):
        if self == created:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse_only_the_volume_directory)

    with pytest.raises(VolumeRootNotOwnedError) as caught:
        registry.create("named-refusal")

    assert str(caught.value) == (
        f"cannot create {created}: the volume store {store} is not writable by "
        f"this control plane (uid {os.geteuid()}). The store has to belong to "
        "that uid before a volume can be created in it or a record written "
        "beside it: hand it over once, non-recursively (never `-R`, since the "
        "directories below it are sandbox volume data owned by pooled sandbox "
        f"uids): chown {os.geteuid()}:{os.geteuid()} \"{store}\" "
        f'"{store}/_meta"'
    )


def test_a_store_the_control_plane_cannot_create_at_startup_is_named_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1 (minor 2): the same creation, one layer earlier.

    ``VolumeRegistry.__init__`` creates the store, and in the compose and
    ``local://`` lanes that is the *first* creation of it (the k8s pod mounts
    ``<store>`` as a subPath, so there it always exists before this runs).
    Unwrapped, that failure was a bare ``PermissionError`` out of the registry's
    constructor — i.e. at app startup, which is exactly the un-named shape the
    create path was fixed for.
    """
    store = tmp_path / "_volumes"
    from control_plane.registry import volumes as volumes_module
    from control_plane.registry.volumes import VolumeRootNotOwnedError

    real_mkdir = Path.mkdir

    def refuse_only_the_store(self, *args, **kwargs):
        if self == store:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse_only_the_store)

    with pytest.raises(VolumeRootNotOwnedError) as caught:
        volumes_module.VolumeRegistry(store)

    assert str(caught.value) == (
        f"cannot create {store}: the volume store {store} is not writable by "
        f"this control plane (uid {os.geteuid()}). The store has to belong to "
        "that uid before a volume can be created in it or a record written "
        "beside it: hand it over once, non-recursively (never `-R`, since the "
        "directories below it are sandbox volume data owned by pooled sandbox "
        f"uids): chown {os.geteuid()}:{os.geteuid()} \"{store}\" "
        f'"{store}/_meta"'
    )


def test_a_volume_store_the_control_plane_owns_is_created_without_the_agent(
    tmp_path: Path,
) -> None:
    """The positive half: with the store owned by this uid, A3 is local.

    No agent, no verb, no whitelist — the directory and the `1777` come from
    the CP's own `mkdir`/`chmod`, which is the whole point of the hand-over.
    """
    store = tmp_path / "_volumes"
    store.mkdir()
    from control_plane.registry.volumes import VolumeRegistry

    registry = VolumeRegistry(store)

    record = registry.create("local-owner")

    assert record.path.parent == store
    assert record.path.is_dir()
    assert (record.path.stat().st_mode & 0o7777) == 0o1777
    assert record.path.stat().st_uid == os.geteuid()
    # ...and the record's own directory, the second write D24's recon found.
    assert (store / "_meta" / f"{record.volume_id}.json").is_file()


# ------------------------------------------------- the compose lane (gap 1)

#: The three stacks whose control plane runs as a non-root uid -- 65533 in the
#: multinode lane, still the workers' 65534 in the two single-volume prod shapes
#: (`docs/open-issues.md` N94) -- and whose `image-cache-init` is the root
#: one-shot that prepares what it must own.
COMPOSE_STACKS = (
    REPO / "deploy" / "compose" / "docker-compose.prod.yml",
    REPO / "deploy" / "compose" / "docker-compose.multinode.yml",
    REPO / "deploy" / "stack" / "docker-compose.prod.yml",
)

#: The volume store, as every compose stack spells it inside its init container.
#: Keyed by the path inside the repo: `deploy/compose` and `deploy/stack` both
#: ship a `docker-compose.prod.yml`.
VOLUME_STORE_ENV = {
    "deploy/compose/docker-compose.prod.yml": "/var/lib/e2b-sandboxes/_volumes",
    "deploy/compose/docker-compose.multinode.yml": "/cache/control/_volumes",
    "deploy/stack/docker-compose.prod.yml": "/var/lib/e2b-sandboxes/_volumes",
}

#: The cache-hand-over half of the init, **exactly**: the careful form the k8s
#: `storage-init` uses, adopted so that a `chown -R "$dir"` -- which takes live
#: sandbox `secrets/<sandbox_id>/<name>.secret` files (0600, owned by that
#: sandbox's pooled uid) back on every `up -d` -- cannot come back. Reverting it
#: to the recursive form is what the review found unpinned; the arms of
#: `_run_compose_init` cannot see it (they exercise the store), so this is the
#: pin for it, and it is an exact list rather than a containment check.
CACHE_HANDOVER_LINES = [
    'for dir in $CACHE_DIRS; do',
    # Task 3: the uid is read off the volume the target lives on (`uid_of`),
    # never a literal at the call site, and a target under neither declared
    # root is refused by name.
    'uid_of "$dir"',
    '[ -n "$uid" ] || refuse_foreign "$dir"',
    'mkdir -p "$dir/_oci"',
    'chmod 0755 "$dir" "$dir/_oci" 2>/dev/null || true',
    # Non-recursive on the cache's own directory...
    'chown "$uid:$uid" "$dir" 2>/dev/null ||',
    'echo "image-cache-init: chown refused ($dir, NFS root_squash?) -- verifying the owner instead"',
    # ...recursive only inside the control plane's `_oci/` sidecar...
    'chown -R "$uid:$uid" "$dir/_oci" 2>/dev/null ||',
    'echo "image-cache-init: chown refused ($dir/_oci, NFS root_squash?) -- verifying the owner instead"',
    # ...and `secrets/` **directories only**: the `*.secret` files stay with the
    # sandbox uid that wrote them.
    'if [ -d "$dir/secrets" ]; then',
    'chown "$uid:$uid" "$dir/secrets" 2>/dev/null ||',
    'echo "image-cache-init: chown refused ($dir/secrets, NFS root_squash?) -- the per-sandbox secret dirs must belong to uid $uid"',
    'find "$dir/secrets" -mindepth 1 -maxdepth 2 -type d -exec chown "$uid:$uid" {} + 2>/dev/null ||',
    'echo "image-cache-init: chown refused ($dir/secrets -mindepth 1 -maxdepth 2, NFS root_squash?) -- the per-sandbox secret dirs must belong to uid $uid"',
    'echo "image-cache-init: handed $dir/secrets (directories only; *.secret files stay with their sandbox uid) to uid $uid"',
    'fi',
    'owner="$(stat -c %u "$dir")"',
    'if [ "$owner" != "$uid" ]; then',
    'echo "image-cache-init: FATAL: $dir is owned by uid $owner, not uid $uid (the uid of the volume it lives on): a $uid writer cannot create its lock files or staging trees there, so every image resolve would fail" >&2',
    'echo "image-cache-init: fix it once (as root on the node, or on the NFS server): chown $uid:$uid $dir && chown -R $uid:$uid $dir/_oci" >&2',
    "exit 1",
    "fi",
    'echo "image-cache-init: $dir is owned by uid $uid"',
    "done",
]

#: The volume-store half (D24), **exactly**: one `chown` of the target itself,
#: re-`stat`ed afterwards, with a FATAL that names the path and the one-time
#: command. Same list for all three stacks (only the env values differ).
STORE_HANDOVER_LINES = [
    "hand_over() {",
    'target="$1"',
    # Task 3: same rule for the store -- its uid comes from the volume it lives
    # on, which in the multinode lane is the control plane's own 65533.
    'uid_of "$target"',
    '[ -n "$uid" ] || refuse_foreign "$target"',
    'if [ ! -e "$target" ]; then',
    'echo "image-cache-init: $target does not exist -- nothing to hand over (the control plane creates it under its own uid when it first needs it)"',
    "return 0",
    "fi",
    'owner="$(stat -c %u "$target")"',
    'mode="$(stat -c %a "$target")"',
    'if [ "$owner" = "$uid" ]; then',
    'echo "image-cache-init: $target already belongs to uid $uid (mode $mode) -- nothing to do"',
    "return 0",
    "fi",
    'chown "$uid:$uid" "$target" 2>/dev/null ||',
    'echo "image-cache-init: chown refused ($target, NFS root_squash?) -- the control plane (uid $uid) cannot create or record a volume until this is done once: chown $uid:$uid $target" >&2',
    'owner="$(stat -c %u "$target")"',
    'mode="$(stat -c %a "$target")"',
    'if [ "$owner" != "$uid" ]; then',
    'echo "image-cache-init: FATAL: $target is owned by uid $owner, not the control-plane uid $uid: the control plane creates every volume as <store>/<volume_id> and writes <store>/_meta/<volume_id>.json, so the first volume create would fail with EACCES" >&2',
    'echo "image-cache-init: fix it once (as root on the node, or on the NFS server): chown $uid:$uid $target  (non-recursive: the volume data directories below it belong to pooled sandbox uids)" >&2',
    "return 1",
    "fi",
    'echo "image-cache-init: $target -> uid $owner mode $mode (a non-recursive hand-over; the sandbox volume data directories below it keep their pooled uids)"',
    "return 0",
    "}",
    "for store in $VOLUME_STORES; do",
    'if [ ! -e "$store" ]; then',
    'mkdir -p "$store"',
    'echo "image-cache-init: created $store (the platform\'s volume store)"',
    "fi",
    'hand_over "$store" || exit 1',
    'hand_over "$store/_meta" || exit 1',
    'store_owner="$(stat -c %u "$store")"',
    'echo "image-cache-init: $store is owned by uid $store_owner (the volume data directories below it are left alone)"',
    "done",
]


def _compose_init_lines(path: Path) -> list[str]:
    """The init body's executable lines, compose's `$$` already unescaped.

    Comments are dropped: this is what the *container* runs.
    """
    body = _compose_init_body(path).replace("$$", "$")
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _slice(lines: list[str], first: str, last: str) -> list[str]:
    """The lines from ``first`` through the *next* ``last`` (inclusive)."""
    start = lines.index(first)
    end = lines.index(last, start)
    return lines[start : end + 1]


def _compose_init(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["image-cache-init"]


def _compose_init_body(path: Path) -> str:
    """The compose init's shell body, one entry point, exactly as it ships."""
    command = _compose_init(path)["command"]
    assert isinstance(command, list) and len(command) == 1, command
    return command[0]


def _compose_init_env(path: Path) -> dict[str, str]:
    env = _compose_init(path)["environment"]
    if isinstance(env, list):
        return {k: v for k, v in (e.split("=", 1) for e in env if "=" in e)}
    return env


def test_each_compose_init_hands_the_volume_store_over_like_storage_init() -> None:
    """Gap 1, the manifest half: the compose lane's `storage-init` discipline.

    The k8s arm hands the store over in the agent pod's `storage-init` (D24).
    The compose stacks have no pod, so the same job belongs to the root one-shot
    that already prepares the volumes (`image-cache-init`) -- and it has to carry
    the *same* properties, because the failure mode is identical: the control
    plane runs as a non-root uid (per lane -- 65533 where it has its own volume,
    65534 on the single-volume prod shapes) and would otherwise hit `EACCES` on
    the first volume create.

    Pinned by text here and behaviourally below:

    * the store and its record directory are handed over by name;
    * **non-recursive** -- the volume data directories and quota slices below
      the store belong to pooled sandbox uids and must survive a rollout;
    * idempotent and audible (an already-owned target says so, and the run says
      which of the three things happened);
    * gated: each target is re-`stat`ed after the attempt, and a target that is
      still not the uid its volume resolves to is a FATAL that names the path and
      the one-time command.

    Also pinned here, exactly and separately: the **cache** half of the same
    discipline (review item 4). Its non-recursion is what keeps live
    `secrets/<sandbox_id>/<name>.secret` files with their sandbox uid -- and it
    is invisible to the behavioural arms below, which exercise the store, so a
    revert to `chown -R "$$dir"` has to fail *here*.
    """
    command_lists: list[list[str]] = []
    for path in COMPOSE_STACKS:
        body = _compose_init_body(path)
        lines = _compose_init_lines(path)
        command_lists.append(lines)
        env = _compose_init_env(path)
        store = VOLUME_STORE_ENV[path.relative_to(REPO).as_posix()]
        assert env["VOLUME_STORES"].split() == [store], path.name
        # Exact, ordered: the store's hand-over block, and the cache's.
        assert (
            _slice(lines, "hand_over() {", "done") == STORE_HANDOVER_LINES
        ), path.name
        assert (
            _slice(lines, "for dir in $CACHE_DIRS; do", "done")
            == CACHE_HANDOVER_LINES
        ), path.name
        # ...and the recursive form may not appear against either root: the
        # store's data directories *and* the sandbox `*.secret` files below the
        # cache both belong to pooled sandbox uids.
        assert 'chown -R 65534:65534 "$store' not in body, path.name
        assert 'chown -R 65534:65534 "$target' not in body, path.name
        assert 'chown -R 65534:65534 "$dir"' not in body, path.name
        # ...and the same service is where the rest of the ownership the control
        # plane's volume uid needs is made true (the k8s `workspace-root-init`
        # products), each verified by the same `stat` gate.
        assert env["OWNED_DIRS"].split(), path.name
        assert env["WRITABLE_ROOTS"].split(), path.name
    # Three stacks, one script: the commands are byte-identical (only the
    # `CACHE_DIRS`/`OWNED_DIRS`/`WRITABLE_ROOTS`/`VOLUME_STORES` values differ),
    # so a change made in one lane cannot silently miss the other two.
    assert command_lists[0] == command_lists[1] == command_lists[2]


def test_each_compose_init_drops_to_the_three_verbs_the_script_uses() -> None:
    """Review item 6: the root one-shot carries `storage-init`'s capability set.

    Its k8s sibling was tightened in review round 1 (`drop: [ALL]` + exactly
    `CHOWN`, `DAC_OVERRIDE`, `FOWNER`); the compose copies kept the runtime's
    default 14-capability set, which is the same widening class in the same
    class of container. The three are *measured* (`docs/deploy-clusters.md`
    §7.12): with `CHOWN`+`FOWNER` only, the script's first
    `mkdir -p "<65534-owned dir>/_oci"` is `EACCES` and `set -e` stops the
    deployment; with all three the same script runs green.
    """
    k8s_init = _init_containers(
        _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    )["storage-init"]
    k8s_caps = k8s_init["securityContext"]["capabilities"]
    assert k8s_caps == {
        "drop": ["ALL"],
        "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
    }
    for path in COMPOSE_STACKS:
        init = _compose_init(path)
        assert init["user"] == "0:0", path.name
        assert init["cap_drop"] == ["ALL"], path.name
        assert init["cap_add"] == ["CHOWN", "DAC_OVERRIDE", "FOWNER"], path.name


@pytest.mark.parametrize("path", COMPOSE_STACKS, ids=lambda p: p.parent.name)
def test_a_compose_store_that_hands_over_reports_success(
    tmp_path: Path, path: Path
) -> None:
    """The control arm: the store really is handed over, and says so.

    Run against a probe tree with the paths redirected through the script's own
    environment, because the two properties at issue (the gate exists, and a
    refusal cannot print a success line) are behavioural. The shims make the
    script deterministic on any host: `stat -c %u` answers from a *stateful*
    model of the two filesystems this script is written for (a target is
    `65534` once its `chown` succeeded), and `chown` refuses exactly the paths
    the arm asks it to.
    """
    result = _run_compose_init(
        tmp_path,
        path=path,
        foreign=("_volumes", "_volumes/_meta"),
    )

    assert result.returncode == 0
    shared = tmp_path / "shared"
    assert result.stdout.splitlines() == [
        f"image-cache-init: {shared}/_images is owned by uid 65534",
        f"image-cache-init: {shared}/_secrets is owned by uid 65534",
        f"image-cache-init: {shared} is writable by uid 65534 (owner=65534 mode=755)",
        f"image-cache-init: {shared}/_volumes -> uid 65534 mode 755 (a "
        "non-recursive hand-over; the sandbox volume data directories below it "
        "keep their pooled uids)",
        f"image-cache-init: {shared}/_volumes/_meta -> uid 65534 mode 755 (a "
        "non-recursive hand-over; the sandbox volume data directories below it "
        "keep their pooled uids)",
        f"image-cache-init: {shared}/_volumes is owned by uid 65534 (the volume "
        "data directories below it are left alone)",
    ]
    assert result.stderr == ""


@pytest.mark.parametrize("path", COMPOSE_STACKS, ids=lambda p: p.parent.name)
def test_a_compose_store_already_owned_is_a_named_no_op(
    tmp_path: Path, path: Path
) -> None:
    """Idempotence, in the direction a re-run of `up -d` takes.

    The second node of a shared mount (and every subsequent `up -d`) must find
    the store already handed over and say so, rather than re-chowning silently.
    """
    result = _run_compose_init(tmp_path, path=path, foreign=())

    assert result.returncode == 0
    shared = tmp_path / "shared"
    assert result.stdout.splitlines() == [
        f"image-cache-init: {shared}/_images is owned by uid 65534",
        f"image-cache-init: {shared}/_secrets is owned by uid 65534",
        f"image-cache-init: {shared} is writable by uid 65534 (owner=65534 mode=755)",
        f"image-cache-init: {shared}/_volumes already belongs to uid 65534 "
        "(mode 755) -- nothing to do",
        f"image-cache-init: {shared}/_volumes/_meta already belongs to uid 65534 "
        "(mode 755) -- nothing to do",
        f"image-cache-init: {shared}/_volumes is owned by uid 65534 (the volume "
        "data directories below it are left alone)",
    ]
    assert result.stderr == ""


@pytest.mark.parametrize("path", COMPOSE_STACKS, ids=lambda p: p.parent.name)
def test_a_compose_record_directory_that_refuses_cannot_read_as_a_hand_over(
    tmp_path: Path,
    path: Path,
) -> None:
    """The gate: `_meta` refused is a FATAL, not a success line.

    This is review round 1's Important from the k8s side, reproduced where the
    compose lane can regress the same way -- `_meta` used to be handed over
    without a gate, so an NFS `root_squash` refusal read as success *here* and
    surfaced much later, un-named, on the control plane's second write
    (`_write_record`). The store root still hands over in this arm, so a pin
    that refused both would not show it.
    """
    result = _run_compose_init(
        tmp_path, path=path, foreign=(), refuse=("_volumes/_meta",)
    )

    assert result.returncode == 1
    shared = tmp_path / "shared"
    # ...the store root's own state is still read and reported (here it is the
    # shape a re-run on a healthy node has), the refused record directory
    # printed **no** success line, and nothing else was printed either.
    assert result.stdout.splitlines() == [
        f"image-cache-init: {shared}/_images is owned by uid 65534",
        f"image-cache-init: {shared}/_secrets is owned by uid 65534",
        f"image-cache-init: {shared} is writable by uid 65534 (owner=65534 mode=755)",
        f"image-cache-init: {shared}/_volumes already belongs to uid 65534 "
        "(mode 755) -- nothing to do",
    ]
    assert result.stderr.splitlines() == [
        f"image-cache-init: chown refused ({shared}/_volumes/_meta, NFS "
        "root_squash?) -- the control plane (uid 65534) cannot create or record "
        "a volume until this is done once: chown 65534:65534 "
        f"{shared}/_volumes/_meta",
        f"image-cache-init: FATAL: {shared}/_volumes/_meta is owned by uid 0, "
        "not the control-plane uid 65534: the control plane creates every "
        "volume as <store>/<volume_id> and writes "
        "<store>/_meta/<volume_id>.json, so the first volume create would fail "
        "with EACCES",
        "image-cache-init: fix it once (as root on the node, or on the NFS "
        f"server): chown 65534:65534 {shared}/_volumes/_meta  (non-recursive: "
        "the volume data directories below it belong to pooled sandbox uids)",
    ]


def _run_compose_init(
    tmp_path: Path,
    *,
    foreign: tuple[str, ...],
    refuse: tuple[str, ...] = (),
    path: Path = COMPOSE_STACKS[0],
) -> subprocess.CompletedProcess:
    """Run a compose `image-cache-init` body under `sh` against a probe tree.

    The script is the compose lane's copy of the k8s `storage-init` discipline,
    so it gets the same treatment `_run_storage_init` gives that one. Three
    shims make it deterministic on any host (the script's own tools are
    Linux-shaped, and macOS `stat` has no `-c`):

    * ``stat -c %u|%a <path>`` -- the uid comes from a stateful model: a target
      starts at ``65534`` unless it matches ``foreign``, and a successful
      ``chown`` of it moves it to ``65534`` for good (a marker file), while a
      ``refuse``d path stays at 0 and is never handed over;
    * ``chown`` -- succeeds (and records the marker), except for ``refuse``.

    ``foreign`` and ``refuse`` are matched as *path suffixes* so the arms can
    name the store and its record directory without knowing the tmp path.
    """
    shared = tmp_path / "shared"
    (shared / "_images" / "_oci").mkdir(parents=True)
    (shared / "_secrets").mkdir()
    (shared / "_volumes" / "_meta").mkdir(parents=True)
    for directory in (
        shared,
        shared / "_images",
        shared / "_secrets",
        shared / "_volumes",
        shared / "_volumes" / "_meta",
    ):
        directory.chmod(0o755)
    markers = tmp_path / "markers"
    markers.mkdir()
    shims = tmp_path / "shims"
    shims.mkdir()
    # The ownership model both shims share: a path is 65534 unless the arm says
    # it is `foreign`, and a successful `chown` of a foreign path moves it to
    # 65534 for the rest of the run (the marker file).
    model = (
        "import os, sys\n"
        "path = PATH_ARG\n"
        "def matches(key):\n"
        "    return [s for s in os.environ.get(key, '').split(':') if s and "
        "path.endswith(s)]\n"
        "marker = os.environ['PROBE_MARKERS'] + '/' + path.replace('/', '_')\n"
        "def owned():\n"
        "    if matches('PROBE_REFUSE'):\n"
        "        return False\n"
        "    return os.path.exists(marker) or not matches('PROBE_FOREIGN')\n"
    )
    _shim(
        shims,
        "stat",
        "exec python3 - \"$2\" \"$3\" <<'PY'\n"
        + model.replace("PATH_ARG", "sys.argv[2]")
        + "fmt = sys.argv[1]\n"
        "if '%u' in fmt:\n"
        "    print(65534 if owned() else 0)\n"
        "else:\n"
        "    print(format(os.stat(path).st_mode & 0o7777, 'o'))\n"
        "PY\n",
    )
    # `chown [-R] 65534:65534 <path>`: the last argument is the target, whether
    # or not the recursive flag is present.
    _shim(
        shims,
        "chown",
        "exec python3 - \"$@\" <<'PY'\n"
        + model.replace("PATH_ARG", "sys.argv[-1]")
        + "if matches('PROBE_REFUSE'):\n"
        "    sys.stderr.write('chown: Operation not permitted\\n')\n"
        "    sys.exit(1)\n"
        "open(marker, 'w').close()\n"
        "PY\n",
    )
    script = tmp_path / "image-cache-init.sh"
    # Compose's own escaping, applied the way `docker compose` does it when it
    # renders this service's entrypoint (`$$` is one literal `$` in the
    # container). Without it the shell would read `$$dir` as "the pid, then
    # `dir`" and the run would be about something else entirely.
    script.write_text(_compose_init_body(path).replace("$$", "$"), encoding="utf-8")
    environment = dict(os.environ)
    environment["PATH"] = f"{shims}{os.pathsep}{environment['PATH']}"
    # Task 3: the script reads each target's uid off the volume it lives on, so
    # the probe declares its one tree as both roots with the one uid the three
    # single-volume lanes use. (The multinode lane's two-volume split is pinned
    # in `test_control_plane_uid.py`, which runs the same script against a
    # control tree and a worker tree.)
    environment["CONTROL_ROOT"] = str(shared)
    environment["CONTROL_UID"] = "65534"
    environment["WORKER_ROOT"] = str(shared)
    environment["WORKER_UID"] = "65534"
    environment["CACHE_DIRS"] = str(shared / "_images")
    environment["OWNED_DIRS"] = str(shared / "_secrets")
    environment["WRITABLE_ROOTS"] = str(shared)
    environment["VOLUME_STORES"] = str(shared / "_volumes")
    environment["PROBE_MARKERS"] = str(markers)
    environment["PROBE_FOREIGN"] = ":".join(foreign)
    environment["PROBE_REFUSE"] = ":".join(refuse)
    return subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
