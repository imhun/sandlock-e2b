"""Exactly where does a MAP_SHARED store stop growing the file?

`RLIMIT_FSIZE` is enforced on the write() path (`generic_write_checks`), not on
page faults, so the question is what the fault path itself does. Three shapes,
each in its own child so a fault cannot take the probe with it:

  a. file ends mid-page, store *inside* that last page but past EOF
  b. file ends mid-page, store in the *next* page
  c. file ends page-aligned, store in the next page
  d. control: store inside the file

Reported per shape: the child's exit status (SIGBUS shows up as -7) and the
file's size afterwards.
"""

import base64
import sys

from e2b import Sandbox

PROBE = r'''
import ctypes
import ctypes.util
import os

libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_long,
]
PAGE = 4096
PROT_READ, PROT_WRITE, MAP_SHARED = 1, 2, 1


def store(path, size, offset, label):
    with open(path, "wb") as fh:
        fh.write(b"a" * size)
    pid = os.fork()
    if pid == 0:
        fd = os.open(path, os.O_RDWR)
        length = max(offset + PAGE, size)
        addr = libc.mmap(None, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0)
        if addr == ctypes.c_void_p(-1).value:
            print(label, "mmap refused errno", ctypes.get_errno(), flush=True)
            os._exit(11)
        ctypes.memset(addr + offset, 98, 1)
        libc.msync(ctypes.c_void_p(addr), ctypes.c_size_t(length), 2)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    after = os.path.getsize(path)
    print(
        f"{label}: child_exit={code} size_before={size} size_after={after} "
        f"grew_by={after - size}",
        flush=True,
    )


base = "/home/user"
store(f"{base}/a.bin", PAGE + 100, PAGE + 200, "a in-last-page")
store(f"{base}/b.bin", PAGE + 100, 2 * PAGE + 10, "b next-page")
store(f"{base}/c.bin", PAGE, 2 * PAGE + 10, "c page-aligned")
store(f"{base}/d.bin", PAGE + 100, 1000, "d control")
'''


def main() -> int:
    sb = Sandbox.create(timeout=900)
    print("sandbox", sb.sandbox_id, flush=True)
    payload = base64.b64encode(PROBE.encode()).decode()
    out = sb.commands.run(
        f"python3 -c \"import base64;exec(base64.b64decode('{payload}'))\"",
        timeout=300,
    )
    print(out.stdout.strip())
    if out.stderr.strip():
        print("stderr:", out.stderr.strip()[:400])
    used = sb.get_metrics()
    used = used[0] if isinstance(used, list) else used
    print(f"platform used: {int(used.disk_used)} bytes")
    sb.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
