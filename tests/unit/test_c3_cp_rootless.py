"""C3 Task 5 (ruling **D24**): the control plane converges to no root.

Task 5's deliverable is "CP 主容器 65534、CP pod 无 root 容器、A 类动作在 CP 侧清零"
(`plan §3`). The plan's §3.2 letter says the CP's remaining A-class action — the
`_volumes` volume-root `mkdir` + `chmod 1777` — is *handed to the agent*. It is
not, and this file pins the decision that replaced it.

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

* the manifest half — the CP pod's main container is uid 65534, nothing in that
  pod runs as uid 0, and the once-root `image-cache-init` has moved to the
  agent's own pod (§3.2 step 3);
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
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
K8S = REPO / "deploy" / "k8s"
CONTROL_PLANE_MANIFEST = K8S / "control-plane.yaml"
AGENT_MANIFEST = K8S / "c3-agent.yaml"
VOLUMES_SOURCE = REPO / "control_plane" / "registry" / "volumes.py"
MAINT_C = REPO / "deploy" / "priv" / "maint.c"

#: The control plane's uid. §13.6's三条 findings collapse to this one value:
#: the platform's own directories are already 65534, `.uid_pool.lock` is
#: `65534:65534 0600` (a different uid cannot even open it) and the image cache
#: owner is 65534 (a different uid would lock itself out of what it creates).
CP_UID = 65534
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


# ------------------------------------------------ the control-plane pod itself


def test_the_control_plane_container_runs_as_the_worker_uid() -> None:
    """判据 8, first half: main container `runAsUser == 65534`.

    `runAsGroup` is pinned with it: the platform's own state is `65534:65534`
    and `.uid_pool.lock` is `0600`, so the uid is the load-bearing half — but a
    container that runs `65534:0` would create every record with the root
    group, and the two are one decision.
    """
    deployment = _only(_load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane")
    cp = _containers(deployment)["control-plane"]
    security = cp["securityContext"]
    assert security["runAsUser"] == CP_UID
    assert security["runAsGroup"] == CP_UID


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
        'hand_over "$volume_store"',
        'hand_over "$volume_store/_meta"',
        # ...one directory at a time, never `-R` (the recursive form would take
        # the sandbox volume data directories back on every agent rollout).
        'chown 65534:65534 "$target" 2>/dev/null ||',
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
        'if [ "$store_owner" != "65534" ]; then',
    ):
        assert expected in lines, expected
    assert "chown -R 65534:65534 \"$SHARED_ROOT/_volumes" not in script
    assert "chown -R 65534:65534 \"$target" not in script
    # Visible, in both directions: the already-owned branch and the refusal.
    assert "already belongs to uid 65534" in script
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
    assert env["CACHE_DIRS"] == f"{SHARED_ROOT}/_images"
    # ...and the control plane's own store, from the app's wiring: the registry
    # is built on `<shared root>/_volumes` when no explicit root is configured
    # (`control_plane/app.py`). Pinned as the same literal.
    app_source = (REPO / "control_plane" / "app.py").read_text(encoding="utf-8")
    assert '"_volumes"' in app_source


# ------------------------------------------------------- the CP code path (A3)


def test_the_privileged_verb_vocabulary_was_not_widened_for_a3() -> None:
    """D24's code half: no `mkdir`/`chmod` verb was added to `e2b-maint`.

    Adding one would have been the plan's letter (§3.2 step 2 hands A3 to the
    agent), and it is exactly what D24 rules out: a new verb in the **root**
    file face for an operation the CP can perform as the store's owner.
    """
    from deploy.c3_agent.fileops import FILE_OP_VERBS

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
        f"cannot create the volume directory {created}: the volume store {store} "
        f"is not writable by this control plane (uid {os.geteuid()}). It is "
        "handed over once, non-recursively -- the directories below it are "
        "sandbox volume data owned by pooled sandbox uids -- by the agent's "
        f'storage-init: chown 65534:65534 "{store}" "{store}/_meta"'
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
