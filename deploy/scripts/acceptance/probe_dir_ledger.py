"""N25/L2c acceptance: the incremental ledger must equal a measurement made
*inside* the sandbox, byte for byte, after a mutation sequence.

The worker's `diskUsed` now comes from the mediator's dirty set instead of a
whole-tree walk, and the whole point of the change is that the two agree. An
independent measurement is taken where the bytes live -- inside the sandbox,
summing `os.path.getsize` over the tree, which is the same quantity
`priv_helpers.dir_size` computes -- so this cannot pass by comparing a
subsystem with itself.

The sequence is chosen to hit each kind of mark the mediator can make: new
files at several depths, a new directory, a rename, a whole-branch removal,
and an append to a file held open (the one shape no path syscall follows).
"""

import os
import time

from e2b import Sandbox

MIB = 1024 * 1024

TREE_SIZE = (
    "python3 -c \"import os,sys;t='/home/user';"
    "print(sum(os.path.getsize(os.path.join(r,f)) "
    "for r,_d,fs in os.walk(t) for f in fs))\""
)

MUTATE = r"""
python3 - <<'PY'
import os, shutil
root = "/home/user"
os.makedirs(f"{root}/a/b/c", exist_ok=True)
for i in range(50):
    with open(f"{root}/a/b/c/f{i}.bin", "wb") as fh:
        fh.write(b"x" * (i * 7 + 1))
with open(f"{root}/a/top.txt", "wb") as fh:
    fh.write(b"t" * 4096)
os.makedirs(f"{root}/later/deep", exist_ok=True)
with open(f"{root}/later/deep/n.bin", "wb") as fh:
    fh.write(b"n" * 2048)
os.rename(f"{root}/a/top.txt", f"{root}/later/moved.txt")
shutil.rmtree(f"{root}/a/b/c")
PY
"""

sb = Sandbox.create(timeout=900)
try:
    # 1. A baseline the worker has certainly seen by now.
    time.sleep(8)
    sb.commands.run(MUTATE, timeout=300)
    truth = int(sb.commands.run(TREE_SIZE, timeout=120).stdout.strip())
    print(f"measured inside the sandbox: {truth} bytes")

    # 2. The worker's next round + the heartbeat that carries it.
    reported = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        metric = sb.get_metrics()
        metric = metric[0] if isinstance(metric, list) else metric
        reported = int(metric.disk_used)
        if reported == truth:
            break
        time.sleep(2)

    print(f"reported by the platform:     {reported} bytes")
    if reported != truth:
        print(f"MISMATCH: off by {reported - truth} bytes")
        raise SystemExit(1)

    # 3. An append to an open descriptor: no path syscall follows the first
    #    `open`, so this is the case the ledger can only catch by re-checking
    #    a directory whose subtree grew.
    sb.commands.run(
        "python3 -c \"import time;"
        "f=open('/home/user/later/deep/append.log','wb');"
        "f.write(b'a'*1000); f.flush(); time.sleep(12); "
        "f.write(b'b'*5000); f.close()\"",
        timeout=300,
    )
    truth = int(sb.commands.run(TREE_SIZE, timeout=120).stdout.strip())
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        metric = sb.get_metrics()
        metric = metric[0] if isinstance(metric, list) else metric
        reported = int(metric.disk_used)
        if reported == truth:
            break
        time.sleep(2)
    print(f"after an append-only writer:  measured={truth} reported={reported}")
    if reported != truth:
        print(f"MISMATCH: off by {reported - truth} bytes")
        raise SystemExit(1)

    print("ledger == sandbox measurement, byte for byte")
finally:
    sb.kill()
