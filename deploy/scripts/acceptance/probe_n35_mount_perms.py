"""Can the sandbox's own identity set up a real root?

The N14 route has the sandbox process itself mount its root, and it does so as
uid 0 *inside its own user namespace* -- which is the slot's host uid on the
host side. Two things have to hold:

  1. the rootfs cache path is traversable by that host uid (the tree is created
     by the worker / test harness, not by the sandbox);
  2. a userns-root process may bind-mount into it (userns mount rules: the
     source must be reachable from the namespace, and locked mounts cannot be
     bound).

Both are measured here by *becoming* that identity: setresuid to the slot uid,
unshare a self-mapped user namespace, unshare a mount namespace, then bind.

Usage (inside the lane container, as root):

    python3 -u deploy/scripts/acceptance/probe_n35_mount_perms.py
"""

import ctypes
import os
import sys
from pathlib import Path

libc = ctypes.CDLL("libc.so.6", use_errno=True)

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
MS_BIND = 4096

SLOT_UID = 1000


def errno_of(fn):
    ctypes.set_errno(0)
    rc = fn()
    return rc, ctypes.get_errno()


def report(label, rc, err):
    print(f"  {label}: rc={rc} {os.strerror(err) if err else 'ok'}")
    sys.stdout.flush()


def chain(path: Path):
    print(f"  chain for {path}:")
    parts = []
    current = Path("/")
    for part in path.parts[1:]:
        current = current / part
        try:
            st = current.stat()
            parts.append(f"{current} mode={oct(st.st_mode & 0o7777)} uid={st.st_uid}")
        except OSError as exc:
            parts.append(f"{current} <{exc.strerror}>")
    for part in parts:
        print(f"    {part}")


def become_slot_and_mount(target_dir: Path, source: Path, label: str) -> None:
    """Run in a forked child: uid 1000 -> userns self-map -> mount ns -> bind."""
    pid = os.fork()
    if pid != 0:
        os.waitpid(pid, 0)
        return
    try:
        print(f"  [{label}] as uid={os.getuid()}")
        rc, err = errno_of(lambda: libc.setresuid(SLOT_UID, SLOT_UID, SLOT_UID))
        report("setresuid(1000)", rc, err)
        # setresuid clears dumpability, which makes /proc/self/* root-owned and
        # unwritable -- the userns map cannot be written until that is undone.
        # (A real slot process starts as uid 1000 instead; this is the probe
        # standing in for it.)
        libc.prctl(4, 1, 0, 0, 0)  # PR_SET_DUMPABLE
        rc, err = errno_of(lambda: libc.unshare(CLONE_NEWUSER))
        report("unshare(CLONE_NEWUSER)", rc, err)
        for path, text in (
            ("/proc/self/setgroups", "deny"),
            ("/proc/self/uid_map", f"0 {SLOT_UID} 1\n"),
            ("/proc/self/gid_map", f"0 {SLOT_UID} 1\n"),
        ):
            try:
                Path(path).write_text(text)
            except OSError as exc:
                print(f"    write {path}: {exc.strerror}")
        print(f"    after userns: uid={os.getuid()} euid={os.geteuid()}")
        rc, err = errno_of(
            lambda: libc.mount(
                str(source).encode(), str(target_dir).encode(), None, MS_BIND, None
            )
        )
        report(f"bind {source} -> {target_dir}", rc, err)
        if rc == 0:
            try:
                listing = sorted(p.name for p in target_dir.iterdir())[:5]
                print(f"    mounted, inside: {listing}")
            except OSError as exc:
                print(f"    mounted, listing failed: {exc}")
        rc, err = errno_of(lambda: libc.unshare(CLONE_NEWNS))
        report("unshare(CLONE_NEWNS)", rc, err)
    except Exception as exc:  # a fixture problem must not look like a permission result
        print(f"    raised {type(exc).__name__}: {exc}")
    finally:
        os._exit(0)


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    from tests.security.conftest import resolve_test_rootfs

    # Create the cache the way the worker does (this process is root in the
    # lane, like the harness; in production it is the worker's uid), so the
    # permissions below are the real ones.
    rootfs = Path(resolve_test_rootfs("python:3.11-slim"))
    print(f"uid={os.getuid()} rootfs={rootfs}")
    print(f"rootfs={rootfs}")
    chain(rootfs)
    work = Path("/tmp/n35-mount-perms")
    work.mkdir(exist_ok=True)
    (work / "src").mkdir(exist_ok=True)
    (work / "src" / "marker").write_text("SRC\n")

    print("== as root (the privileged baseline)")
    become_slot_and_mount(work / "src", work / "src", "root-src-bind")
    print("== into the rootfs (what the real root needs)")
    become_slot_and_mount(rootfs / "workspace", work / "src", "rootfs-workspace")
    print("== into a fresh dir under the rootfs (mount point that exists)")
    fresh = rootfs / "n35-mnt"
    fresh.mkdir(exist_ok=True)
    become_slot_and_mount(fresh, work / "src", "rootfs-fresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
