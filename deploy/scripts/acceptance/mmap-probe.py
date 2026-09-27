"""Does a MAP_SHARED store past EOF grow the file on *this* filesystem?

Run as: mmap-probe.py <dir> <label>

Four shapes, each in its own file and its own child (a fault must not take the
probe down). The sizes are chosen so the mapping is far larger than the file:
if the filesystem allows a mapped store to extend the file, the growth shows up
in `size_after` immediately.
"""

import ctypes
import ctypes.util
import os
import sys

libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_long,
]

PAGE = 4096
MIB = 1 << 20
MAP_SHARED = 1


def store(path, size, offset, label):
    with open(path, "wb") as fh:
        fh.write(b"a" * size)
    pid = os.fork()
    if pid == 0:
        fd = os.open(path, os.O_RDWR)
        length = max(offset + PAGE, size, 8 * MIB)
        addr = libc.mmap(None, length, 3, MAP_SHARED, fd, 0)
        if addr == ctypes.c_void_p(-1).value:
            print(f"  {label:34s} mmap refused errno={ctypes.get_errno()}", flush=True)
            os._exit(12)
        ctypes.memset(addr + offset, 98, 1)
        libc.msync(ctypes.c_void_p(addr), ctypes.c_size_t(length), 2)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    note = {0: "store ok", -7: "SIGBUS", 12: "mmap refused"}.get(code, f"exit {code}")
    after = os.path.getsize(path)
    print(
        f"  {label:34s} {note:12s} size {size} -> {after} (grew {after - size})",
        flush=True,
    )


def main() -> int:
    directory = sys.argv[1]
    label = sys.argv[2]
    os.makedirs(directory, exist_ok=True)
    print(f"== {label} ({directory})", flush=True)
    try:
        store(f"{directory}/a.bin", PAGE, PAGE + 10, "page-aligned EOF, next page")
        store(f"{directory}/b.bin", PAGE, 4 * MIB, "page-aligned EOF, 4 MiB out")
        store(f"{directory}/c.bin", PAGE + 100, PAGE + 104, "inside last partial page")
        store(f"{directory}/d.bin", PAGE + 100, 4 * MIB, "4 MiB out, mid-page EOF")
        store(f"{directory}/e.bin", PAGE + 100, 1000, "control, inside the file")
    finally:
        for name in ("a", "b", "c", "d", "e"):
            try:
                os.unlink(f"{directory}/{name}.bin")
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
