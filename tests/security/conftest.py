"""Sandlock availability helpers for security tests."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


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
    """
    ws = str(sandbox_tmpdir())
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    executor = SandlockExecutor(
        workspace_dir=ws,
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )
    cmd = ["/bin/sh", "-c", "printf x > tool && chmod 700 tool && echo CHOWNED"]
    result = executor._build_sandbox(
        ExecConfig(cmd=cmd, env={}, cwd=ws, stdin_enabled=False)
    ).run(cmd)
    detail = (result.stderr or b"").decode("utf-8", "replace").strip()
    return result.exit_code == 0, f"exit_code={result.exit_code} stderr={detail!r}"


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
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )
    smoke = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/echo", "ok"],
            env={},
            cwd=ws,
            stdin_enabled=False,
        )
    ).run(["/bin/echo", "ok"])
    if smoke.exit_code != 0:
        pytest.fail(
            f"sandlock cannot execute commands in this environment "
            f"(smoke test exit_code={smoke.exit_code}, error={smoke.error!r}); "
            "run the test runner with seccomp unconfined / privileged"
        )
