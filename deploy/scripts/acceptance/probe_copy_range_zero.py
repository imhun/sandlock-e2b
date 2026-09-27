"""`copy_file_range` reported 300 MiB moved while the file stayed 4096 bytes.

Three explanations, and only one of them is a quota hole:

  (a) the copy did nothing and the count was wrong,
  (b) the copy happened, but this client's cached attributes are stale,
  (c) the NFS client handed the copy to the server (NFS 4.2 COPY), which does
      not see RLIMIT_FSIZE at all -- a genuine way past the ceiling.

Distinguishing them: after one 1 MiB copy, look at the size, the *blocks*
(`du`, which reflects real data), and the same numbers again after the
attribute cache would have expired.
"""

import sys

from e2b import Sandbox

PROBE = r'''
import os
import time

FILL = "/home/user/fill.bin"
DEST = "/home/user/dest.bin"


def look(label):
    st = os.stat(DEST)
    print(f"  {label:26s} size={st.st_size} blocks={st.st_blocks * 512}", flush=True)


look("before")

s = os.open(FILL, os.O_RDONLY)
d = os.open(DEST, os.O_WRONLY)
try:
    n = os.copy_file_range(s, d, 1 << 20, offset_src=0, offset_dst=0)
    print("  one copy returned", n, flush=True)
except OSError as exc:
    print("  one copy refused", exc, flush=True)
finally:
    os.close(s)
    os.close(d)

look("immediately after")
time.sleep(5)
look("after 5 s")
time.sleep(20)
look("after 25 s")

# Same copy through the file offsets (no explicit offsets), which is the shape
# a `cp`-like tool uses.
d = os.open(DEST, os.O_WRONLY)
s = os.open(FILL, os.O_RDONLY)
os.lseek(d, 0, os.SEEK_SET)
try:
    n = os.copy_file_range(s, d, 1 << 20)
    print("  offset copy returned", n, flush=True)
except OSError as exc:
    print("  offset copy refused", exc, flush=True)
finally:
    os.close(s)
    os.close(d)
look("after the offset copy")
'''


def main() -> int:
    sb = Sandbox.create(timeout=1800)
    print("sandbox", sb.sandbox_id, flush=True)
    try:
        sb.commands.run(
            "cd /home/user && head -c 4096 /dev/zero > dest.bin && "
            "dd if=/dev/zero of=fill.bin bs=1M count=900 status=none && "
            "dd if=/dev/zero of=rest.bin bs=1M count=1024 status=none || true",
            timeout=900,
        )
        for line in sb.commands.run(
            "df -h /home/user | tail -1; stat -c 'dest=%s fill=%s rest=%s' "
            "/home/user/dest.bin /home/user/fill.bin /home/user/rest.bin",
            timeout=120,
        ).stdout.strip().splitlines():
            print("  ", line.strip(), flush=True)
        out = sb.commands.run(f"python3 - <<'PY'\n{PROBE}\nPY", timeout=600)
        print(out.stdout.rstrip())
        if out.stderr.strip():
            print("  stderr:", out.stderr.strip()[:200], flush=True)
        m = sb.get_metrics()
        m = m[0] if isinstance(m, list) else m
        print(f"  platform used: {int(m.disk_used) / 1024 / 1024:.1f} MiB", flush=True)
    finally:
        sb.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
