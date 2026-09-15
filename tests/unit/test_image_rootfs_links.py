"""The extracted rootfs' ``..``-relative symlinks are rewritten (resolver).

The mediated chroot resolves every path with ``openat2(RESOLVE_IN_ROOT)``,
which the kernel may refuse with the documented *retryable* ``EAGAIN``
("could not ensure that a ".." component didn't escape") -- and the fork's
executor turns any such failure into an immediate ``Errno(ENOENT)``, i.e. the
workload never runs and the command reports exit 127 with empty stderr. A
Debian/Ubuntu image reaches its dynamic linker through exactly such a link
(``/lib64/ld-linux-x86-64.so.2 -> ../lib/x86_64-linux-gnu/ld-linux-x86-64.so.2``),
so the resolver hands out rootfs trees whose ``..`` components are already
collapsed into the equivalent rooted absolute target.

The rewrite must be *semantics-preserving*: inside the chroot, ``..`` is
clamped at the root, so the rewritten link has to resolve to the very same
inode. These cases assert that inode-for-inode.
"""

from __future__ import annotations

import os
from pathlib import Path

from envd_service.runtime import image_resolver as ir


def _inode(path: Path) -> tuple[int, int]:
    stat = os.stat(path)
    return (stat.st_dev, stat.st_ino)


def _resolved_under(rootfs: Path, link: Path) -> tuple[int, int]:
    """The inode the *sandbox* would reach: absolute targets re-root at ``rootfs``.

    Outside a chroot the host resolves an absolute link target against the real
    ``/``, which is exactly what ``RESOLVE_IN_ROOT`` does not do -- so the
    comparison has to re-root it the way the kernel would.
    """
    target = os.readlink(link)
    if os.path.isabs(target):
        path = rootfs / target.lstrip("/")
    else:
        path = link
    return _inode(path)


def test_dotdot_link_is_rewritten_and_resolves_to_the_same_inode(
    tmp_path: Path,
) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "lib" / "x86_64-linux-gnu").mkdir(parents=True)
    (rootfs / "lib" / "x86_64-linux-gnu" / "ld-linux-x86-64.so.2").write_bytes(
        b"linker"
    )
    (rootfs / "lib64").mkdir()
    link = rootfs / "lib64" / "ld-linux-x86-64.so.2"
    link.symlink_to("../lib/x86_64-linux-gnu/ld-linux-x86-64.so.2")
    before = _resolved_under(rootfs, link)

    assert ir._prepared_rootfs("python-mcp:3.14", rootfs) == rootfs

    assert os.readlink(link) == "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
    assert _resolved_under(rootfs, link) == before
    assert (rootfs / "lib/x86_64-linux-gnu/ld-linux-x86-64.so.2").read_bytes() == (
        b"linker"
    )


def test_a_relative_link_without_dotdot_is_left_alone(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / "bin" / "bash").write_bytes(b"bash")
    link = rootfs / "bin" / "sh"
    link.symlink_to("bash")

    ir._prepared_rootfs("base", rootfs)

    assert os.readlink(link) == "bash"


def test_an_absolute_link_is_left_alone(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "bin" / "python3.14").write_bytes(b"python")
    link = rootfs / "usr" / "bin" / "python3"
    link.symlink_to("/usr/bin/python3.14")

    ir._prepared_rootfs("base", rootfs)

    assert os.readlink(link) == "/usr/bin/python3.14"


def test_a_dotdot_that_would_escape_the_root_clamps_like_resolve_in_root(
    tmp_path: Path,
) -> None:
    """``/tool`` -> ``../../etc/passwd`` must land on ``/etc/passwd``, not above it."""
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    target = rootfs / "etc" / "passwd"
    target.write_bytes(b"root:x:0:0\n")
    tool = rootfs / "tool"
    tool.symlink_to("../../etc/passwd")

    ir._prepared_rootfs("base", rootfs)

    assert os.readlink(tool) == "/etc/passwd"
    assert _resolved_under(rootfs, tool) == _inode(target)
    assert (rootfs / "etc" / "passwd").read_bytes() == b"root:x:0:0\n"


def test_the_rewrite_is_idempotent(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "a" / "b").mkdir(parents=True)
    (rootfs / "a" / "b" / "file").write_bytes(b"f")
    link = rootfs / "a" / "link"
    link.symlink_to("b/../b/file")
    ir._prepared_rootfs("base", rootfs)
    first = os.readlink(link)

    ir._PREPARED_ROOTFS.discard(str(rootfs))
    ir._prepared_rootfs("base", rootfs)

    assert first == "/a/b/file"
    assert os.readlink(link) == first
    assert _resolved_under(rootfs, link) == _inode(rootfs / "a" / "b" / "file")


def test_confine_at_root_matches_resolve_in_root_semantics() -> None:
    assert ir._confine_at_root("/a/b/../c") == "/a/c"
    assert ir._confine_at_root("/../x") == "/x"
    assert ir._confine_at_root("/a/../../b") == "/b"
    assert ir._confine_at_root("//a/./b//") == "/a/b"
