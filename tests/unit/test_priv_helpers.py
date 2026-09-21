"""Task F1 (Track F): the file-capability broker wiring, at the Python layer.

The two production brokers are compiled C binaries
(``deploy/priv/slot_spawn.c`` / ``deploy/priv/maint.c``) that run as uid 65534
and hold their capability **as a file capability** -- that is what lets a
non-root worker start a route-B slot at an arbitrary pooled uid and manage
sandbox-owned trees. Everything the worker *asks* of them is built and
validated here, so the refusals are pinned off-Linux:

* uid outside the configured pool (both verbs -- never uid 0);
* a path outside ``<workspace_base>/`` / ``<shared_volume_root>/`` after
  ``realpath`` (``..`` and symlink escapes included);
* the ``spawn`` program pinned to the absolute ``sandlock-supervise`` path
  (the broker is not a general "run something as uid X" tool);
* the startup self-check refusing to *claim* the broker shape when a broker
  pair is half-installed (one binary missing, capability stripped,
  reachable from a sandbox) -- a deployment defect is named, never silently
  downgraded.

The C side enforces the same rules; the container lane is the evidence that
it does (``getcap`` + a real non-root container run).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from envd_service import priv_helpers as ph


def _helpers(tmp_path: Path, **overrides) -> ph.PrivHelpers:
    workspace = tmp_path / "sandboxes"
    shared = tmp_path / "shared"
    workspace.mkdir(exist_ok=True)
    shared.mkdir(exist_ok=True)
    fields = dict(
        slot_spawn=tmp_path / "e2b-priv" / "e2b-slot-spawn",
        maint=tmp_path / "e2b-priv" / "e2b-maint",
        supervise_bin=tmp_path
        / "site-packages"
        / "sandlock"
        / "bin"
        / "sandlock-supervise",
        uid_pool_start=10000,
        uid_pool_size=1000,
        workspace_base=workspace,
        shared_volume_root=shared,
    )
    fields.update(overrides)
    return ph.PrivHelpers(**fields)


def _roots_text(workspace_base: Path, shared_volume_root: Path) -> str:
    return f"{workspace_base}, {shared_volume_root}"


def _settings(tmp_path: Path, **overrides):
    from envd_service.config import Settings

    (tmp_path / "sandboxes").mkdir(exist_ok=True)
    (tmp_path / "shared").mkdir(exist_ok=True)
    fields = dict(
        priv_helpers="auto",
        workspace_base=tmp_path / "sandboxes",
        shared_volume_root=str(tmp_path / "shared"),
        uid_pool_start=10000,
        uid_pool_size=1000,
        route_b_tmp_root=tmp_path / "sandboxes" / ".route-b",
    )
    fields.update(overrides)
    return Settings(**fields)


def _install(
    tmp_path: Path,
    monkeypatch,
    *,
    binaries: tuple[str, ...] = ("e2b-slot-spawn", "e2b-maint"),
    caps: dict[str, int] | None = None,
    mode: int = ph.HELPER_DIR_MODE,
    file_mode: int = ph.HELPER_FILE_MODE,
) -> Path:
    """Fake broker files + a stubbed capability reader.

    The real ``security.capability`` xattr is exercised in the container lane
    (``getcap``) and by :func:`test_file_capability_xattr_round_trips`; here
    the reader is stubbed so the resolver's rules stay observable off-Linux.
    """
    helper_dir = tmp_path / "e2b-priv"
    helper_dir.mkdir(mode=mode, exist_ok=True)
    helper_dir.chmod(mode)
    os.chown(helper_dir, 0, os.getegid())
    for name in binaries:
        target = helper_dir / name
        target.write_bytes(b"\x7fELF")
        target.chmod(file_mode)
        os.chown(target, 0, os.getegid())
    caps = (
        caps
        if caps is not None
        else {
            "e2b-slot-spawn": ph.CAP_SETUID_MASK | ph.CAP_SETGID_MASK,
            "e2b-maint": ph.CAP_CHOWN_MASK | ph.CAP_DAC_OVERRIDE_MASK,
        }
    )

    def _fake_read(path: Path) -> ph.FileCapabilities:
        mask = caps.get(path.name)
        if mask is None:
            raise ph.PrivHelperError(f"{path} has no security.capability xattr")
        return ph.FileCapabilities(effective=mask, permitted=mask, inheritable=0)

    monkeypatch.setattr(ph, "read_file_capabilities", _fake_read)
    monkeypatch.setattr(ph, "DEFAULT_HELPER_DIR", helper_dir)
    return helper_dir


# ------------------------------------------------------------------ uid pool


def test_uid_outside_the_configured_pool_is_refused(tmp_path: Path) -> None:
    helpers = _helpers(tmp_path)
    assert helpers.validate_uid(10000) == 10000
    assert helpers.validate_uid(10999) == 10999
    for uid in (9999, 11000, 0):
        with pytest.raises(ph.PrivHelperError) as excinfo:
            helpers.validate_uid(uid)
        assert str(excinfo.value) == (
            f"uid {uid} is outside the privileged helper uid pool 10000..10999"
        )


def test_spawn_argv_refuses_a_uid_without_a_matching_gid(tmp_path: Path) -> None:
    helpers = _helpers(tmp_path)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.spawn_argv(
            uid=10001, gid=10002, supervise_args=["--policy", "p.json"]
        )
    assert str(excinfo.value) == (
        "e2b-slot-spawn starts one host identity: uid 10001 and gid 10002 "
        "must match"
    )


# ------------------------------------------------------------------- paths


@pytest.mark.parametrize(
    "escape",
    [
        pytest.param("absolute", id="absolute"),
        pytest.param("dotdot", id="dotdot"),
        pytest.param("symlink", id="symlink"),
    ],
)
def test_path_escape_from_the_privileged_roots_is_refused(
    tmp_path: Path, escape: str
) -> None:
    helpers = _helpers(tmp_path)
    if escape == "absolute":
        raw: Path = Path("/etc/passwd")
    elif escape == "dotdot":
        raw = helpers.workspace_base / ".." / "escaped"
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_text("x", encoding="utf-8")
        link = helpers.workspace_base / "loop"
        link.symlink_to(outside, target_is_directory=True)
        raw = link / "secret"
    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.resolve_path(raw)
    assert str(excinfo.value) == (
        f"path {raw} is outside the privileged helper roots "
        f"({_roots_text(helpers.workspace_base, helpers.shared_volume_root)})"
    )


def test_workspace_and_shared_volume_paths_resolve_inside(tmp_path: Path) -> None:
    helpers = _helpers(tmp_path)
    inside_workspace = helpers.workspace_base / "sbx_a"
    inside_workspace.mkdir()
    assert helpers.resolve_path(inside_workspace) == inside_workspace
    inside_shared = helpers.shared_volume_root / "vol"
    inside_shared.mkdir()
    assert helpers.resolve_path(inside_shared) == inside_shared


# ------------------------------------------------------------------ argv[0]


def test_spawn_program_must_be_the_pinned_supervise_absolute_path(
    tmp_path: Path,
) -> None:
    helpers = _helpers(tmp_path)
    assert (
        helpers.validate_spawn_program(str(helpers.supervise_bin))
        == str(helpers.supervise_bin)
    )
    for program in ("sandlock-supervise", "/bin/sh"):
        with pytest.raises(ph.PrivHelperError) as excinfo:
            helpers.validate_spawn_program(program)
        assert str(excinfo.value) == (
            "the spawned program must be the absolute path "
            f"{helpers.supervise_bin} (got {program!r}): e2b-slot-spawn is not "
            "a general run-as-uid-X launcher"
        )


def test_spawn_argv_shape_and_env_carry_the_pool(tmp_path: Path) -> None:
    helpers = _helpers(tmp_path)
    policy = helpers.workspace_base / "sbx_a" / "policy.json"
    argv = helpers.spawn_argv(
        uid=10007,
        supervise_args=["--policy", str(policy), "--uid", "10007", "--serve"],
    )
    assert argv == [
        str(helpers.slot_spawn),
        "spawn",
        "--uid",
        "10007",
        "--gid",
        "10007",
        "--",
        str(helpers.supervise_bin),
        "--policy",
        str(policy),
        "--uid",
        "10007",
        "--serve",
    ]
    env = helpers.subprocess_env()
    assert env["E2B_UID_POOL_START"] == "10000"
    assert env["E2B_UID_POOL_SIZE"] == "1000"
    assert env["E2B_WORKSPACE_BASE"] == str(helpers.workspace_base)
    assert env["E2B_SHARED_VOLUME_ROOT"] == str(helpers.shared_volume_root)
    assert env["E2B_SUPERVISE_BIN"] == str(helpers.supervise_bin)


def test_chown_rm_and_walk_argv_shape(tmp_path: Path) -> None:
    helpers = _helpers(tmp_path)
    target = helpers.workspace_base / "sbx_a"
    assert helpers.chown_argv(uid=10003, path=target, recursive=True) == [
        str(helpers.maint),
        "chown",
        "--uid",
        "10003",
        "--gid",
        "10003",
        "--recursive",
        "--path",
        str(target),
    ]
    assert helpers.chown_argv(uid=10003, path=target) == [
        str(helpers.maint),
        "chown",
        "--uid",
        "10003",
        "--gid",
        "10003",
        "--path",
        str(target),
    ]
    assert helpers.rm_argv(path=target) == [
        str(helpers.maint),
        "rm",
        "--path",
        str(target),
    ]
    assert helpers.walk_argv(path=target) == [
        str(helpers.maint),
        "walk",
        "--path",
        str(target),
    ]


def test_chown_argv_carries_the_workers_group_for_the_c1_model(tmp_path: Path) -> None:
    """Fix round 1 (c1): `0770 owner=<sandbox uid> group=<worker gid>`."""
    helpers = _helpers(tmp_path)
    target = helpers.workspace_base / "sbx_a"
    assert helpers.chown_argv(uid=10003, gid=os.getegid(), path=target) == [
        str(helpers.maint),
        "chown",
        "--uid",
        "10003",
        "--gid",
        str(os.getegid()),
        "--path",
        str(target),
    ]
    # A pooled gid is still allowed (the root/legacy `X:X` shape).
    assert helpers.validate_chown_gid(10003) == 10003
    assert helpers.validate_chown_gid(os.getegid()) == os.getegid()
    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.validate_chown_gid(4242)
    assert str(excinfo.value) == (
        "gid 4242 is neither the worker's own gid "
        f"({os.getegid()}) nor a member of the privileged helper uid pool "
        "10000..10999"
    )


def test_chown_refuses_a_uid_outside_the_pool(tmp_path: Path) -> None:
    helpers = _helpers(tmp_path)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.chown_argv(uid=0, path=helpers.workspace_base / "sbx_a")
    assert (
        str(excinfo.value)
        == "uid 0 is outside the privileged helper uid pool 10000..10999"
    )


def test_delete_and_chown_never_target_a_whole_managed_root(tmp_path: Path) -> None:
    helpers = _helpers(tmp_path)
    for build in (helpers.rm_argv, helpers.chown_worker_argv):
        with pytest.raises(ph.PrivHelperError) as excinfo:
            build(path=helpers.workspace_base)
        assert str(excinfo.value) == (
            f"path {helpers.workspace_base} is outside the privileged helper "
            f"roots ({_roots_text(helpers.workspace_base, helpers.shared_volume_root)})"
        )


def test_reclaim_hands_the_orphan_back_to_the_workers_own_identity(
    tmp_path: Path,
) -> None:
    helpers = _helpers(tmp_path)
    orphan = helpers.workspace_base / "sbx_orphan"
    assert helpers.chown_worker_argv(path=orphan, recursive=True) == [
        str(helpers.maint),
        "chown",
        "--worker",
        "--recursive",
        "--path",
        str(orphan),
    ]


def test_walk_output_is_parsed_with_owner_and_size(tmp_path: Path) -> None:
    entry = ph.WalkEntry.parse("f 10002 10002 600 12 /var/lib/e2b-sandboxes/a/b.txt")
    assert entry == ph.WalkEntry(
        kind="f",
        uid=10002,
        gid=10002,
        mode=0o600,
        size=12,
        path="/var/lib/e2b-sandboxes/a/b.txt",
    )


# -------------------------------------------------------------- self-check


def test_missing_helpers_keep_the_worker_in_process_and_name_the_binary(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    monkeypatch.setattr(ph, "DEFAULT_HELPER_DIR", tmp_path / "e2b-priv")
    settings = _settings(tmp_path)
    assert ph.resolve_priv_helpers(settings) is None
    assert ph.helpers_unavailable_reason(settings) == (
        f"E2B_PRIV_HELPERS=auto on a non-root worker, but "
        f"{tmp_path / 'e2b-priv' / 'e2b-slot-spawn'} is missing: this worker "
        "keeps the in-process (E5.1) shape; ship the file-capability brokers "
        "to get per-sandbox host uids and route-B slots"
    )


def test_a_half_installed_broker_pair_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    helper_dir = _install(tmp_path, monkeypatch, binaries=("e2b-slot-spawn",))
    settings = _settings(tmp_path)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(settings)
    assert str(excinfo.value) == (
        f"{helper_dir / 'e2b-maint'} is missing while "
        f"{helper_dir / 'e2b-slot-spawn'} is present: a partial broker "
        "install must not be guessed at"
    )


def test_a_helper_without_its_file_capabilities_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    helper_dir = _install(
        tmp_path,
        monkeypatch,
        caps={"e2b-slot-spawn": 0, "e2b-maint": ph.CAP_CHOWN_MASK},
    )
    settings = _settings(tmp_path)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(settings)
    assert str(excinfo.value) == (
        f"{helper_dir / 'e2b-slot-spawn'} is missing the file capabilities "
        "['CAP_SETGID', 'CAP_SETUID'] (found []): run "
        "`setcap cap_setuid,cap_setgid+ep` in the final image stage "
        "(`COPY --from` does not preserve the xattr)"
    )


def test_a_sandbox_reachable_helper_directory_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    helper_dir = _install(tmp_path, monkeypatch, mode=0o755)
    settings = _settings(tmp_path)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(settings)
    assert str(excinfo.value) == (
        f"{helper_dir} must be root-owned mode 0710 so the worker can exec "
        "the brokers while no sandbox uid can: got owner "
        f"0, group {os.getegid()}, mode 0755"
    )


def test_a_worker_inaccessible_broker_directory_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """A root-owned 0700 directory is *not* the answer: the worker cannot
    traverse it either, so the brokers become dead weight."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    helper_dir = _install(tmp_path, monkeypatch, mode=0o700)
    settings = _settings(tmp_path)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(settings)
    assert str(excinfo.value) == (
        f"{helper_dir} must be root-owned mode 0710 so the worker can exec "
        "the brokers while no sandbox uid can: got owner "
        f"0, group {os.getegid()}, mode 0700"
    )


