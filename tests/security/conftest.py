"""Sandlock availability helpers for security tests."""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
import tempfile
from pathlib import Path

import pytest

from tests.lane_identity import lane_identity_reporter


def sandlock_ready() -> bool:
    """True only on Linux with a working sandlock and Landlock ABI >= 6."""
    if sys.platform != "linux":
        return False
    try:
        import sandlock

        return sandlock.landlock_abi_version() >= 6
    except Exception:
        return False


def make_sandbox_visible(*paths: str | Path) -> None:
    """Give the sandbox host uid a path it can resolve.

    ``tempfile.mkdtemp`` and pytest ``tmp_path`` trees are 0700 and owned by
    the test runner, so a sandbox that runs as uid 1000 cannot walk through
    them to reach its own workspace or image rootfs -- ``sandlock_create``
    then fails for a reason unrelated to the behaviour under test (and
    ``exit_code != 0`` denial assertions would pass vacuously). Real workers
    put sandboxes under a 0755 ``workspace_base``, so widen the same way here.

    Best effort, and only additive: a directory "other" can already enter ends
    the walk, and one we are not allowed to change stops it (macOS refuses
    ``chmod`` on parts of the per-user temp tree, and the tests that would care
    about that skip there anyway).
    """
    for raw in paths:
        candidate = Path(raw)
        while candidate != candidate.parent:
            try:
                mode = candidate.stat().st_mode
            except OSError:  # not created yet: the parent chain still matters
                candidate = candidate.parent
                continue
            if mode & 0o001:
                break
            try:
                os.chmod(candidate, mode | 0o055)
            except OSError:
                break
            candidate = candidate.parent


# A sandbox runs as this host uid unless the worker hands it a pooled uid
# (``E2B_UID_POOL_START`` range), see ``SandlockExecutor._run_as_identity``.
SANDBOX_UID = 1000


def sandbox_tmpdir(suffix: str = "", uid: int = SANDBOX_UID) -> Path:
    """A temporary workspace a sandbox can actually use.

    Mirrors what the worker does for a real sandbox directory: owned by the
    sandbox host uid (``apply_sandbox_ownership``) so in-sandbox writes land,
    and reachable through its parent chain. ``tempfile.mkdtemp`` gives
    neither -- it is 0700 and owned by the test runner.
    """
    path = Path(tempfile.mkdtemp(suffix=suffix))
    make_sandbox_visible(path)
    if os.geteuid() == 0:
        os.chown(path, uid, uid)
    os.chmod(path, 0o700)
    return path


def sandbox_owns_files_it_creates() -> tuple[bool, str]:
    """Can a sandbox chmod the files it writes in its own workspace?

    Returns ``(ok, detail)``. ``ok`` is False on worker storage where the file
    a sandbox creates is not owned by the sandbox identity: the child runs as
    the host uid from ``RunAs`` while the filesystem attributes the new file to
    the mount owner, so ``chmod``/``touch`` return EPERM. Docker runners on
    overlayfs (OrbStack/Docker Desktop) hit this; host-local XFS/ext4 storage
    -- the production shape -- does not.

    N15: through the deployment's entry point, like every other probe here. A
    hand-built executor cannot even create the sandbox on a root worker now
    (SL-1), and its ``exit_code=-1`` would have been read as "this storage does
    not support ownership" -- skipping the very tests this fixture guards.
    """
    executor, workspace = own_identity_sandbox(None, None, workspace=sandbox_tmpdir())
    try:
        code, _out, err = asyncio.run(
            run_sh(executor, workspace, "printf x > tool && chmod 700 tool && echo CHOWNED")
        )
    finally:
        executor.close()
    detail = err.decode("utf-8", "replace").strip()
    return code == 0, f"exit_code={code} stderr={detail!r}"


