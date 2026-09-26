"""The pure shape's synthesized root: predicates, skeleton, mount map.

Off-Linux by construction: the native sandlock module is absent, so
``_build_instance_policy`` returns the plain namespace the fork would receive,
and materializing the skeleton only creates directories.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import (
    SandlockExecutor,
    _SYNTHETIC_ROOTFS_SKELETON_DIRS,
    _SYNTHETIC_ROOTFS_SYSTEM_DIRS,
    _synthetic_rootfs_mounts,
)


def _executor(tmp_path: Path, **overrides) -> SandlockExecutor:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        workspace_dir=str(ws),
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id="sbx_synth",
        pure_rootfs_dir=str(tmp_path / "_pure_rootfs"),
    )
    kwargs.update(overrides)
    return SandlockExecutor(**kwargs)


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


@contextlib.contextmanager
def _umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def test_the_synthetic_root_is_the_sandboxs_own_directory(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    assert ex._synthetic_rootfs == tmp_path / "_pure_rootfs" / "sbx_synth"
    assert ex._has_sandbox_root is True
    assert ex._chroot_root == str(tmp_path / "_pure_rootfs" / "sbx_synth")


def test_without_the_switch_the_pure_shape_keeps_the_identity_root(tmp_path: Path) -> None:
    ex = _executor(tmp_path, pure_rootfs_dir=None)
    assert ex._synthetic_rootfs is None
    assert ex._has_sandbox_root is False
    assert ex._chroot_root == "/"


def test_an_image_sandbox_ignores_the_pure_rootfs_switch(tmp_path: Path) -> None:
    rootfs = tmp_path / "image"
    rootfs.mkdir()
    ex = _executor(tmp_path, base_image="python:3.11-slim", image_rootfs=rootfs)
    assert ex._synthetic_rootfs is None
    assert ex._chroot_root == str(rootfs)


def test_the_synthetic_root_does_not_change_the_allow_list(tmp_path: Path) -> None:
    """The route's whole claim: same visible set, different resolution.

    Both executors are built on the *same* workspace: the claim is about the
    shape, and `fs_writable[0]` is the workspace directory itself.
    """
    off = _executor(tmp_path, pure_rootfs_dir=None)._build_instance_policy()
    on = _executor(tmp_path)._build_instance_policy()
    assert list(on.fs_readable) == list(off.fs_readable)
    assert list(on.fs_writable) == list(off.fs_writable)
    assert list(on.fs_denied) == list(off.fs_denied)
    assert on.chroot == str(tmp_path / "_pure_rootfs" / "sbx_synth")
    assert off.chroot == "/"


def test_the_synthetic_root_mount_map_is_workspace_volumes_system_dirs_dev(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    vol = tmp_path / "vol"
    vol.mkdir()
    ex = _executor(
        tmp_path,
        fs_mounts={"/workspace/mnt/data": str(vol), "/home/user/mnt/data": str(vol)},
    )
    policy = ex._build_instance_policy()
    expected = {
        "/home/user": str(ws),
        "/workspace": str(ws),
        "/workspace/mnt/data": str(vol),
        "/home/user/mnt/data": str(vol),
    }
    expected.update(_synthetic_rootfs_mounts())
    assert dict(policy.fs_mount) == expected
    # Declaration order is load-bearing: the fork breaks host-source ties by it.
    assert list(dict(policy.fs_mount))[:2] == ["/home/user", "/workspace"]


def test_the_skeleton_is_traversable_and_0755_under_umask_077(tmp_path: Path) -> None:
    """Every directory is 0755 by an explicit chmod, not by the ambient umask.

    ``mkdir(mode=...)`` is masked by the umask -- under ``umask 077`` a request
    for ``0o755`` yields ``0o700`` -- and these directories are not owned by the
    sandbox's own uid, so a ``0700`` one fails the bind with ``EACCES`` before
    the sandbox ever starts (``route_b.py`` makes the same call for the slot
    documents). The build therefore runs under ``umask 077`` on purpose: a
    ``0o755`` assertion under the default umask proves nothing.
    """
    with _umask(0o077):
        ex = _executor(tmp_path)
        policy = ex._build_instance_policy()
    root = ex._synthetic_rootfs
    assert root is not None
    assert (root / "proc").is_dir()
    assert (root / "home" / "user").is_dir()
    for virtual in policy.fs_mount:
        assert (root / str(virtual).lstrip("/")).exists(), virtual
    # Task 5's pure-rootfs switch hands out ``<pure_rootfs_dir>/<sandbox_id>``,
    # so the parent layer the sandbox uid traverses into has to be 0755 too.
    assert _mode(root.parent) == "0o755"
    assert _mode(root) == "0o755"
    for name in _SYNTHETIC_ROOTFS_SKELETON_DIRS:
        assert _mode(root / name) == "0o755", name
    assert _mode(root / "home" / "user") == "0o755"
    for virtual in policy.fs_mount:
        assert _mode(root / str(virtual).lstrip("/")) == "0o755", virtual


def test_system_dirs_are_filtered_by_host_existence(tmp_path: Path) -> None:
    """A missing host directory must not become an empty stub in the sandbox."""
    expected = {d: d for d in _SYNTHETIC_ROOTFS_SYSTEM_DIRS if os.path.isdir(d)}
    expected["/dev"] = "/dev"
    assert _synthetic_rootfs_mounts() == expected


def test_the_system_dir_mounts_are_never_writable(tmp_path: Path) -> None:
    """The other half of the mount table: a bind must not grant host writes.

    Every system directory has to be in ``fs_mount`` (the fork derives a mount
    source's rights from what the policy declares for its mount point) and has
    to stay out of ``fs_writable`` -- for a host directory bind that spelling is
    write access to the worker's own ``/usr``.
    """
    policy = _executor(tmp_path)._build_instance_policy()
    for virtual in _synthetic_rootfs_mounts():
        assert virtual in policy.fs_mount
        assert virtual not in policy.fs_writable
    assert list(policy.fs_writable) == [
        str(tmp_path / "ws"),
        "/workspace",
        "/home/user",
    ]


def test_the_synthesized_root_answers_with_the_virtual_cwd(tmp_path: Path) -> None:
    """The fork chdirs to ``root.join(cwd)``, so the cwd has to be virtual.

    The host spelling is what the *unrooted* pure shape answers -- it is what
    the fork can chdir to when the root is "/". With a root of its own the same
    answer would resolve under the synthesized tree, where the host workspace
    path does not exist.
    """
    def cfg(cwd: str) -> ExecConfig:
        return ExecConfig(cmd=["/bin/sh"], env={}, cwd=cwd, stdin_enabled=False)

    rooted = _executor(tmp_path)
    assert rooted._view_cwd(cfg(str(tmp_path / "ws"))) == "/home/user"
    assert rooted._view_cwd(cfg("")) == "/home/user"
    assert rooted._view_cwd(cfg("/workspace")) == "/workspace"
    assert rooted._view_cwd(cfg("/tmp")) == "/tmp"
    unrooted = _executor(tmp_path, pure_rootfs_dir=None)
    assert unrooted._view_cwd(cfg(str(tmp_path / "ws"))) == str(tmp_path / "ws")


def test_the_one_shot_builder_takes_the_same_two_shapes(tmp_path: Path) -> None:
    """``_build_sandbox`` is the second builder; both shapes must agree."""
    config = ExecConfig(
        cmd=["/bin/sh"],
        env={},
        cwd=str(tmp_path / "ws"),
        stdin_enabled=False,
    )
    on = _executor(tmp_path)._build_sandbox(config)
    assert on.chroot == str(tmp_path / "_pure_rootfs" / "sbx_synth")
    assert dict(on.fs_mount) == {
        "/home/user": str(tmp_path / "ws"),
        "/workspace": str(tmp_path / "ws"),
        **_synthetic_rootfs_mounts(),
    }
    assert list(on.fs_writable) == [
        str(tmp_path / "ws"),
        "/workspace",
        "/home/user",
    ]
    assert list(on.fs_readable) == ["/usr", "/lib", "/bin", "/opt"]
    assert list(on.fs_denied) == []
    assert on.cwd == "/home/user"
    off = _executor(tmp_path, pure_rootfs_dir=None)._build_sandbox(config)
    assert off.chroot == "/"
    assert dict(off.fs_mount) == {
        "/home/user": str(tmp_path / "ws"),
        "/workspace": str(tmp_path / "ws"),
    }
    assert list(off.fs_readable) == list(on.fs_readable)
    assert list(off.fs_denied) == list(on.fs_denied)
    assert off.cwd == str(tmp_path / "ws")
