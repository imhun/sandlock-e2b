"""N80 Phase 0：一次 `clone3` 能不能把 leader 直接生进 userns+pidns（+mount/netns）。

今天建箱是 A `fork` → B（单线程）`unshare(NEWUSER)` + map + `unshare(NEWPID)` + `fork` → C。
B 存在的唯一理由是 `unshare(CLONE_NEWUSER)` 要求调用者不是多线程，而 A 是多线程。

本探针验证替代路线：A 直接 `clone3` 带多个 `CLONE_NEW*` 位。四条 arm，各起一个独立
python3 进程（clone 不可逆，必须独立）；子进程 sleep 住让父进程读
`/proc/<pid>/status` 的 `NSpid:` 与 `/proc/<pid>/stat` 的宿主 PPid，再 kill + wait。

必须在 worker 容器里跑（真实 seccomp profile + 真实内核 + arm64）：

    deploy/scripts/open-cluster-tunnel.sh
    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    kubectl -n sandlock exec e2b-worker-0 -- python3 - \
        < deploy/scripts/acceptance/probe_clone3_shape.py

判据：A2（多线程 clone3 双 ns 位）是 Task 1 的 go/no-go；A4（再加 NEWNS|NEWNET）供 Task 2；
A3 是老 `clone` 备选（`unshare` 的“调用者不能多线程”限制不属于 `clone`）。
"""

import os
import subprocess
import sys
import textwrap

CHILD = textwrap.dedent(
    """
    import ctypes, errno, os, struct, threading, time

    NEWUSER, NEWPID, NEWNS, NEWNET = 0x10000000, 0x20000000, 0x00020000, 0x40000000
    SIGCHLD = 17

    def nr_of(name):
        machine = os.uname().machine
        if machine in ("aarch64", "arm64"):
            return {"clone3": 435, "clone": 220}[name]
        return {"clone3": 435, "clone": 56}[name]

    libc = ctypes.CDLL(None, use_errno=True)

    ARM = ARM_PLACEHOLDER
    if ARM == "A1":
        flags, which, threads = NEWUSER | NEWPID, "clone3", 0
    elif ARM == "A2":
        flags, which, threads = NEWUSER | NEWPID, "clone3", 1
    elif ARM == "A3":
        # 老 clone 把 exit_signal 放在 flags 的低 8 位；传 0 会让内核把子进程
        # 当成“不可 wait”（waitpid -> ECHILD）。clone3 正是把它拆成独立字段。
        flags, which, threads = NEWUSER | NEWPID | SIGCHLD, "clone", 1
    elif ARM == "A4":
        flags, which, threads = NEWUSER | NEWPID | NEWNS | NEWNET, "clone3", 1
    else:
        raise SystemExit("bad arm")

    if threads:
        threading.Thread(target=lambda: time.sleep(30), daemon=True).start()
        time.sleep(0.3)

    ctypes.set_errno(0)
    if which == "clone3":
        # struct clone_args: flags, pidfd, child_tid, parent_tid, exit_signal,
        # stack, stack_size, tls, set_tid, set_tid_size, cgroup
        args = struct.pack("<11Q", flags, 0, 0, 0, SIGCHLD, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(args, len(args))
        pid = libc.syscall(nr_of("clone3"), ctypes.byref(buf), len(args))
    else:
        # arm64 的 clone 参数顺序是 (flags, stack, parent_tid, tls, child_tid)，
        # 这里后四个全传 0，顺序不影响结果。
        pid = libc.syscall(nr_of("clone"), flags, 0, 0, 0, 0)
    err = ctypes.get_errno()

    if pid == 0:
        # 子进程：留在新 ns 里等父进程读 /proc，然后被 kill。
        time.sleep(30)
        os._exit(0)

    if pid < 0:
        print("%s: FAIL %s errno=%d (%s) threads=%d"
              % (ARM, which, err, errno.errorcode.get(err, "?"),
                 len(os.listdir("/proc/self/task"))))
        raise SystemExit(0)

    time.sleep(0.3)
    nspid = "?"
    try:
        with open("/proc/%d/status" % pid) as fh:
            for line in fh:
                if line.startswith("NSpid:"):
                    nspid = line.split(":", 1)[1].strip()
    except OSError as exc:
        nspid = "read-error:%s" % exc
    host_ppid = "?"
    try:
        with open("/proc/%d/stat" % pid) as fh:
            stat = fh.read()
        host_ppid = stat.rsplit(")", 1)[1].split()[1]
    except OSError as exc:
        host_ppid = "read-error:%s" % exc

    os.kill(pid, 9)
    os.waitpid(pid, 0)
    print("%s: OK %s pid=%d NSpid=[%s] hostPPid=%s threads=%d"
          % (ARM, which, pid, nspid, host_ppid, len(os.listdir("/proc/self/task"))))
    """
)


def run(arm: str) -> str:
    code = CHILD.replace("ARM_PLACEHOLDER", repr(arm))
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    out = (proc.stdout or "").strip()
    if proc.stderr.strip():
        out += "\n  stderr: " + proc.stderr.strip()[:400]
    return out or "%s: (no output, rc=%d)" % (arm, proc.returncode)


def main() -> int:
    print("machine=%s uid=%d" % (os.uname().machine, os.getuid()))
    for arm in ("A1", "A2", "A3", "A4"):
        print(run(arm))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
