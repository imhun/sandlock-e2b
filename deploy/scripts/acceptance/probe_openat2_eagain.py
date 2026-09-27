"""Does *this* kernel answer EAGAIN for openat2(RESOLVE_IN_ROOT) through a `..`?

FUP-28 hangs on that question: the E2B side rewrites `..`-relative symlinks in
an image before the sandbox sees them, purely to avoid a kernel `EAGAIN` that
the fork's resolver used to treat as fatal. FUP-26 added a bounded retry, so
the rewrite is only worth keeping if the kernel really does answer EAGAIN on
the hosts we deploy to (measured on the dev kernel: ~231 in 97482 managed
opens, i.e. ~1/400, and only under a racing rename on the walked path).

So this probe reproduces the mechanism directly -- a `..`-relative symlink,
RESOLVE_IN_ROOT, and a thread renaming the target directory underneath the
walk -- and prints the errno histogram. Prints "kernel never said EAGAIN" when
that is the answer, because that is a real (and decision-changing) result.

Usage: python3 probe_openat2_eagain.py [iterations] [racer_on|racer_off]
"""

import ctypes
import errno
import os
import shutil
import sys
import tempfile
import threading
import time

SYS_OPENAT2 = 437  # same number on x86_64 and arm64
RESOLVE_IN_ROOT = 0x10
O_PATH = 0o10000000
AT_FDCWD = -100

libc = ctypes.CDLL("libc.so.6", use_errno=True)


class OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def openat2(dirfd: int, path: str, how: OpenHow) -> int:
    return libc.syscall(
        ctypes.c_long(SYS_OPENAT2),
        ctypes.c_int(dirfd),
        ctypes.c_char_p(path.encode()),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )


def build_fixture(root: str) -> None:
    """`<root>/lib64/ld.so -> ../lib/real/ld.so` plus the real file.

    The shape of a distro's ELF interpreter, which is exactly the link FUP-26
    was about (`lib64/ld-linux-x86-64.so.2 -> ../lib/x86_64-linux-gnu/...`).
    """
    os.makedirs(os.path.join(root, "lib", "real"), exist_ok=True)
    with open(os.path.join(root, "lib", "real", "ld.so"), "wb") as fh:
        fh.write(b"\x7fELF")
    os.makedirs(os.path.join(root, "lib64"), exist_ok=True)
    link = os.path.join(root, "lib64", "ld.so")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink("../lib/real/ld.so", link)


def racer(root: str, stop: threading.Event) -> None:
    """Rename the *lower* directory under the walk (the documented trigger)."""
    real = os.path.join(root, "lib", "real")
    other = os.path.join(root, "lib", "real2")
    while not stop.is_set():
        try:
            os.rename(real, other)
            os.rename(other, real)
        except OSError:
            time.sleep(0.0001)


def main() -> int:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 40000
    racer_on = (sys.argv[2] if len(sys.argv) > 2 else "racer_on") != "racer_off"
    root = tempfile.mkdtemp(prefix="eagain-probe-")
    try:
        build_fixture(root)
        rootfd = os.open(root, os.O_PATH | os.O_DIRECTORY)
        how = OpenHow(flags=O_PATH, mode=0, resolve=RESOLVE_IN_ROOT)
        stop = threading.Event()
        threads = []
        if racer_on:
            for _ in range(4):
                th = threading.Thread(target=racer, args=(root, stop), daemon=True)
                th.start()
                threads.append(th)
        counts: dict[int, int] = {}
        ok = 0
        started = time.monotonic()
        for _ in range(iterations):
            ctypes.set_errno(0)
            fd = openat2(rootfd, "lib64/ld.so", how)
            if fd >= 0:
                ok += 1
                os.close(fd)
            else:
                counts[ctypes.get_errno()] = counts.get(ctypes.get_errno(), 0) + 1
        elapsed = time.monotonic() - started
        stop.set()
        for th in threads:
            th.join(timeout=1)
        print(f"kernel: {os.uname().release} ({os.uname().machine})")
        print(f"iterations={iterations} racer={'on' if racer_on else 'off'} "
              f"elapsed={elapsed:.1f}s rate={iterations / elapsed:.0f}/s")
        print(f"ok={ok}")
        for err, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  errno {err} ({errno.errorcode.get(err, '?')}): {count}")
        if counts.get(errno.EAGAIN):
            print(f"RESULT: kernel DOES answer EAGAIN under this load "
                  f"({counts[errno.EAGAIN]}/{iterations})")
        else:
            print("RESULT: kernel never said EAGAIN (the rewrite protects "
                  "against nothing on this host)")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
