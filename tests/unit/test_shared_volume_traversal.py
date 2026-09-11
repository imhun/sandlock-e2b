"""A5: the host volume path must be traversable for tenant uids (o+x).

The mediator opens the volume host path *as the sandbox's own uid* (route-B
supervise slot), so DAC needs execute on **every** level between ``/`` and the
volume view -- the volume root alone is not enough. Where it is missing, even
an absolute volume path fails with EACCES; only the deleted ``mount --bind``
workaround used to hide it by copying the volume into the workspace.

Every assertion is on **mode bits**, never ``os.access``: the gate lane runs
the test process as root, and root passes ``os.access`` on a 0700 directory it
does not own, which would make the check vacuous.

This file is **euid-independent on purpose**. The chmod helpers
(``_ensure_traversable``, ``_ensure_shared_volume_root``) do the work and are
called directly, so the same cases run green as root and as an unprivileged
uid (macOS full suite, ``test-prod-shaped.sh`` phase 2). The root-only
``provision_sandbox_volume_mount`` branch is pinned by stubbing its euid gate
and asserting the call site instead of carrying a root-only (skipped) case;
the ownership/slice assertions that genuinely need root already live in
``tests/unit/test_volume_quota.py`` under the repo's existing skipif.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

import envd_service.app as app_module
import envd_service.volumes as volumes
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.volumes import (
    _ensure_shared_volume_root,
    _ensure_traversable,
    provision_sandbox_volume_mount,
)

#: A uid from the pool, distinct from the test process and from the
#: directories' owner, so only the "other" bits can admit it.
HOST_UID = 21700

#: group-x + other-x: the tenant uid owns nothing and has no supplementary
#: groups (single-entry userns), so it needs the other bit; the group bit is
#: kept alongside because that is what ``0711`` supplies.
TRAVERSAL_BITS = 0o011

#: ``EnvdSettings``' pool start: the "first uid in the pool" the startup probe
#: checks with.
POOL_START = EnvdSettings().uid_pool_start


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_traversable(path: Path) -> None:
    mode = _mode(path)
    assert mode & TRAVERSAL_BITS == TRAVERSAL_BITS, (
        f"{path} is mode {mode:04o}: tenant uid {HOST_UID} cannot traverse it"
    )


def _arrange(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``<tmp>/root/_volumes/vol_a`` with both leading levels left at 0700.

    ``vol_a`` stands in for the volume *view* the mediator opens; ``_volumes``
    and ``root`` are the middle directories between ``/`` and the volume whose
    o+x is the whole subject of this task.
    """
    root = tmp_path / "root"
    volumes_dir = root / "_volumes"
    volume = volumes_dir / "vol_a"
    volume.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(volumes_dir, 0o700)
    return root, volumes_dir, volume


def _provision(volume: Path, *, quota_mb: int = 0) -> tuple[Path, int | None]:
    return provision_sandbox_volume_mount(
        sandbox_id="sbx_a",
        volume_id="vol_a",
        mount_path="mnt/data",
        volume_path=volume,
        per_sandbox_quota_mb=quota_mb,
        fallback_mount_point="/srv",
        via_agent=False,
        host_uid=HOST_UID,
    )


# ------------------------------------------------------- the traversal gate


def test_ensure_traversable_only_adds_the_missing_execute_bits(tmp_path):
    """``0711`` where traversal was missing, wider modes left alone."""
    cases = ((0o700, 0o711), (0o750, 0o751), (0o755, 0o755), (0o1777, 0o1777))
    for before, after in cases:
        level = tmp_path / f"mode-{before:04o}"
        level.mkdir()
        os.chmod(level, before)

        _ensure_traversable(level)

        assert _mode(level) == after
        _assert_traversable(level)


def test_ensure_traversable_reaches_every_ancestor_of_the_volume_view(tmp_path):
    """The volume view alone is not enough: its 0700 parents come with it."""
    root, volumes_dir, volume = _arrange(tmp_path)
    os.chmod(volume, 0o755)  # the view itself is already reachable

    _ensure_traversable(volume)

    for level in (volume, volumes_dir, root):
        _assert_traversable(level)
    assert _mode(volume) == 0o755  # unchanged, not tightened or widened
    assert _mode(volumes_dir) == 0o711
    assert _mode(root) == 0o711


def test_ensure_shared_volume_root_applies_1777_and_covers_the_chain(tmp_path):
    """The E3.2 root model, plus the A5 ancestor chain, in one call."""
    volume = tmp_path / "root" / "_volumes" / "vol_a"
    volume.mkdir(parents=True)
    os.chmod(tmp_path / "root", 0o700)
    os.chmod(tmp_path / "root" / "_volumes", 0o755)

    _ensure_shared_volume_root(volume, HOST_UID)

    assert _mode(volume) == 0o1777
    for level in (volume, volume.parent, volume.parent.parent):
        _assert_traversable(level)
    # A level that was already wider keeps its mode (only x is ever added).
    assert _mode(volume.parent) == 0o755
    assert _mode(volume.parent.parent) == 0o711