@pytest.fixture()
def require_sandbox_file_ownership():
    """Skip sandbox-storage tests this runner's filesystem cannot support."""
    if not sandlock_ready():
        pytest.skip("requires Linux with Landlock ABI >= 6")
    ok, detail = sandbox_owns_files_it_creates()
    if not ok:
        pytest.skip(
            "worker storage does not give the sandbox ownership of the files "
            f"it creates, so chmod in-sandbox fails ({detail}); needs "
            "host-local XFS/ext4 workspace storage, not overlayfs in Docker "
            "(see docs/HANDOFF.md, open issue)"
        )


@pytest.fixture(autouse=True)
def _sandbox_can_enter_tmp_path(tmp_path):
    """Widen the per-test ``tmp_path`` the same way worker storage is widened.

    Security tests routinely use ``tmp_path`` as a sandbox workspace or as the
    parent of an image cache; pytest creates it 0700, which a sandbox running
    as uid 1000 cannot traverse.
    """
    make_sandbox_visible(tmp_path)


@pytest.fixture()
def require_sandlock():
    if not sandlock_ready():
        pytest.skip(
            "Sandlock security tests require Linux with Landlock ABI >= 6 "
            "(run inside the Docker test runner / Linux CI)"
        )
    # The environment claims sandlock support; prove it can actually create a
    # confined process before any isolation assertion runs. A create failure
    # here must fail loudly instead of letting ``exit_code != 0`` assertions
    # below pass vacuously.
    #
    # N15: the probe goes through the *deployment's* entry point
    # (`own_identity_sandbox`) rather than a bare executor. The pure shape is
    # mediated now, so a bare executor on a root worker is exactly the shape the
    # fork refuses (SL-1: the mediation would run as the host root and the
    # sandbox's own writes would belong to it). Probing a shape no deployment
    # has would turn every isolation assertion below into a setup error.
    executor, workspace = own_identity_sandbox(None, None)
    code, out, err = asyncio.run(
        run_sh(executor, workspace, "/bin/echo ok")
    )
    if code != 0:
        pytest.fail(
            f"sandlock cannot execute commands in this environment "
            f"(smoke test exit_code={code}, stdout={out!r}, stderr={err!r}); "
            "run the test runner with seccomp unconfined / privileged"
        )


# --------------------------------------------------------------------------
# Chroot (image-rootfs) sandboxes on the production path
# --------------------------------------------------------------------------
#
# The chroot shape is the only one where E2B asks the fork for path mediation
# (`fs_denied` + chroot), and mediation now runs in the sandbox's own host uid
# -- inside an own-identity `sandlock-supervise` slot. There is no mediation tier to
# set any more (E2B stopped sending it 2026-09-10; fork B3 deleted the field
# 2026-09-11), so an in-process mediated create on a privileged worker is
# *refused* rather than silently producing supervisor-owned files (T5). Tests
# of this shape therefore have to build the sandbox the way the worker does,
# which is what these helpers do.

_slot_serial = itertools.count()


#: One extracted rootfs per image per *lane*, not per call. The resolver caches
#: inside whatever directory it is handed (a ``.complete`` marker plus a digest
#: sidecar, ``resolve_image_rootfs``), so a stable directory **is** the cache --
#: and a fresh ``mkdtemp`` per call re-extracted the image every single time
#: (measured 2026-10-11: 2.7-2.9 s per call with the local mirror, 13-14 s
#: without one, and 15 call sites in this suite alone -- ~40 s of every lane
#: run spent unpacking the same 60 MB tree).
#:
#: One tree for every caller is also the *product's* own invariant: the shared
#: image rootfs is read-only from inside a sandbox, which is what
#: ``test_real_root_denials`` asserts by looking for a file that must never
#: appear in it. ``E2B_TEST_ROOTFS_DIR`` lets a lane place the cache outside
#: the per-run scratch (a reused lane container would otherwise throw it away
#: with ``E2B_TEST_TMP_ROOT``).
_ROOTFS_CACHE: dict[str, Path] = {}


