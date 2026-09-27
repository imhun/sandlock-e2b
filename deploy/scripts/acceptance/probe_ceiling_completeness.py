"""Can anything still grow the tree once the ceiling is exactly zero?

The tight-ceiling half is covered by `probe_write_paths.py` and
`probe_kernel_copy.py` (each path in its own sandbox with ~124 MiB left). This
is the stricter half: fill a 1024 MiB sandbox *to the byte*, then try every way
to grow a file -- on files that already exist, so the entry gate cannot be what
stops it. The platform's number must not move.

Test files are created *before* the fill, because once the pool is empty the
mediator refuses new entries (which is itself one of the results here).
"""

import base64
import sys
import time

from e2b import Sandbox

MIB = 1024 * 1024

PHASE = r'''
import ctypes
import ctypes.util
import os
import resource

libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_long,
]
PAGE = 4096
TEST = 300 * 1024 * 1024

cur, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
print(f"  ceiling this command: cur={cur} hard={hard}", flush=True)


def store(path, size, offset, label):
    """MAP_SHARED store in a child, so a fault cannot take the probe down."""
    before = os.path.getsize(path)
    pid = os.fork()
    if pid == 0:
        fd = os.open(path, os.O_RDWR)
        length = max(offset + PAGE, size)
        addr = libc.mmap(None, length, 3, 1, fd, 0)
        if addr == ctypes.c_void_p(-1).value:
            os._exit(12)
        ctypes.memset(addr + offset, 98, 1)
        libc.msync(ctypes.c_void_p(addr), ctypes.c_size_t(length), 2)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    note = {0: "store ok", -7: "SIGBUS", 12: "mmap refused"}.get(code, f"exit {code}")
    after = os.path.getsize(path)
    print(f"  {label:32s} {note:12s} size {before} -> {after}", flush=True)


try:
    with open("/home/user/new.bin", "wb") as fh:
        fh.write(b"x" * 1024)
    print("  new file (O_CREAT)              created", flush=True)
except OSError as exc:
    print(f"  new file (O_CREAT)              refused {exc}", flush=True)

for label, opener, call in (
    ("write 300 MiB", os.O_WRONLY, lambda fd: os.write(fd, b"x" * (1 << 20))),
    ("ftruncate 300 MiB", os.O_WRONLY, lambda fd: os.ftruncate(fd, TEST)),
    ("fallocate 300 MiB", os.O_WRONLY, lambda fd: os.posix_fallocate(fd, 0, TEST)),
):
    path = "/home/user/" + label.split()[0] + ".bin"
    fd = os.open(path, opener)
    before = os.path.getsize(path)
    try:
        done = 0
        while done < TEST:
            call(fd)
            done += 1 << 20
        print(f"  {label:32s} landed       size {before} -> {os.path.getsize(path)}",
              flush=True)
    except OSError as exc:
        print(f"  {label:32s} {exc.errno}: {exc.strerror}", flush=True)
    finally:
        os.close(fd)

store("/home/user/mm_last_page.bin", PAGE + 100, PAGE + 200, "mmap past EOF, last page")
store("/home/user/mm_next_page.bin", PAGE + 100, 2 * PAGE + 10, "mmap past EOF, next page")
store("/home/user/mm_inside.bin", PAGE + 100, 1000, "mmap inside the file")

for label, fn in (
    ("copy_file_range 300 MiB",
     lambda s, d: os.copy_file_range(s, d, 1 << 20, offset_src=0, offset_dst=0)),
    ("sendfile 300 MiB", lambda s, d: os.sendfile(d, s, 0, 1 << 20)),
):
    path = "/home/user/" + label.split()[0] + ".bin"
    s = os.open("/home/user/fill.bin", os.O_RDONLY)
    d = os.open(path, os.O_WRONLY)
    before = os.path.getsize(path)
    try:
        moved = 0
        while moved < TEST:
            n = fn(s, d)
            if not n:
                break
            moved += n
        print(f"  {label:32s} landed {moved:>10}  size {before} -> "
              f"{os.path.getsize(path)}", flush=True)
    except OSError as exc:
        print(f"  {label:32s} {exc.errno}: {exc.strerror} (after {moved})",
              flush=True)
    finally:
        os.close(s)
        os.close(d)
'''


def used(sb) -> int:
    m = sb.get_metrics()
    m = m[0] if isinstance(m, list) else m
    return int(m.disk_used)


def phase(sb, label: str) -> None:
    print(f"--- {label}", flush=True)
    time.sleep(4)
    print(f"  platform used before: {used(sb) / MIB:.1f} MiB", flush=True)
    payload = base64.b64encode(PHASE.encode()).decode()
    out = sb.commands.run(
        f"python3 -c \"import base64;exec(base64.b64decode('{payload}'))\"",
        timeout=600,
    )
    print(out.stdout.rstrip())
    if out.stderr.strip():
        print("  stderr:", out.stderr.strip()[:300], flush=True)
    time.sleep(6)
    print(f"  platform used after:  {used(sb) / MIB:.1f} MiB", flush=True)


def main() -> int:
    sb = Sandbox.create(timeout=1800)
    print("sandbox", sb.sandbox_id, flush=True)
    try:
        # Every target exists before the fill: the entry gate must not be what
        # stops a growth attempt.
        sb.commands.run(
            "cd /home/user && for n in write ftruncate fallocate copy_file_range "
            "sendfile mm_last_page mm_next_page mm_inside; do "
            "head -c 4096 /dev/zero > $n.bin; done; ls -l *.bin | head -3",
            timeout=300,
        )
        sb.commands.run(
            "dd if=/dev/zero of=/home/user/fill.bin bs=1M count=900 status=none",
            timeout=900,
        )
        sb.commands.run(
            # Lands only what is left (the rest is EFBIG, which is the point).
            "dd if=/dev/zero of=/home/user/rest.bin bs=1M count=1024 status=none "
            "|| true",
            timeout=900,
        )
        phase(sb, "exhausted: the tree is at the 1024 MiB budget")
    finally:
        sb.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