def test_a_root_worker_keeps_the_unprivileged_brokers_unused(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _install(tmp_path, monkeypatch)
    assert ph.resolve_priv_helpers(_settings(tmp_path)) is None


def test_off_keeps_todays_in_process_behavior(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    _install(tmp_path, monkeypatch)
    settings = _settings(tmp_path, priv_helpers="off")
    assert ph.resolve_priv_helpers(settings) is None


def test_the_workspace_mode_is_the_c1_shape() -> None:
    assert ph.WORKSPACE_MODE == 0o770


@pytest.mark.parametrize(
    "uid, gid, expected",
    [
        pytest.param(65534, 65534, None, id="outside-pool"),
        pytest.param(9999, 65534, None, id="just-below-the-pool"),
        pytest.param(11000, 65534, None, id="just-above-the-pool"),
        pytest.param(
            10000,
            65534,
            "the sandbox uid pool 10000..10999 contains the worker's own uid "
            "(10000): a sandbox would share the worker's identity and could "
            "read every other sandbox's 0770 workspace (the group model relies "
            "on the sandbox gid differing from the worker's); move "
            "E2B_UID_POOL_START/SIZE off 10000",
            id="worker-uid-in-pool",
        ),
        pytest.param(
            65534,
            10999,
            "the sandbox gid pool 10000..10999 contains the worker's own gid "
            "(10999): a sandbox would share the worker's identity and could "
            "read every other sandbox's 0770 workspace (the group model relies "
            "on the sandbox gid differing from the worker's); move "
            "E2B_UID_POOL_START/SIZE off 10999",
            id="worker-gid-in-pool",
        ),
    ],
)
def test_the_worker_identity_must_stay_outside_the_pool(
    uid: int, gid: int, expected: str | None
) -> None:
    """Fix round 1 (c1) guard: a pooled worker identity would defeat `0770`.

    A sandbox allocated the worker's own uid/gid would be inside the worker's
    trust boundary and could read every other sandbox's workspace, so the
    worker refuses to start unless it is named out of the pool.
    """
    if expected is None:
        ph.check_worker_identity_outside_pool(
            uid=uid, gid=gid, start=10000, size=1000
        )
        return
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.check_worker_identity_outside_pool(
            uid=uid, gid=gid, start=10000, size=1000
        )
    assert str(excinfo.value) == expected


def test_the_resolver_applies_the_worker_identity_guard(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 10005)
    monkeypatch.setattr(os, "getegid", lambda: 10005)
    _install(tmp_path, monkeypatch)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(_settings(tmp_path))
    assert str(excinfo.value) == (
        "the sandbox uid pool 10000..10999 contains the worker's own uid "
        "(10005): a sandbox would share the worker's identity and could read "
        "every other sandbox's 0770 workspace (the group model relies on the "
        "sandbox gid differing from the worker's); move "
        "E2B_UID_POOL_START/SIZE off 10005"
    )


def test_remove_and_modes_are_in_process_first(tmp_path: Path, monkeypatch) -> None:
    """c1 shrinks the broker's verb surface: the worker's own group access
    handles ordinary trees, so no broker is needed (and none is configured
    here)."""
    tree = tmp_path / "sandboxes" / "sbx_a"
    (tree / "workspace").mkdir(parents=True)
    (tree / "workspace" / "a.txt").write_bytes(b"0123456789")
    # N31 fix 2: the 10 file bytes plus each directory's own `st_size` (on this
    # NFS a directory is 16 KiB of real space), asserted against an `os.stat`
    # oracle rather than the implementation's own probe.
    assert ph.dir_size(tree) == (
        10
        + os.stat(tree).st_blocks * 512
        + os.stat(tree / "workspace").st_blocks * 512
    )
    ph.remove_tree(tree)
    assert not tree.exists()


def test_the_maint_fallback_runs_for_a_tree_the_worker_cannot_remove(
    tmp_path: Path, monkeypatch
) -> None:
    """W7-4: the fallback branch of ``remove_tree`` used to die on its first log
    line.

    ``remove_tree`` hands a tree the worker's own DAC cannot remove to
    ``e2b-maint`` -- the whole reason the maintenance broker exists (a
    sandbox-made ``0700`` subdirectory, a sealed ``0555`` one, a root-owned
    leftover). This module had no ``logger`` at all, so the ``logger.info``
    that announces the hand-off raised ``NameError`` *before* ``broker_remove``
    ran: the broker was never executed, and every teardown path (the delete
    endpoint, the orphan-tree GC, the volume-slice cleanup) turned the shape
    into a 500 that kept the tree. The assertion is the broker *call*, not the
    return value.
    """
    import shutil

    tree = tmp_path / "sandboxes" / "sbx_sealed"
    (tree / "workspace").mkdir(parents=True)

    def deny(*args, **kwargs) -> None:
        raise PermissionError(13, "Permission denied", str(tree))

    monkeypatch.setattr(shutil, "rmtree", deny)
    helpers = _helpers(tmp_path)
    monkeypatch.setattr(ph, "_ACTIVE", [helpers])
    removed: list[str] = []
    monkeypatch.setattr(
        ph.PrivHelpers, "remove", lambda self, path: removed.append(str(path))
    )

    ph.remove_tree(tree)

    assert removed == [str(tree)]


def test_remove_tree_raises_when_neither_side_can_remove_the_tree(
    tmp_path: Path, monkeypatch
) -> None:
    """W7-2's confirmation needs the failure to reach the caller.

    ``on_error="raise"`` is what the teardown paths use: no broker (or a
    broker that refuses) must surface as a failure instead of a silent partial
    delete that leaves a tree with no readable record behind.
    """
    import shutil

    tree = tmp_path / "sandboxes" / "sbx_sealed"
    (tree / "workspace").mkdir(parents=True)

    def deny(*args, **kwargs) -> None:
        raise PermissionError(13, "Permission denied", str(tree))

    monkeypatch.setattr(shutil, "rmtree", deny)
    monkeypatch.setattr(ph, "_ACTIVE", [None])

    with pytest.raises(PermissionError) as excinfo:
        ph.remove_tree(tree, on_error="raise")

    assert str(excinfo.value) == f"[Errno 13] Permission denied: '{tree}'"
    assert tree.is_dir()


def test_create_app_refuses_a_pool_that_contains_the_worker_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """The guard is wired into startup for **every** worker shape (a root
    worker's group model is just as load-bearing), so a bad pool fails the
    worker with the reason instead of quietly shipping the hole."""
    from envd_service.app import create_app
    from envd_service.runtime.registry import RuntimeRegistry

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getegid", lambda: 10050)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = _settings(
        tmp_path,
        priv_helpers="off",
        workspace_base=workspace,
        uid_pool_start=10000,
        uid_pool_size=1000,
    )
    with pytest.raises(ph.PrivHelperError) as excinfo:
        create_app(settings=settings, runtime_registry=RuntimeRegistry(workspace))
    assert str(excinfo.value) == (
        "the sandbox gid pool 10000..10999 contains the worker's own gid "
        "(10050): a sandbox would share the worker's identity and could read "
        "every other sandbox's 0770 workspace (the group model relies on the "
        "sandbox gid differing from the worker's); move "
        "E2B_UID_POOL_START/SIZE off 10050"
    )


def test_the_broker_shape_requires_pooled_uids(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    _install(tmp_path, monkeypatch)
    settings = _settings(tmp_path, per_sandbox_uid=False)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(settings)
    assert str(excinfo.value) == (
        "E2B_PRIV_HELPERS needs E2B_PER_SANDBOX_UID: without a pooled sandbox "
        "uid the sandbox would run as the worker's own identity, which is "
        "exactly the gid that can exec the brokers (whoever can exec "
        "e2b-slot-spawn holds cap_setuid)"
    )


def test_the_broker_shape_requires_route_b(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    _install(tmp_path, monkeypatch)
    settings = _settings(tmp_path, route_b="off")
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(settings)
    assert str(excinfo.value) == (
        "E2B_PRIV_HELPERS needs E2B_ROUTE_B != off: a non-root worker cannot "
        "remap a sandbox in-process (S1.2), so the broker-started slot is the "
        "only way to run as the pooled host uid"
    )


def test_the_route_b_scratch_root_must_be_reachable_by_the_maintenance_broker(
    tmp_path: Path, monkeypatch
) -> None:
    """The slot's policy/program documents are group-scoped to the slot uid
    through ``e2b-maint`` (0440 root:<uid>), and the broker only touches paths
    under its whitelist roots. A scratch root outside them would leave the
    policy -- which carries egress-proxy credentials -- world-readable, so the
    broker shape refuses it by name instead of shipping the leak."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    _install(tmp_path, monkeypatch)
    settings = _settings(tmp_path, route_b_tmp_root=Path("/tmp/sandlock-route-b"))
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(settings)
    assert str(excinfo.value) == (
        "route-B scratch root /tmp/sandlock-route-b is outside the privileged "
        f"helper roots ({_roots_text(settings.workspace_base, tmp_path / 'shared')}): "
        "the slot documents are group-scoped to the slot uid through "
        "e2b-maint, so point E2B_ROUTE_B_TMP_ROOT at the workspace base"
    )


def test_a_complete_broker_pair_resolves(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    helper_dir = _install(tmp_path, monkeypatch)
    supervise = (
        tmp_path / "site-packages" / "sandlock" / "bin" / "sandlock-supervise"
    )
    import envd_service.route_b as route_b

    monkeypatch.setattr(route_b, "default_supervise_bin", lambda: supervise)
    helpers = ph.resolve_priv_helpers(_settings(tmp_path))
    assert helpers is not None
    assert helpers.slot_spawn == helper_dir / "e2b-slot-spawn"
    assert helpers.maint == helper_dir / "e2b-maint"
    assert helpers.supervise_bin == supervise
    assert helpers.uid_pool_start == 10000
    assert helpers.uid_pool_size == 1000
    assert helpers.workspace_base == tmp_path / "sandboxes"
    assert helpers.shared_volume_root == tmp_path / "shared"


# ------------------------------------------------------------- capability IO


def test_file_capability_xattr_round_trips() -> None:
    mask = ph.CAP_SETUID_MASK | ph.CAP_SETGID_MASK
    decoded = ph.decode_file_capabilities(
        ph.encode_file_capabilities(effective=mask, permitted=mask)
    )
    assert decoded.permitted == mask
    assert decoded.effective == mask
    assert decoded.inheritable == 0
    assert ph.capability_names(decoded.permitted) == frozenset(
        {"CAP_SETUID", "CAP_SETGID"}
    )


def test_capability_names_covers_the_broker_sets() -> None:
    assert ph.capability_names(ph.CAP_CHOWN_MASK) == frozenset({"CAP_CHOWN"})
    assert ph.capability_names(ph.CAP_DAC_OVERRIDE_MASK) == frozenset(
        {"CAP_DAC_OVERRIDE"}
    )
    assert ph.capability_names(0) == frozenset()


def test_the_broker_spawner_hands_over_the_events_descriptor(tmp_path, monkeypatch):
    """N25's pushed-events fd must reach the slot through the broker too.

    ``W1SlotPool`` passes ``events_fd`` unconditionally
    (``envd_service/route_b.py``), and a **non-root** worker -- the production
    compose shape -- reaches route B through ``PrivHelpers.slot_spawner``
    instead of the root ``setpriv`` form. When this signature lacked the
    parameter, every slot start on such a worker raised

        TypeError: PrivHelpers.slot_spawner() got an unexpected keyword
        argument 'events_fd'

    the sandbox's command then exited 127, and nothing in the fast lane
    noticed: it took the unprivileged phase of ``test-prod-shaped.sh``
    (measured 2026-09-21). The descriptor handoff is pinned here so the next
    refactor of either spawner cannot drop it silently.
    """
    helpers = ph.PrivHelpers(
        slot_spawn=tmp_path / "e2b-slot-spawn",
        maint=tmp_path / "e2b-maint",
        supervise_bin=tmp_path / "sandlock-supervise",
        uid_pool_start=10000,
        uid_pool_size=10,
        workspace_base=tmp_path,
    )
    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["pass_fds"] = kwargs.get("pass_fds")

    monkeypatch.setattr(ph.subprocess, "Popen", FakePopen)
    helpers.slot_spawner(
        uid=10000,
        policy_path=tmp_path / "policy.json",
        program_path=tmp_path / "program.json",
        name="slot",
        token="t",
        worker_uid=65534,
        control_fd=7,
        events_fd=8,
    )

    argv = captured["argv"]
    assert argv[argv.index("--control-fd") + 1] == "7"
    assert argv[argv.index("--events-fd") + 1] == "8"
    # Both must survive the broker's execve: the slot reads the *numbers* it
    # was handed, so a descriptor left out of `pass_fds` closes at exec.
    assert captured["pass_fds"] == (7, 8)


def test_the_broker_spawner_without_events_hands_over_control_only(
    tmp_path, monkeypatch
):
    """The older shape (no events channel) still starts a slot."""
    helpers = ph.PrivHelpers(
        slot_spawn=tmp_path / "e2b-slot-spawn",
        maint=tmp_path / "e2b-maint",
        supervise_bin=tmp_path / "sandlock-supervise",
        uid_pool_start=10000,
        uid_pool_size=10,
        workspace_base=tmp_path,
    )
    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["pass_fds"] = kwargs.get("pass_fds")

    monkeypatch.setattr(ph.subprocess, "Popen", FakePopen)
    helpers.slot_spawner(
        uid=10000,
        policy_path=tmp_path / "policy.json",
        program_path=tmp_path / "program.json",
        name="slot",
        token="t",
        worker_uid=65534,
        control_fd=7,
    )

    assert "--events-fd" not in captured["argv"]
    assert captured["pass_fds"] == (7,)