def resolve_test_rootfs(image: str = "python:3.11-slim") -> Path:
    """The image rootfs, extracted once per image (see ``_ROOTFS_CACHE``).

    Pulled through the registry client (no Docker daemon needed), so the same
    helper serves the Docker-less production-shape lane.
    """
    cached = _ROOTFS_CACHE.get(image)
    if cached is not None and cached.is_dir():
        return cached

    from envd_service.runtime.image_resolver import resolve_image_rootfs

    base = Path(os.environ.get("E2B_TEST_ROOTFS_DIR") or "")
    if not str(base):
        base = sandbox_tmpdir(suffix="-rootfs-cache")
    base.mkdir(parents=True, exist_ok=True)
    rootfs = resolve_image_rootfs(image, str(base))
    _ROOTFS_CACHE[image] = rootfs
    return rootfs


#: The per-node agent's write, performed in-process by the lane. Moved to
#: ``tests/lane_identity.py`` when the multi-node harness needed the same thing
#: (one implementation, three lanes); the old name stays because
#: ``tests/contract/test_nonroot_own_identity.py`` imports it.
_lane_identity_reporter = lane_identity_reporter


def own_identity_sandbox(
    image: str | None,
    rootfs: Path | None,
    *,
    with_own_identity: bool = True,
    host_uid: int | None = SANDBOX_UID if os.geteuid() == 0 else None,
    per_sandbox_uid: bool = True,
    workspace: str | Path | None = None,
    **overrides,
) -> tuple["object", Path]:
    """A sandbox built the way the worker builds one, as (executor, workspace).

    ``image`` + ``rootfs`` are the chroot (mediated) shape; both ``None`` give
    the pure shape. Both are mediated and both get a real root of their own
    (N14 S5): the pure one synthesizes it (N16), the image one extracts it, so
    this helper is also the way a test asks for "the deployment's shape" rather
    than for one particular mediation state.

    ``with_own_identity`` mirrors the production default (``E2B_OWN_IDENTITY=auto``): the
    mediated shape is exactly the one auto engages a slot for. ``False`` stands
    for an operator who set ``E2B_OWN_IDENTITY=off``.

    The default ``host_uid`` follows the worker's privilege, exactly like
    ``envd_service/app.py`` does: a root worker gets a pooled uid (and can map
    it), an unprivileged one gets ``None`` -- the pool is switched off there
    because a single-entry userns can only cover the caller's own euid, so the
    sandbox runs as the worker's identity (E5.1). Passing an explicit uid on a
    non-root worker is a real configuration error (the fork refuses the
    ``RunAs``), which is worth testing but not by default.

    ``workspace`` overrides the scratch directory (for cases that own their
    tree, e.g. the per-uid isolation matrix), and ``**overrides`` are passed to
    ``SandlockExecutor`` for the rest (network policy, secrets, a fixed pooled
    uid): the point of routing every security case through here is that no test
    hand-builds a shape the deployment does not have.
    """
    from envd_service.executors.sandlock import SandlockExecutor
    from envd_service.own_identity import OwnIdentityConfig

    # N14 S5: one shape. The pure skeleton (N16) is the only pure root and the
    # real root (mount ns + pivot_root) is the only root. This helper used to
    # *read* the two switches (`E2B_PURE_ROOTFS`, `E2B_REAL_ROOT`); a lane that
    # still sets a retired value is refused at startup by
    # `refuse_retired_root_levers`, and a helper that kept reading them would
    # quietly build the retired shape while the fleet runs one -- so it
    # synthesizes the root unconditionally instead.
    pure_rootfs_dir = os.environ.get("E2B_PURE_ROOTFS_DIR") or str(
        sandbox_tmpdir(suffix="-pure-rootfs")
    )
    fields: dict = dict(
        workspace_dir=str(workspace if workspace is not None else sandbox_tmpdir(suffix="-ws")),
        base_image=image,
        image_rootfs=rootfs,
        host_uid=host_uid,
        per_sandbox_uid=per_sandbox_uid,
        # The deployed worker's own value (`E2B_PID_NS=true` in every shipped
        # stack). The fixture has to match it, because with the *shipped*
        # seccomp profile (`deploy/seccomp/sandlock-worker.json` -- no
        # `unshare`, deliberately, N80) a sandbox that is not already inside a
        # user namespace cannot create one: `context.rs` calls
        # `unshare(CLONE_NEWUSER)` for the real root (which needs a mount ns)
        # and the filter answers EPERM, so the child dies before its notif fd
        # and every create in this file fails with `read notif fd from child:
        # pipe closed before 4 bytes read` (measured 2026-10-11, with the
        # profile's `SANLOCK_REALROOT_TRACE` naming the exact line:
        # `FAILURE: sandlock child: unshare(CLONE_NEWUSER): Operation not
        # permitted`). With `pid_ns` on, the one `clone3` call carries the
        # user/PID/mount/net namespaces exactly as it does on the fleet.
        pid_ns=True,
        pure_rootfs_dir=Path(pure_rootfs_dir),
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=f"sbx_slot_{next(_slot_serial)}",
        own_identity=OwnIdentityConfig(
            mode="auto" if with_own_identity else "off",
            uid_start=host_uid if host_uid is not None else SANDBOX_UID,
            uid_size=2,
            tmp_root=sandbox_tmpdir(suffix="-route-b"),
            # ``E2B_SLOT_READY_TIMEOUT_S`` applies to hand-built executors too:
            # the *first* chroot launch inside a container opens with a closed
            # control channel and only answers ~30.5 s in (measured 2026-10-11),
            # so the 30 s default is a coin flip that decides whether a whole
            # file runs -- 11 security files lost their run to it. A lane that
            # pays that cost once, up front, raises this for the warm-up; an
            # ordinary run leaves it unset and keeps the deployment's bound.
            socket_timeout_s=float(
                os.environ.get("E2B_SLOT_READY_TIMEOUT_S") or 30.0
            ),
            # C3 is the only slot-identity shape left (N52): every slot is
            # granted by writing the child's map, and in this lane the root
            # process performing the write stands in for the node's agent.
            identity_grant="agent-grant",
            identity_reporter=_lane_identity_reporter(
                host_uid if host_uid is not None else SANDBOX_UID, 2
            ),
        ),
    )
    fields.update(overrides)
    executor = SandlockExecutor(**fields)
    return executor, Path(fields["workspace_dir"])


