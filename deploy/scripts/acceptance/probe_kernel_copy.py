"""Do kernel-side copies respect RLIMIT_FSIZE?

`write`/`ftruncate`/`fallocate` are checked (measured), and a page fault past
EOF is SIGBUS with no growth (measured). The remaining way to make a file
bigger without a write() is a copy the kernel does itself: `copy_file_range`
and `sendfile`. Same setup as the other probes -- 900 MiB of a 1024 MiB budget,
so ~124 MiB of ceiling left -- and each tries to land 300 MiB.
"""

import time

from e2b import Sandbox

MIB = 1024 * 1024
FILL_MIB = 900
TARGET_MIB = 300

PROGRAMS = {
    "copy_file_range": (
        "python3 -c \"import os\n"
        "s=os.open('/home/user/fill.bin', os.O_RDONLY)\n"
        "d=os.open('/home/user/t.bin', os.O_CREAT|os.O_WRONLY, 0o644)\n"
        "want={target}*1024*1024; off=0\n"
        "while off < want:\n"
        "    try:\n"
        "        n=os.copy_file_range(s, d, min(want-off, 1<<20), offset_src=off, offset_dst=off)\n"
        "    except OSError as e:\n"
        "        print('copy_file_range refused', e); break\n"
        "    if not n: print('short copy at', off); break\n"
        "    off += n\n"
        "print('copied', off, 'size', os.path.getsize('/home/user/t.bin'))\""
    ),
    "sendfile": (
        "python3 -c \"import os\n"
        "s=os.open('/home/user/fill.bin', os.O_RDONLY)\n"
        "d=os.open('/home/user/t.bin', os.O_CREAT|os.O_WRONLY, 0o644)\n"
        "want={target}*1024*1024; off=0\n"
        "while off < want:\n"
        "    try:\n"
        "        n=os.sendfile(d, s, off, min(want-off, 1<<20))\n"
        "    except OSError as e:\n"
        "        print('sendfile refused', e); break\n"
        "    if not n: print('short copy at', off); break\n"
        "    off += n\n"
        "print('sent', off, 'size', os.path.getsize('/home/user/t.bin'))\""
    ),
    "splice/tee via pipe": (
        "python3 -c \"import os\n"
        "s=os.open('/home/user/fill.bin', os.O_RDONLY)\n"
        "d=os.open('/home/user/t.bin', os.O_CREAT|os.O_WRONLY, 0o644)\n"
        "r,w=os.pipe()\n"
        "want={target}*1024*1024; off=0\n"
        "while off < want:\n"
        "    chunk=min(want-off, 1<<20)\n"
        "    try:\n"
        "        moved=os.splice(s, w, chunk, offset_src=off)\n"
        "        if moved: written=os.splice(r, d, moved)\n"
        "        else: written=0\n"
        "    except OSError as e:\n"
        "        print('splice refused', e); break\n"
        "    if not moved: print('short splice at', off); break\n"
        "    off += written\n"
        "print('spliced', off, 'size', os.path.getsize('/home/user/t.bin'))\""
    ),
}


def used(sb) -> int:
    m = sb.get_metrics()
    m = m[0] if isinstance(m, list) else m
    return int(m.disk_used)


def main() -> int:
    for name, program in PROGRAMS.items():
        sb = Sandbox.create(timeout=1800)
        try:
            sb.commands.run(
                f"dd if=/dev/zero of=/home/user/fill.bin bs=1M count={FILL_MIB} status=none",
                timeout=900,
            )
            time.sleep(8)
            try:
                out = sb.commands.run(program.format(target=TARGET_MIB), timeout=900)
            except Exception as exc:  # noqa: BLE001 - a failure is data
                print(f"--- {name}: command failed: {str(exc)[:200]}", flush=True)
                continue
            time.sleep(6)
            lines = [line for line in out.stdout.splitlines() if line.strip()]
            print(f"--- {name}: rc={out.exit_code}", flush=True)
            for line in lines[-3:]:
                print(f"    {line.strip()[:120]}", flush=True)
            if out.stderr.strip():
                print(f"    stderr: {out.stderr.strip()[:200]}", flush=True)
            print(f"    platform used: {used(sb) / MIB:.1f} MiB", flush=True)
        finally:
            sb.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
