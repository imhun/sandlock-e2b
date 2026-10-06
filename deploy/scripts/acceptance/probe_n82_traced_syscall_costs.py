#!/usr/bin/env python3
"""N82 取证：通知表里**除 stat 之外**哪些 syscall 会撞 5000/s 限流。

限流器（`seccomp/notif.rs` 的 `WindowBudget`，worker 侧 `E2B_SANDBOX_NOTIFY_RATE_LIMIT`，
默认 5000/s）数的是**通知条数**，不是"类"。所以只要还在通知表里 —— 包括无条件进表的
`openat`/`openat2`/`getdents64` 与 `NETLINK_NOTIF_SYSCALLS`（`socket`/`bind`/`getsockname`/
`recvfrom`/`recvmsg`/**`close`**）—— 就会在超过 5000 条/s 之后每秒被睡掉约 0.86 s。

形状照 `stat_stall_hunt.py`：每轮 2000 次「getpid + 目标 op」，记单次最坏与轮均值；
`STALL` 行即"单次 > 20 ms"，也就是撞上了睡满窗口。

用法：
    export E2B_API_URL=http://<入口>:3000 E2B_SANDBOX_URL=http://<入口>:3000
    export E2B_API_KEY=...
    python deploy/scripts/acceptance/probe_n82_traced_syscall_costs.py --op openclose --seconds 40

    --op 取 openclose | close | mmap | getdents | uname | chdir | stat | getpid
    （`stat` 是 N81 之后的对照：应当 0 停顿、几十万 op/s；`getpid` 是纯循环对照。）
"""

from __future__ import annotations

import argparse
import sys
from textwrap import dedent

from e2b import Sandbox

INNER = dedent(
    """
    import mmap as mmap_mod
    import os
    import time

    OP = os.environ.get("N82_OP", "openclose")
    PATH = "/etc/os-release"
    DENT = "/usr/lib/python3.11" if os.path.isdir("/usr/lib/python3.11") else "/etc"

    def one():
        if OP == "openclose":
            fd = os.open(PATH, os.O_RDONLY)
            os.close(fd)
        elif OP == "close":
            fds = [os.open(PATH, os.O_RDONLY) for _ in range(1000)]
            for fd in fds:
                os.close(fd)
        elif OP == "mmap":
            m = mmap_mod.mmap(-1, 4096)
            m.close()
        elif OP == "getdents":
            os.listdir(DENT)
        elif OP == "uname":
            os.uname()
        elif OP == "chdir":
            os.chdir("/")
            os.chdir(DENT)
        elif OP == "stat":
            os.stat(PATH)
        elif OP == "getpid":
            pass
        else:
            raise SystemExit("unknown op " + OP)

    one()  # warm
    started = time.time()
    rounds = stalls = 0
    while time.time() - started < float(os.environ.get("N82_SECONDS", "70")):
        worst = 0.0
        worst_at = 0.0
        t0 = time.perf_counter()
        for _ in range(2000):
            t = time.perf_counter()
            os.getpid()
            t = time.perf_counter()
            one()
            s = (time.perf_counter() - t) * 1e6
            if s > worst:
                worst, worst_at = s, time.time()
        mean = (time.perf_counter() - t0) / 2000 * 1e6
        if worst > 20000:
            stalls += 1
            print(f"STALL wall={worst_at:.3f} op_us={worst:.0f} mean_us={mean:.1f} round={rounds}",
                  flush=True)
        rounds += 1
    elapsed = time.time() - started
    print(f"DONE op={OP} stalls={stalls} rounds={rounds} elapsed_s={elapsed:.1f} "
          f"ops_per_s={rounds * 2000 / elapsed:.0f}")
    """
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--op", default="openclose")
    parser.add_argument("--seconds", default="70")
    args = parser.parse_args()

    box = Sandbox.create()
    try:
        out = box.commands.run(
            "cat > hunt.py <<'PYEOF'\n" + INNER + "PYEOF\n"
            f"N82_OP={args.op} N82_SECONDS={args.seconds} python3 hunt.py",
            timeout=int(args.seconds) + 120,
        )
        print(((out.stdout or "") + (out.stderr or "")).strip())
        return 0
    finally:
        box.kill()


if __name__ == "__main__":
    sys.exit(main())
