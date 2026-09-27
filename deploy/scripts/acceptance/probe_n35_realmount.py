"""Can a real root (mount ns + pivot_root) be built in the deployed shape?

The N14 route needs the sandbox's own init process to unshare a mount
namespace, bind the rootfs and the workspace into it, and pivot into it. Three
things have to be true, and none of them is visible from the guest (its /proc is
synthesized):

  1. the process has CAP_SYS_ADMIN *in a namespace it can use* -- measured
     separately as `probe_n35_ns.py`: the sandbox child runs in its own user
     namespace with CapEff=000001ffffffffff, i.e. yes;
  2. `unshare(CLONE_NEWNS)` and `mount`/`pivot_root` are not blocked by the
     *worker container's* seccomp profile (deploy/seccomp/sandlock-worker.json).
     That profile allows mount/umount2 only for a process that has
     CAP_SYS_ADMIN, and does NOT list pivot_root at all (so it lands on
     defaultAction: SCMP_ACT_ERRNO). Whether that cap gate is evaluated against
     the *container's* cap set (runc-time) or against the process's live caps
     (userns-root) is the question this probe answers;
  3. the same steps work with the cap set the manifests actually deploy
     (PROD_DROP_CAPS=SYS_ADMIN).

Usage (inside the lane container, root):

    python3 -u tmp/k0s/probe_n35_realmount.py
    PROD_DROP_CAPS=SYS_ADMIN sh tmp/k0s/n35-lane.sh python3 -u tmp/k0s/probe_n35_realmount.py
"""

import ctypes
import os
import sys
from pathlib import Path

libc = ctypes.CDLL("libc.so.6", use_errno=True)

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
MS_BIND = 4096


def status_field(field: str) -> str:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(field):
            return line.split(":", 1)[1].strip()
    return "<absent>"


def step(label: str, fn):
    ctypes.set_errno(0)
    result = fn()
    err = ctypes.get_errno()
    detail = os.strerror(err) + f" (errno {err})" if err else "ok"
    print(f"{label}: rc={result} {detail}")
    sys.stdout.flush()
    return result == 0


def write(path: str, text: str) -> bool:
    try:
        Path(path).write_text(text)
        return True
    except OSError as exc:
        print(f"  write {path} failed: {exc}")
        return False


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    print(f"uid={os.getuid()} gid={os.getgid()} "
          f"CapEff={status_field('CapEff')} Seccomp={status_field('Seccomp')}")
    base = Path("/tmp/n35-realmount")
    src = base / "src"
    root = base / "root"
    src.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    (src / "marker").write_text("REAL-MOUNT-OK\n")

    # 1. user namespace first (production has no CAP_SYS_ADMIN in the container,
    #    so this is the only way to get one; the codes below are the rootless
    #    container pattern).
    if step("unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER)):
        write("/proc/self/setgroups", "deny")
        write("/proc/self/uid_map", "0 0 1\n")
        write("/proc/self/gid_map", "0 0 1\n")
        print(f"  after userns: uid={os.getuid()} CapEff={status_field('CapEff')}")

    # 2. mount namespace.
    step("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS))
    step("mount(NULL, /, NULL, MS_REC|MS_PRIVATE, NULL)",
         lambda: libc.mount(None, b"/", None, MS_BIND | 16384 | 262144, None))

    # 3. a real bind mount inside it.
    step("mount --bind src root",
         lambda: libc.mount(str(src).encode(), str(root).encode(), None, MS_BIND, None))
    step("mount --bind workspace-like",
         lambda: libc.mount(b"/tmp", b"/tmp/n35-realmount/src", None, MS_BIND, None))

    # 4. pivot_root (needs the new root to be a mount point -- step 3 made it one).
    put_old = root / "put_old"
    put_old.mkdir(exist_ok=True)
    ok = step("pivot_root(root, root/put_old)",
              lambda: libc.syscall(
                  ctypes.c_long(155),  # x86_64 SYS_pivot_root
                  str(root).encode(),
                  str(put_old).encode(),
              ))
    if ok:
        try:
            print("  after pivot_root: /marker = "
                  + Path("/marker").read_text().strip())
        except OSError as exc:
            print(f"  after pivot_root: reading /marker failed: {exc}")

    # 5. The cheaper variant: plain chroot(2) into a prepared root, no mount
    #    namespace. The profile allows chroot only for CAP_SYS_CHROOT, and the
    #    deployed worker pod adds neither SYS_CHROOT nor SYS_ADMIN.
    chroot_root = base / "chroot-root"
    chroot_root.mkdir(exist_ok=True)
    (chroot_root / "marker").write_text("CHROOT-OK\n")
    if step("chroot(root) + chdir(/) [before: cwd=%s]" % os.getcwd(),
            lambda: libc.chroot(str(chroot_root).encode())):
        step("chdir(/)", lambda: libc.chdir(b"/"))
        try:
            print("  after chroot: /marker = " + Path("/marker").read_text().strip())
        except OSError as exc:
            print(f"  after chroot: reading /marker failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