async def run_sh(executor, cwd: str | Path, sh: str) -> tuple[int, bytes, bytes]:
    """``start()`` + drain: how the process manager runs one command."""
    from envd_service.executors.base import ExecConfig

    running = await executor.start(
        ExecConfig(
            cmd=["/bin/sh", "-c", sh],
            env={},
            cwd=str(cwd),
            stdin_enabled=False,
        )
    )
    out: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    async for kind, chunk in running.output():
        if kind in out:
            out[kind].append(chunk)
    code = await running.exit_code()
    return code, b"".join(out["stdout"]), b"".join(out["stderr"])


def require_mediation_capable(executor) -> None:
    """Skip only when *neither* backend can create this sandbox.

    A slot needs a starter able to drop to the sandbox uid (root/CAP_SETUID) and
    the wheel's ``sandlock-supervise``. Without a slot the mediated create goes
    in-process, and the fork refuses that only when the mediator could remap the
    sandbox to a *different* non-zero uid
    (`SandlockExecutor._in_process_mediation_is_refused`). The unprivileged
    worker both production manifests ship mediates as the sandbox's own identity
    and is not refused -- so its chroot tests must keep running rather than
    skip: that shape *is* the deployment. Asserting anyway where a create cannot
    happen would let an ``exit_code != 0`` check pass vacuously.
    """
    if not executor._own_identity_active and executor._in_process_mediation_is_refused():
        pytest.skip(
            "this worker can neither lease an own-identity slot nor be accepted by the "
            f"fork's in-process mediation ({executor._own_identity_decline}); needs a "
            "root worker plus the wheel's sandlock-supervise binary"
        )
