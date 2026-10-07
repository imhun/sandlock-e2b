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

    --op 取 openclose | close | mmap | clone | clone3 | getdents | uname | chdir | stat | getpid
    （`stat` 是 N81 之后的对照：应当 0 停顿、几十万 op/s；`getpid` 是纯循环对照。）

`clone` 是 N83 Phase 2 Task 4 加进来的那一支（R7）：没有它，退通知表就只量得到 mmap
那一半。它 run 的是**热 fork 循环**的形状 —— `fork()` + `waitpid()` 一次。两族在
Task 4 里**都没有退**（理由见 `seccomp_plan.rs`）。

**`clone3` 是 N86 补的另一半**：`os.fork()` 走的是老 `clone`（glibc 的 `fork` 不用
`clone3`），而 R13 保住整族的理由恰恰是 `clone3`（`clone_args` 在用户指针后、cBPF 读不到）。
裸 `clone3(flags=0, exit_signal=SIGCHLD)` 的子进程是 **clone child** —— `wait4(flags=0)`
答 `ECHILD`，必须带 `__WCLONE` 才收得到（形状与三处对照见
`deploy/scripts/acceptance/probe_clone3_wait_shape.py`）—— 所以这一支的 `one()` 里
reap 用的是 `wait4(..., __WCLONE)`，不是 `os.waitpid()`。
"""

from __future__ import annotations

import argparse
import sys
from textwrap import dedent

from e2b import Sandbox

INNER = dedent(
    """
    import ctypes
    import mmap as mmap_mod
    import os
    import time

    OP = os.environ.get("N82_OP", "openclose")
    PATH = "/etc/os-release"
    DENT = "/usr/lib/python3.11" if os.path.isdir("/usr/lib/python3.11") else "/etc"

    # clone3 = 435 on x86_64 and aarch64; a bare clone3 child is a *clone child*,
    # so its reap needs __WCLONE (N86).
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
    _LIBC.syscall.restype = ctypes.c_long
    _LIBC.wait4.restype = ctypes.c_long
    _LIBC.wait4.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _SYS_CLONE3 = 435
    _SIGCHLD = 17
    _WCLONE = -2147483648


    class _CloneArgs(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "flags", "exit_signal", "stack", "stack_size", "tls",
                "set_tid", "set_tid_size", "cgroup",
                "last_tid", "last_tid_size", "padding",
            )
        ]


    def _clone3_once():
        args = _CloneArgs(flags=0, exit_signal=_SIGCHLD)
        ctypes.set_errno(0)
        rc = _LIBC.syscall(_SYS_CLONE3, ctypes.byref(args), ctypes.c_size_t(64))
        if rc == 0:
            os._exit(0)
        if rc > 0:
            _LIBC.wait4(rc, None, _WCLONE, None)

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
        elif OP == "clone":
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
        elif OP == "clone3":
            _clone3_once()
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
