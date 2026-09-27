"""Which ways of making a file bigger does RLIMIT_FSIZE actually stop?

One fresh sandbox per method, each set up the same way: fill the tree to
900 MiB of a 1024 MiB budget, so the per-exec ceiling is ~124 MiB, then try to
land a 300 MiB file (or a 300 MiB extension) and report what actually landed.

  dd                     -- write()/pwrite()
  truncate -s            -- ftruncate()
  fallocate -l           -- fallocate()
  mmap MAP_SHARED stores -- page faults
  os.copy_file_range     -- kernel-side copy
  sendfile               -- kernel-side copy
"""

import time

from e2b import Sandbox

MIB = 1024 * 1024
FILL_MIB = 900
TARGET_MIB = 300

PROGRAMS = {
    "write (dd)": (
        "dd if=/dev/zero of=/home/user/t.bin bs=1M count={target} "
        "2>&1 | tail -1; stat -c %s /home/user/t.bin"
    ),
    "ftruncate": (
        "python3 -c \"import os\n"
        "fd=os.open('/home/user/t.bin', os.O_CREAT|os.O_WRONLY, 0o644)\n"
        "try:\n"
        "    os.ftruncate(fd, {target}*1024*1024); print('ftruncate ok')\n"
        "except OSError as e: print('ftruncate refused', e)\n"
        "print('size', os.path.getsize('/home/user/t.bin'))\""
    ),
    "fallocate": (
        "python3 -c \"import os\n"
        "fd=os.open('/home/user/t.bin', os.O_CREAT|os.O_WRONLY, 0o644)\n"
        "try:\n"
        "    os.posix_fallocate(fd, 0, {target}*1024*1024); print('fallocate ok')\n"
        "except OSError as e: print('fallocate refused', e)\n"
        "print('size', os.path.getsize('/home/user/t.bin'))\""
    ),
    "mmap stores": (
        # ctypes, not `mmap.mmap`: CPython refuses `length > file size`, and the
        # syscall does not -- the guest can be C, Go or Rust.
        "python3 -c \"import ctypes, ctypes.util, os\n"
        "libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)\n"
        "libc.mmap.restype = ctypes.c_void_p\n"
        "libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]\n"
        "p='/home/user/t.bin'\n"
        "with open(p,'wb') as f: f.write(b'a'*(1<<20))\n"
        "fd=os.open(p, os.O_RDWR)\n"
        "length={target}*1024*1024\n"
        "addr=libc.mmap(None, length, 3, 1, fd, 0)\n"
        "if addr == ctypes.c_void_p(-1).value:\n"
        "    print('mmap refused errno', ctypes.get_errno())\n"
        "else:\n"
        "    print('mmap ok, len', length)\n"
        "    ctypes.memset(addr + length - 1, 98, 1)\n"
        "    ctypes.memset(addr + (1<<20), 98, 1)\n"
        "    print('stores ok')\n"
        "    os.posix_fadvise(fd, 0, 0, 3)\n"
        "    libc.msync(ctypes.c_void_p(addr), ctypes.c_size_t(length), 4)\n"
        "print('size', os.path.getsize(p))\""
    ),
    "copy_file_range": (
        "python3 -c \"import os\n"
        "src='/home/user/src.bin'\n"
        "with open(src,'wb') as f: f.truncate((1<<20))\n"
        "s=os.open(src, os.O_RDONLY); d=os.open('/home/user/t.bin', os.O_CREAT|os.O_WRONLY, 0o644)\n"
        "left={target}*1024*1024; off=0\n"
        "while left>0:\n"
        "    n=os.copy_file_range(s, d, min(left, 1<<20), offset_src=0, offset_dst=off)\n"
        "    if n==0: break\n"
        "    off+=n; left-=n\n"
        "print('copied', off)\n"
        "print('size', os.path.getsize('/home/user/t.bin'))\""
    ),
    "sendfile": (
        "python3 -c \"import os\n"
        "src='/home/user/src.bin'\n"
        "with open(src,'wb') as f: f.truncate((1<<20))\n"
        "s=os.open(src, os.O_RDONLY); d=os.open('/home/user/t.bin', os.O_CREAT|os.O_WRONLY, 0o644)\n"
        "left={target}*1024*1024; off=0\n"
        "while left>0:\n"
        "    n=os.sendfile(d, s, off, min(left, 1<<20))\n"
        "    if n==0: break\n"
        "    off+=n; left-=n\n"
        "print('sent', off)\n"
        "print('size', os.path.getsize('/home/user/t.bin'))\""
    ),
}


def used(sb) -> int:
    m = sb.get_metrics()
    m = m[0] if isinstance(m, list) else m
    return int(m.disk_used)


def run(name: str, program: str) -> None:
    sb = Sandbox.create(timeout=1800)
    try:
        sb.commands.run(
            f"dd if=/dev/zero of=/home/user/fill.bin bs=1M count={FILL_MIB} status=none",
            timeout=900,
        )
        time.sleep(8)
        try:
            out = sb.commands.run(program, timeout=900)
        except Exception as exc:  # noqa: BLE001 - one method failing is data
            print(f"--- {name}: command failed: {str(exc)[:160]}", flush=True)
            return
        time.sleep(6)
        lines = [line for line in out.stdout.splitlines() if line.strip()]
        print(f"--- {name}: rc={out.exit_code}", flush=True)
        for line in lines[-4:]:
            print(f"    {line.strip()[:110]}", flush=True)
        if out.stderr.strip():
            print(f"    stderr: {out.stderr.strip()[:110]}", flush=True)
        print(f"    platform used: {used(sb) / MIB:.1f} MiB", flush=True)
    finally:
        sb.kill()


def main() -> int:
    print(f"budget 1024 MiB, filled {FILL_MIB} MiB, target {TARGET_MIB} MiB",
          flush=True)
    for name, program in PROGRAMS.items():
        run(name, program.format(target=TARGET_MIB))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
