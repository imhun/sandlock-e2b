"""Does the seccomp gate actually refuse a raise, below CPython's own check?

`resource.setrlimit` refuses a raised *hard* limit in user space (ValueError),
which hides whether the kernel -- or our gate -- was involved at all. This
goes under it: ctypes straight to `setrlimit`/`prlimit64`, and reports the
raw return value and errno.

Expected with the sandbox near its budget (soft < hard):

  * setrlimit(soft=hard)   -> -1, EPERM     (the gate)
  * prlimit64(soft=hard)   -> -1, EPERM     (the gate)
  * setrlimit(hard=1 TiB)  -> -1, EPERM     (the gate)
  * setrlimit(soft=cur+64K)-> -1, EPERM     (a raise is a raise)
  * setrlimit(soft=cur/2)  ->  0            (lowering stays legal)
"""

import base64
import os
import sys
import time

from e2b import Sandbox

PROBE = r'''
import ctypes
import errno
import resource

libc = ctypes.CDLL("libc.so.6", use_errno=True)


class RLimit(ctypes.Structure):
    _fields_ = [("rlim_cur", ctypes.c_ulonglong), ("rlim_max", ctypes.c_ulonglong)]


RES = resource.RLIMIT_FSIZE


def call(label, fn):
    ctypes.set_errno(0)
    rc = fn()
    err = ctypes.get_errno()
    name = errno.errorcode.get(err, str(err))
    print(f"{label}: rc={rc} errno={name}")


cur, hard = resource.getrlimit(RES)
print(f"before cur={cur} hard={hard}")

call(
    "setrlimit soft=hard",
    lambda: libc.setrlimit(RES, ctypes.byref(RLimit(hard, hard))),
)
call(
    "prlimit64 soft=hard",
    lambda: libc.prlimit64(0, RES, ctypes.byref(RLimit(hard, hard)), None),
)
call(
    "setrlimit max=1TiB",
    lambda: libc.setrlimit(RES, ctypes.byref(RLimit(hard, 1 << 40))),
)
call(
    "setrlimit soft=cur+64K",
    lambda: libc.setrlimit(RES, ctypes.byref(RLimit(cur + (1 << 16), hard))),
)
call(
    "setrlimit soft=cur/2",
    lambda: libc.setrlimit(RES, ctypes.byref(RLimit(max(1, cur // 2), hard))),
)
cur2, hard2 = resource.getrlimit(RES)
print(f"after cur={cur2} hard={hard2}")

with open("/home/user/ctypes.bin", "wb") as fh:
    fh.write(b"x" * (1 << 20))
print("write ok", __import__("os").path.getsize("/home/user/ctypes.bin"))
'''


def main() -> int:
    sb = Sandbox.create(timeout=600)
    print("sandbox", sb.sandbox_id, flush=True)
    fill = sb.commands.run(
        "dd if=/dev/zero of=/home/user/fill.bin bs=1M count=1000 status=none"
    )
    print("fill rc", fill.exit_code, flush=True)
    time.sleep(4)
    payload = base64.b64encode(PROBE.encode()).decode()
    out = sb.commands.run(
        f"python3 -c \"import base64;exec(base64.b64decode('{payload}'))\"",
        timeout=120,
    )
    print("rc", out.exit_code)
    print(out.stdout.strip())
    if out.stderr.strip():
        print("stderr:", out.stderr.strip())
    sb.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