def test_traversal_fixup_never_widens_the_sandbox_slice(tmp_path):
    """It walks *upward* only: the per-sandbox slice stays 0700."""
    volume = tmp_path / "vol_a"
    slice_dir = volume / "sbx_a"
    slice_dir.mkdir(parents=True)
    os.chmod(volume, 0o700)
    os.chmod(slice_dir, 0o700)

    _ensure_traversable(volume)

    assert _mode(volume) == 0o711
    assert _mode(slice_dir) == 0o700


def test_ensure_traversable_survives_an_unresolvable_path(
    monkeypatch, tmp_path, caplog
):
    """``resolve()`` can raise (symlink loop): warn, fall back, keep going.

    Provisioning is the caller, and it must not fail over a permission
    *widening* step -- the literal parent chain still gets the x bits.
    """
    root, volumes_dir, volume = _arrange(tmp_path)
    os.chmod(volume, 0o755)
    loop = OSError(40, "Too many levels of symbolic links")

    def unresolvable(self, strict=False):
        raise loop

    monkeypatch.setattr(Path, "resolve", unresolvable)
    caplog.set_level(logging.WARNING)

    _ensure_traversable(volume)

    for level in (volume, volumes_dir, root):
        _assert_traversable(level)
    assert _mode(volumes_dir) == 0o711
    from_volumes = [r.message for r in caplog.records if r.name == "envd_service.volumes"]
    # The chain walk may also warn about levels this uid is not allowed to
    # chmod (macOS refuses parts of the per-user temp tree); the resolve
    # failure is the one under test, asserted exactly.
    assert [m for m in from_volumes if not m.startswith("cannot make ")] == [
        f"cannot resolve {volume} to widen its ancestors for tenant uids: {loop}"
    ]
    # The same call the mount provisioning makes must return, not raise.
    _ensure_shared_volume_root(volume, HOST_UID)
    assert _mode(volume) == 0o1777


def test_provision_applies_the_volume_root_model_for_a_host_uid(
    monkeypatch, tmp_path
):
    """The ``host_uid`` branch of the mount provisioning.

    That branch is gated on ``os.geteuid() == 0``, which is the only reason a
    root worker would be needed; the gate is stubbed here so the *call* is
    pinned in every lane. It is a call-site assertion, not a permission
    assertion -- the permissions themselves are covered by the cases above,
    which need no privilege at all.
    """
    volume = tmp_path / "vol_a"
    volume.mkdir()
    seen: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        volumes,
        "_ensure_shared_volume_root",
        lambda root, host_uid: seen.append((root, host_uid)),
    )
    monkeypatch.setattr(volumes.os, "geteuid", lambda: 0)

    view, projid = _provision(volume)

    assert seen == [(volume, HOST_UID)]
    assert view == volume
    assert projid is None


# ------------------------------------------------ worker startup self-check


class _PoolStub:
    """Just what the probe reads: the first uid in the pool."""

    def __init__(self, start: int = POOL_START) -> None:
        self.start = start


class _RegistryStub:
    def __init__(self, pool: _PoolStub | None) -> None:
        self.uid_pool = pool


def _probe_settings(volume: Path) -> EnvdSettings:
    return EnvdSettings(
        executor="local",
        per_sandbox_uid=True,
        shared_volume_root=str(volume),
    )


@pytest.fixture()
def chain_under_tmp():
    """``/tmp/<scratch>/root/_volumes/vol_a``, yielding scratch/root/chain/view.

    Rooted at ``/tmp`` rather than ``tmp_path`` on purpose: the probe walks
    *every* level up to ``/``, and pytest's own scratch roots are 0700 (on
    macOS the per-user temp root refuses the chmod outright), which would make
    the expected offender list depend on the runner. ``/tmp`` is reachable
    everywhere the suite runs.
    """
    # resolve() so the paths below are the ones the probe walks on platforms
    # where /tmp is a symlink (macOS: /private/tmp) -- the expected offender
    # list in the warning must be built from the same spelling.
    base = Path(tempfile.mkdtemp(prefix="a5-traversal-", dir="/tmp")).resolve()
    try:
        os.chmod(base, 0o755)
        root = base / "root"
        volumes_dir = root / "_volumes"
        volume = volumes_dir / "vol_a"
        volume.mkdir(parents=True)
        os.chmod(root, 0o700)
        os.chmod(volumes_dir, 0o700)
        yield base, root, volumes_dir, volume
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_gap_probe_flags_a_0700_level_and_not_a_reachable_one(tmp_path):
    """The probe's own rule: unexecutable levels, and only those."""
    root, volumes_dir, volume = _arrange(tmp_path)
    os.chmod(volume, 0o755)

    gaps = dict(app_module._shared_volume_traversal_gaps(volume, HOST_UID))

    assert gaps[root] == 0o700
    assert gaps[volumes_dir] == 0o700
    assert volume not in gaps
    # Whatever else the runner's scratch chain contributes, every flagged
    # level really is unreachable for that uid.
    assert all(mode & TRAVERSAL_BITS != TRAVERSAL_BITS for mode in gaps.values())
    assert Path("/") not in gaps


