"""Which mount operations work for the *sandbox's* identity (uid 1000 + userns)?

Route A wants the sandbox process itself to build its root: unshare a user
namespace (so it has CAP_SYS_ADMIN of its own), unshare a mount namespace, bind
the host workspace and volumes into the image rootfs, then pivot into it.

Whether that is possible depends on the kernel's rules for mounts made in a
user namespace, and on who created the namespace. This probe answers it by
brute force: each variant runs in a forked child and reports the errno of every
step.

Usage (inside the lane container, as root):

    python3 -u deploy/scripts/acceptance/probe_n35_mount_variants.py
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


def call(label: str, fn, quiet: bool = False) -> int:
    ctypes.set_errno(0)
    rc = fn()
    err = ctypes.get_errno()
    if not quiet:
        print(f"    {label}: rc={rc} {os.strerror(err) if err else 'ok'}")
        sys.stdout.flush()
    return 0 if rc == 0 else err


def write_proc(path: str, text: str) -> None:
    try:
        Path(path).write_text(text)
    except OSError as exc:
        print(f"    write {path}={text.strip()!r}: {exc.strerror}")


def child(variant: str, as_uid: int | None, source: Path, target: Path) -> None:
    pid = os.fork()
    if pid != 0:
        os.waitpid(pid, 0)
        return
    print(f"  [{variant}]")
    sys.stdout.flush()
    try:
        if as_uid is not None:
            call(f"setresuid({as_uid})", lambda: libc.setresuid(as_uid, as_uid, as_uid))
            # setresuid clears dumpability -> /proc/self/* is root-owned
            libc.prctl(4, 1, 0, 0, 0)  # PR_SET_DUMPABLE
        host_uid = os.getuid()
        call("unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER))
        write_proc("/proc/self/setgroups", "deny")
        write_proc("/proc/self/uid_map", f"0 {host_uid} 1\n")
        write_proc("/proc/self/gid_map", f"0 {host_uid} 1\n")
        print(f"    inside: uid={os.getuid()} gid={os.getgid()}")
        if call("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS)) != 0:
            return
        call(f"bind {source} -> {target}",
             lambda: libc.mount(str(source).encode(), str(target).encode(),
                                None, MS_BIND, None))
        call(f"tmpfs -> {target}",
             lambda: libc.mount(b"none", str(target).encode(), b"tmpfs", 0, None))
    except Exception as exc:
        print(f"    raised {type(exc).__name__}: {exc}")
    finally:
        os._exit(0)


def sequence_staged(rootfs: Path, work: Path) -> None:
    """The container-runtime recipe: pivot into a *separate* mount of the rootfs.

    V5 pivots into the rootfs' own mount (a self-bind at the same path) and the
    policy mounts end up outside the tree that becomes `/`. This variant gives
    the new root its own mount (`tmpfs`-free, a bind at a staging path under
    /tmp), mounts everything inside *that*, and only then pivots.
    """
    pid = os.fork()
    if pid != 0:
        _, status = os.waitpid(pid, 0)
        print(f"  [V6 staged] child exited {os.WEXITSTATUS(status)}")
        sys.stdout.flush()
        return
    print("  [V6 staged]")
    try:
        call(f"setresuid({SLOT_UID})", lambda: libc.setresuid(SLOT_UID, SLOT_UID, SLOT_UID))
        libc.prctl(4, 1, 0, 0, 0)
        host_uid = os.getuid()
        call("unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER))
        write_proc("/proc/self/setgroups", "deny")
        write_proc("/proc/self/uid_map", f"0 {host_uid} 1\n")
        write_proc("/proc/self/gid_map", f"0 {host_uid} 1\n")
        call("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS))
        call("mount(NULL, /, MS_REC|MS_PRIVATE)",
             lambda: libc.mount(None, b"/", None, 16384 | 262144, None))
        stage = Path("/tmp/n35-stage")
        stage.mkdir(exist_ok=True)
        call(f"mount(rootfs -> {stage})",
             lambda: libc.mount(str(rootfs).encode(), str(stage).encode(), None, 4096 | 16384, None))
        for virtual, host in (("/workspace", work / "a"), ("/dev/null", Path("/dev/null"))):
            target = stage / virtual.removeprefix("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.touch()
            call(f"mount({host} -> {target})",
                 lambda v=host, t=target: libc.mount(str(v).encode(), str(t).encode(), None, 4096, None))
        call("chdir(stage)", lambda: libc.chdir(str(stage).encode()))
        call("pivot_root('.', '.')", lambda: libc.syscall(155, b".", b"."))
        call("umount2('.', MNT_DETACH)", lambda: libc.umount2(b".", 2))
        call("chdir('/')", lambda: libc.chdir(b"/"))
        call("chdir('/workspace')", lambda: libc.chdir(b"/workspace"))
        print(f"    cwd={os.getcwd()} inode={os.stat('.').st_ino} "
              f"host_source_inode={(work / 'a').stat().st_ino if False else 'n/a'}")
        for probe_path in ("./echo_copy", "/workspace/echo_copy"):
            try:
                st = os.stat(probe_path)
                print(f"    stat {probe_path}: inode={st.st_ino} size={st.st_size}")
            except OSError as exc:
                print(f"    stat {probe_path}: {exc.strerror}")
        grand = os.fork()
        if grand == 0:
            try:
                os.execve("./echo_copy", ["./echo_copy", "REL-OK"], {"PATH": "/bin:/usr/bin"})
            except OSError as exc:
                print(f"    exec ./echo_copy failed: {exc}")
                sys.stdout.flush()
            os._exit(1)
        _, grand_status = os.waitpid(grand, 0)
        print(f"    grandchild exit={os.WEXITSTATUS(grand_status)}")
    except Exception as exc:
        print(f"    raised {type(exc).__name__}: {exc}")
    finally:
        sys.stdout.flush()
        os._exit(0)


def sequence_rebind(rootfs: Path, work: Path) -> None:
    """Bind the policy mounts first, then recursively re-bind the rootfs.

    `MS_BIND|MS_REC` replicates the subtree, so a self-bind taken *after* the
    policy mounts carries them into the mount the pivot moves. No staging path,
    nothing left behind: the old root (and the original mounts) is detached.
    """
    pid = os.fork()
    if pid != 0:
        _, status = os.waitpid(pid, 0)
        print(f"  [V7 rebind] child exited {os.WEXITSTATUS(status)}")
        sys.stdout.flush()
        return
    print("  [V7 rebind]")
    try:
        call(f"setresuid({SLOT_UID})", lambda: libc.setresuid(SLOT_UID, SLOT_UID, SLOT_UID))
        libc.prctl(4, 1, 0, 0, 0)
        host_uid = os.getuid()
        call("unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER))
        write_proc("/proc/self/setgroups", "deny")
        write_proc("/proc/self/uid_map", f"0 {host_uid} 1\n")
        write_proc("/proc/self/gid_map", f"0 {host_uid} 1\n")
        call("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS))
        call("mount(NULL, /, MS_REC|MS_PRIVATE)",
             lambda: libc.mount(None, b"/", None, 16384 | 262144, None))
        for virtual, host in (("/workspace", work / "a"), ("/dev/null", Path("/dev/null"))):
            target = rootfs / virtual.removeprefix("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.touch()
            call(f"mount({host} -> {target})",
                 lambda v=host, t=target: libc.mount(str(v).encode(), str(t).encode(), None, 4096, None))
        call("self-bind rootfs (MS_BIND|MS_REC) AFTER the mounts",
             lambda: libc.mount(str(rootfs).encode(), str(rootfs).encode(), None, 4096 | 16384, None))
        call("chdir(rootfs)", lambda: libc.chdir(str(rootfs).encode()))
        call("pivot_root('.', '.')", lambda: libc.syscall(155, b".", b"."))
        call("umount2('.', MNT_DETACH)", lambda: libc.umount2(b".", 2))
        call("chdir('/')", lambda: libc.chdir(b"/"))
        call("chdir('/workspace')", lambda: libc.chdir(b"/workspace"))
        for probe_path in ("./echo_copy", "/workspace/echo_copy"):
            try:
                st = os.stat(probe_path)
                print(f"    stat {probe_path}: inode={st.st_ino} size={st.st_size}")
            except OSError as exc:
                print(f"    stat {probe_path}: {exc.strerror}")
        grand = os.fork()
        if grand == 0:
            try:
                os.execve("./echo_copy", ["./echo_copy", "REL-OK"], {"PATH": "/bin:/usr/bin"})
            except OSError as exc:
                print(f"    exec ./echo_copy failed: {exc}")
                sys.stdout.flush()
            os._exit(1)
        _, grand_status = os.waitpid(grand, 0)
        print(f"    grandchild exit={os.WEXITSTATUS(grand_status)}")
    except Exception as exc:
        print(f"    raised {type(exc).__name__}: {exc}")
    finally:
        sys.stdout.flush()
        os._exit(0)


def sequence_full_list(rootfs: Path, work: Path) -> None:
    """V7's order, with the fork's full mount list (workspace + the six /dev nodes)."""
    pid = os.fork()
    if pid != 0:
        _, status = os.waitpid(pid, 0)
        print(f"  [V8 full-list] child exited {os.WEXITSTATUS(status)}")
        sys.stdout.flush()
        return
    print("  [V8 full-list]")
    try:
        call(f"setresuid({SLOT_UID})", lambda: libc.setresuid(SLOT_UID, SLOT_UID, SLOT_UID))
        libc.prctl(4, 1, 0, 0, 0)
        host_uid = os.getuid()
        call("unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER))
        write_proc("/proc/self/setgroups", "deny")
        write_proc("/proc/self/uid_map", f"0 {host_uid} 1\n")
        write_proc("/proc/self/gid_map", f"0 {host_uid} 1\n")
        call("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS))
        call("mount(NULL, /, MS_REC|MS_PRIVATE)",
             lambda: libc.mount(None, b"/", None, 16384 | 262144, None))
        pairs = [
            ("/home/user", work / "a"),
            ("/workspace", work / "a"),
            ("/dev/ptmx", Path("/dev/ptmx")),
            ("/dev/pts", Path("/dev/pts")),
            ("/dev/null", Path("/dev/null")),
            ("/dev/urandom", Path("/dev/urandom")),
            ("/dev/zero", Path("/dev/zero")),
            ("/dev/tty", Path("/dev/tty")),
        ]
        for virtual, host in pairs:
            target = rootfs / virtual.removeprefix("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    target.touch()
                except OSError as exc:
                    print(f"    touch {target}: {exc.strerror}")
                    continue
            call(f"mount({host} -> {virtual})",
                 lambda v=host, t=target: libc.mount(str(v).encode(), str(t).encode(), None, 4096, None))
        call("self-bind rootfs (MS_BIND|MS_REC)",
             lambda: libc.mount(str(rootfs).encode(), str(rootfs).encode(), None, 4096 | 16384, None))
        call("chdir(rootfs)", lambda: libc.chdir(str(rootfs).encode()))
        call("pivot_root('.', '.')", lambda: libc.syscall(155, b".", b"."))
        call("umount2('.', MNT_DETACH)", lambda: libc.umount2(b".", 2))
        call("chdir('/')", lambda: libc.chdir(b"/"))
        call("chdir('/workspace')", lambda: libc.chdir(b"/workspace"))
        for probe_path in ("./echo_copy", "/workspace/echo_copy"):
            try:
                st = os.stat(probe_path)
                print(f"    stat {probe_path}: inode={st.st_ino} size={st.st_size}")
            except OSError as exc:
                print(f"    stat {probe_path}: {exc.strerror}")
        grand = os.fork()
        if grand == 0:
            try:
                os.execve("./echo_copy", ["./echo_copy", "REL-OK"], {"PATH": "/bin:/usr/bin"})
            except OSError as exc:
                print(f"    exec ./echo_copy failed: {exc}")
                sys.stdout.flush()
            os._exit(1)
        _, grand_status = os.waitpid(grand, 0)
        print(f"    grandchild exit={os.WEXITSTATUS(grand_status)}")
    except Exception as exc:
        print(f"    raised {type(exc).__name__}: {exc}")
    finally:
        sys.stdout.flush()
        os._exit(0)


def sequence_pidns_procfs(work: Path) -> None:
    """Can a userns-root sandbox mount a real procfs -- and does it need a PID ns?

    (a) without one: the kernel refuses (the userns does not own this pid ns);
    (b) with `unshare(CLONE_NEWPID)` + a fork: the mount is permitted, which is
    the recipe a real /proc inside a real root would have to use.
    """
    for with_pid_ns in (False, True):
        pid = os.fork()
        if pid != 0:
            os.waitpid(pid, 0)
            continue
        label = "with CLONE_NEWPID" if with_pid_ns else "no pid ns"
        try:
            libc.prctl(4, 1, 0, 0, 0)
            host_uid = os.getuid()
            call(f"[{label}] unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER))
            write_proc("/proc/self/setgroups", "deny")
            write_proc("/proc/self/uid_map", f"0 {host_uid} 1\n")
            write_proc("/proc/self/gid_map", f"0 {host_uid} 1\n")
            if with_pid_ns:
                call("unshare(CLONE_NEWPID)", lambda: libc.unshare(0x20000000))
                # CLONE_NEWPID puts the *children* in the new namespace, so the
                # mount has to happen after a fork -- this is the step the first
                # attempt got wrong.
                grand = os.fork()
                if grand != 0:
                    os.waitpid(grand, 0)
                    os._exit(0)
                print(f"    [{label}] after fork: pid={os.getpid()}")
            call("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS))
            target = Path("/tmp/n35-procfs")
            target.mkdir(exist_ok=True)
            code = call(f"[{label}] mount proc -> {target}",
                        lambda: libc.mount(b"proc", str(target).encode(), b"proc", 0, None))
            os._exit(0 if code == 0 else 1)
        except Exception as exc:
            print(f"    raised {type(exc).__name__}: {exc}")
            sys.stdout.flush()
            os._exit(2)


def sequence_procfs_on_own_mount() -> None:
    """Is procfs refused because of the *filesystem* or the *target mount*?

    V10 mounted procfs onto a path inside the container's root mount (not owned
    by our namespace) and got EPERM even with a PID namespace of our own. This
    variant first mounts a tmpfs we own, then mounts procfs inside *that* -- the
    shape a real root has, where /proc would land inside the sandbox's own bind.
    """
    for with_pid_ns in (False, True):
        pid = os.fork()
        if pid != 0:
            os.waitpid(pid, 0)
            continue
        label = "own-mount + pid ns" if with_pid_ns else "own-mount, no pid ns"
        try:
            libc.prctl(4, 1, 0, 0, 0)
            host_uid = os.getuid()
            call(f"[{label}] unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER))
            write_proc("/proc/self/setgroups", "deny")
            write_proc("/proc/self/uid_map", f"0 {host_uid} 1\n")
            write_proc("/proc/self/gid_map", f"0 {host_uid} 1\n")
            if with_pid_ns:
                libc.unshare(0x20000000)
                grand = os.fork()
                if grand != 0:
                    os.waitpid(grand, 0)
                    os._exit(0)
            libc.unshare(CLONE_NEWNS)
            stage = Path("/tmp/n35-ownmount")
            stage.mkdir(exist_ok=True)
            call(f"[{label}] tmpfs -> {stage}",
                 lambda: libc.mount(b"none", str(stage).encode(), b"tmpfs", 0, None))
            inner = stage / "proc"
            inner.mkdir(exist_ok=True)
            call(f"[{label}] mount proc -> {inner}",
                 lambda: libc.mount(b"proc", str(inner).encode(), b"proc", 0, None))
            os._exit(0)
        except Exception as exc:
            print(f"    raised {type(exc).__name__}: {exc}")
            sys.stdout.flush()
            os._exit(2)


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    from tests.security.conftest import resolve_test_rootfs

    rootfs = Path(resolve_test_rootfs("python:3.11-slim"))
    work = Path("/tmp/n35-variants")
    (work / "a").mkdir(parents=True, exist_ok=True)
    (work / "b").mkdir(parents=True, exist_ok=True)
    (work / "a" / "marker").write_text("A\n")
    import shutil as _shutil
    _shutil.copy("/bin/echo", work / "a" / "echo_copy")
    (work / "a" / "echo_copy").chmod(0o755)
    print(f"rootfs={rootfs} mode={oct(rootfs.stat().st_mode & 0o7777)}")

    # 1. The privileged baseline: container root maps itself to itself.
    child("V1 root + map 0->0 + bind /tmp/a -> /tmp/b", None, work / "a", work / "b")
    # 2. The shape route A needs: the slot uid maps itself to guest root.
    child("V2 uid1000 + map 0->1000 + bind /tmp/a -> /tmp/b",
          SLOT_UID, work / "a", work / "b")
    # 3. Same identity, but the source lives inside the image rootfs.
    (rootfs / "workspace").mkdir(exist_ok=True)
    (rootfs / "workspace2").mkdir(exist_ok=True)
    child("V3 uid1000 + map 0->1000 + bind rootfs/workspace -> rootfs/workspace2",
          SLOT_UID, rootfs / "workspace", rootfs / "workspace2")
    # 4. Same identity, a fresh filesystem (the canonical userns mount).
    child("V4 uid1000 + map 0->1000 + tmpfs -> /tmp/n35-variants/b",
          SLOT_UID, work / "a", work / "b")
    # 5. The fork's own sequence, step by step, as the slot uid: this is what
    #    `realroot::real_root` does, and any step that dies instead of returning
    #    an errno shows up in the child's wait status here.
    sequence_as_slot(rootfs, work, (work / "a").stat().st_ino)
    sequence_staged(rootfs, work)
    sequence_rebind(rootfs, work)
    sequence_full_list(rootfs, work)
    sequence_pidns_procfs(work)
    sequence_procfs_on_own_mount()
    return 0


def sequence_as_slot(rootfs: Path, work: Path, host_inode: int) -> None:
    import ctypes as _ctypes

    libc_c = _ctypes.CDLL("libc.so.6", use_errno=True)
    pid = os.fork()
    if pid != 0:
        _, status = os.waitpid(pid, 0)
        if os.WIFSIGNALED(status):
            print(f"  [V5 sequence] child died from signal {os.WTERMSIG(status)}")
        else:
            print(f"  [V5 sequence] child exited {os.WEXITSTATUS(status)}")
        sys.stdout.flush()
        return
    print("  [V5 sequence]")
    try:
        call(f"setresuid({SLOT_UID})", lambda: libc.setresuid(SLOT_UID, SLOT_UID, SLOT_UID))
        libc.prctl(4, 1, 0, 0, 0)  # PR_SET_DUMPABLE
        host_uid = os.getuid()
        call("unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER))
        write_proc("/proc/self/setgroups", "deny")
        write_proc("/proc/self/uid_map", f"0 {host_uid} 1\n")
        write_proc("/proc/self/gid_map", f"0 {host_uid} 1\n")
        call("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS))
        # MS_REC|MS_PRIVATE = 16384 | 262144
        call("mount(NULL, /, MS_REC|MS_PRIVATE)",
             lambda: libc.mount(None, b"/", None, 16384 | 262144, None))
        # self-bind the rootfs so it is a mount point
        call("mount(rootfs, rootfs, MS_BIND|MS_REC)",
             lambda: libc.mount(str(rootfs).encode(), str(rootfs).encode(), None, 4096 | 16384, None))
        # the policy's own mounts: workspace + the dev nodes
        for virtual, host in (("/workspace", work), ("/dev/null", Path("/dev/null"))):
            target = rootfs / virtual.removeprefix("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.touch()
            call(f"mount({host} -> {target})",
                 lambda v=host, t=target: libc.mount(str(v).encode(), str(t).encode(), None, 4096, None))
        call("chdir(rootfs)", lambda: libc.chdir(str(rootfs).encode()))
        call("pivot_root('.', '.')",
             lambda: libc.syscall(155, b".", b"."))
        call("umount2('.', MNT_DETACH)", lambda: libc.umount2(b".", 2))
        call("chdir('/')", lambda: libc.chdir(b"/"))
        # The fork's last step is a chdir to the *guest* cwd; do the same and see
        # whether the mount is what the cwd lands on (absolute exec works,
        # relative exec returns ENOENT -- that is the question).
        call("chdir('/workspace')", lambda: libc.chdir(b"/workspace"))
        print(f"    cwd={os.getcwd()} inode={os.stat('.').st_ino} "
              f"host_source_inode={host_inode}")
        for probe_path in ("./echo_copy", "/workspace/echo_copy", "."):
            try:
                st = os.stat(probe_path)
                print(f"    stat {probe_path}: inode={st.st_ino} size={st.st_size}")
            except OSError as exc:
                print(f"    stat {probe_path}: {exc.strerror}")
        # What does the new root actually look like? And can a binary in it run?
        # (`execvp("/bin/sh")` returning ENOENT after the pivot is the failure
        # the fork hit; this is where its cause shows.)
        for probe_path in ("/bin/sh", "/bin/dash", "/lib64/ld-linux-x86-64.so.2",
                           "/usr/lib/x86_64-linux-gnu/libc.so.6", "/proc"):
            try:
                st = os.stat(probe_path)
                kind = "dir" if os.path.isdir(probe_path) else "file"
                print(f"    {probe_path}: {kind} mode={oct(st.st_mode)}")
            except OSError as exc:
                print(f"    {probe_path}: {exc.strerror}")
        try:
            print(f"    readlink /bin/sh -> {os.readlink('/bin/sh')}")
        except OSError as exc:
            print(f"    readlink /bin/sh: {exc.strerror}")
        grand = os.fork()
        if grand == 0:
            try:
                os.execve("/bin/sh", ["/bin/sh", "-c", "echo PIVOT-EXEC-OK"],
                          {"PATH": "/bin:/usr/bin"})
            except OSError as exc:
                print(f"    execve(/bin/sh) failed: {exc}")
                sys.stdout.flush()
            os._exit(1)
        _, grand_status = os.waitpid(grand, 0)
        print(f"    grandchild status={grand_status} exit={os.WEXITSTATUS(grand_status)}")
        # drop CAP_SYS_ADMIN (bit 21) from effective/permitted/inheritable
        class Hdr(_ctypes.Structure):
            _fields_ = [("version", _ctypes.c_uint32), ("pid", _ctypes.c_int32)]

        class Dat(_ctypes.Structure):
            _fields_ = [
                ("effective", _ctypes.c_uint32),
                ("permitted", _ctypes.c_uint32),
                ("inheritable", _ctypes.c_uint32),
            ]

        hdr = Hdr(version=0x20080522, pid=0)
        data = (Dat * 2)()
        call("capget", lambda: libc_c.syscall(125, _ctypes.byref(hdr), _ctypes.byref(data)))
        data[0].effective &= ~0x200000
        data[0].permitted &= ~0x200000
        data[0].inheritable &= ~0x200000
        call("capset", lambda: libc_c.syscall(126, _ctypes.byref(hdr), _ctypes.byref(data)))
        print("    SEQUENCE OK")
    except Exception as exc:
        print(f"    raised {type(exc).__name__}: {exc}")
    finally:
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
