"""N31 fix 2 acceptance: the platform number charges what a tree allocates.

The number the platform reports for a sandbox is the worker's
`priv_helpers.dir_size` -- the same quantity `DirLedger` maintains and, since
this round, **every file's size plus every directory's allocated size**
(`st_blocks * 512`).  N31 measured 2000 empty entries moving the platform
number by 0 bytes; the space such a tree holds is the directories' blocks.

Two independent measurements, in the same probe:

* **inside the sandbox** with `os.walk` + `os.stat` (never the worker's code),
  spelled to the same definition;
* the platform's `sb.get_metrics().disk_used`.

`du -s -B1` (allocated blocks, the number an operator compares against) is
printed next to both: with the directory term charged as allocation, the
platform's number and `du` are the same quantity.
"""

import time

from e2b import Sandbox

#: Every file's size plus every directory's **allocated** size.  Single quotes
#: around the payload, so nothing needs escaping through the exec channel.
TREE_SIZE = """python3 -c 'import os
t="/home/user"
tot=0
for r,_d,fs in os.walk(t):
    tot += os.stat(r).st_blocks * 512
    tot += sum(os.path.getsize(os.path.join(r,f)) for f in fs)
print(tot)'
"""

#: The old definition, for contrast in the log.
FILE_ONLY = """python3 -c 'import os
t="/home/user"
print(sum(os.path.getsize(os.path.join(r,f)) for r,_d,fs in os.walk(t) for f in fs))'
"""

#: 40 directories (each a real directory block) plus one file, and a second
#: tree that is *only* directories -- the N31 shape that used to be invisible
#: end to end.
MUTATE = r"""
python3 - <<'PY'
import os
root = "/home/user"
os.makedirs(f"{root}/tree/one/deep", exist_ok=True)
for i in range(40):
    os.makedirs(f"{root}/tree/one/deep/d{i:02d}", exist_ok=True)
with open(f"{root}/tree/one/deep/payload.bin", "wb") as fh:
    fh.write(b"p" * 4096)
os.makedirs(f"{root}/empty/only/dirs", exist_ok=True)
for i in range(10):
    os.makedirs(f"{root}/empty/only/dirs/e{i:02d}", exist_ok=True)
PY
"""


def measure(sb) -> int:
    return int(sb.commands.run(TREE_SIZE, timeout=120).stdout.strip())


def file_only(sb) -> int:
    return int(sb.commands.run(FILE_ONLY, timeout=120).stdout.strip())


def du_bytes(sb) -> int:
    out = sb.commands.run("du -s -B1 /home/user | cut -f1", timeout=120)
    return int(out.stdout.strip())


def wait_for_report(sb, expected: int, deadline_s: float = 45.0) -> int:
    deadline = time.monotonic() + deadline_s
    last = -1
    while time.monotonic() < deadline:
        metric = sb.get_metrics()
        metric = metric[0] if isinstance(metric, list) else metric
        last = int(metric.disk_used)
        if last == expected:
            break
        time.sleep(2)
    return last


sb = Sandbox.create(timeout=900)
try:
    time.sleep(8)  # let the worker bank a baseline
    sb.commands.run(MUTATE, timeout=300)

    truth = measure(sb)
    files = file_only(sb)
    du = du_bytes(sb)
    print(f"inside the sandbox: files+dir-blocks={truth} file-only={files} du={du}")
    print(f"  directory allocation the old definition missed: {truth - files} bytes")

    got = wait_for_report(sb, truth)
    print(f"reported by the platform: {got}")
    if got != truth:
        print(f"MISMATCH: off by {got - truth} bytes")
        raise SystemExit(1)
    print(f"platform == sandbox measurement, byte for byte ({truth})")
    print(f"platform vs du -s -B1: {got} vs {du} (diff {got - du})")
finally:
    sb.kill()