def test_gap_probe_honours_the_owner_execute_bit(monkeypatch, tmp_path):
    """A view owned by the tenant is entered through its *owner* bit.

    The pool uid owns its own volume slices, so ``0700`` there is reachable
    even though other-x is clear. ``stat`` is faked because the alternative --
    a real `chown` -- would make the case root-only.
    """

    class _Stat:
        def __init__(self, st_uid: int, st_mode: int) -> None:
            self.st_uid = st_uid
            self.st_mode = st_mode

    level = tmp_path / "owned-by-tenant"
    level.mkdir()
    real_stat = Path.stat
    owner_mode = {"value": 0o700}

    def fake_stat(self, *args, **kwargs):
        if self == level:
            return _Stat(HOST_UID, owner_mode["value"])
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)

    # owner-x set, other-x clear: reachable for the uid that owns it.
    assert level not in dict(
        app_module._shared_volume_traversal_gaps(level, HOST_UID)
    )

    # owner-x clear: not reachable, even though it is the owner.
    owner_mode["value"] = 0o600
    assert dict(app_module._shared_volume_traversal_gaps(level, HOST_UID))[level] == (
        0o600
    )


def test_startup_probe_is_quiet_when_the_volume_root_is_reachable(
    chain_under_tmp, caplog
):
    """A traversable chain must not produce the A5 warning."""
    base, _root, _volumes_dir, volume = chain_under_tmp
    _ensure_traversable(volume)
    assert app_module._shared_volume_traversal_gaps(volume, POOL_START) == []
    caplog.set_level(logging.WARNING)

    app_module._disclose_shared_volume_traversal(
        _probe_settings(volume), _RegistryStub(_PoolStub())
    )

    assert base.is_dir()
    assert [r.message for r in caplog.records if r.name == "envd_service.app"] == []


def test_startup_probe_warns_and_names_the_unreachable_directory(
    chain_under_tmp, caplog
):
    """One unreachable level is named with its actual mode and the fix."""
    _base, _root, volumes_dir, volume = chain_under_tmp
    _ensure_traversable(volume)
    assert app_module._shared_volume_traversal_gaps(volume, POOL_START) == []
    os.chmod(volumes_dir, 0o700)  # exactly one offending level
    caplog.set_level(logging.WARNING)

    app_module._disclose_shared_volume_traversal(
        _probe_settings(volume), _RegistryStub(_PoolStub())
    )

    assert [r.message for r in caplog.records if r.name == "envd_service.app"] == [
        (
            f"shared volume root {volume} is not traversable for tenant uids "
            f"(first pool uid {POOL_START}): {volumes_dir} is mode 0700. "
            "The mediator opens volume host paths as the sandbox's own uid, so "
            "every level from / down to the volume view needs o+x; without it "
            "volume mounts fail with EACCES even on an absolute path. Fix: "
            "chmod 0711 (or 0755 where listing is acceptable) on each of those "
            f"directories -- {volume} and its ancestors."
        )
    ]


async def test_worker_lifespan_runs_the_startup_probe(monkeypatch, tmp_path):
    """The probe is wired into the worker's own startup, once."""
    seen: list[tuple[EnvdSettings, RuntimeRegistry]] = []
    monkeypatch.setattr(
        app_module,
        "_disclose_shared_volume_traversal",
        lambda settings, registry: seen.append((settings, registry)),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry = RuntimeRegistry(workspace)
    settings = EnvdSettings(executor="local", workspace_base=workspace)
    app = create_envd_app(settings=settings, runtime_registry=registry)

    async with app.router.lifespan_context(app):
        pass

    assert seen == [(settings, registry)]


# ------------------------------------------------- the RED reverse variant


def test_ancestor_put_back_to_0700_fails_the_same_assertion(tmp_path):
    """Reverse variant: with an ancestor back at 0700 the identical mode-bit
    check is RED. ``os.access`` would still have said "yes" here -- the test
    process is root -- so this is what proves the assertion has teeth."""
    _root, volumes_dir, volume = _arrange(tmp_path)
    _ensure_shared_volume_root(volume, HOST_UID)
    _assert_traversable(volumes_dir)  # green in the fixed world

    os.chmod(volumes_dir, 0o700)  # revert the ancestor

    with pytest.raises(AssertionError) as excinfo:
        _assert_traversable(volumes_dir)
    # pytest appends its rewritten-expression explanation after the first
    # line; the message itself is asserted exactly.
    assert excinfo.value.args[0].splitlines()[0] == (
        f"{volumes_dir} is mode 0700: tenant uid {HOST_UID} cannot traverse it"
    )


def test_negative_control_without_the_traversal_step(monkeypatch, tmp_path):
    """Mutation control: neutralize ``_ensure_traversable`` (the pre-A5 world)
    and the same assertion fails on the very same arrangement."""
    monkeypatch.setattr(volumes, "_ensure_traversable", lambda _path: None)
    _root, volumes_dir, volume = _arrange(tmp_path)

    _ensure_shared_volume_root(volume, HOST_UID)

    # The E3.2 root model is still applied; only the chain fixup is missing.
    assert _mode(volume) == 0o1777
    assert _mode(volumes_dir) == 0o700
    with pytest.raises(AssertionError):
        _assert_traversable(volumes_dir)
