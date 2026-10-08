"""N80 收尾：own identity 的 slot 能不能用 clone3 建 userns，然后立刻 exec 出干净进程。

要验证的链（改后的 `identity_grant.spawn_child` 会走的形状）：

    worker（多线程）clone3(CLONE_NEWUSER) → 子进程里一行 os.execvpe(小 python)
      → exec 之后是干净进程（锁状态已随 exec 消失） → 它在新 userns 里
      → 写自己的 map → setresuid(0) 成功

真实路径里 map 由 agent face A 写（`as_uid`），这里用非特权自映射替代 —— 那一段没变，
本探针只回答“clone3 + 立刻 exec 这条链成不成立”。

**它答对了内核那一问，但不要把它当成端到端证据。** 2026-10-06 的现场是：本探针 C 臂全绿，
而真实 `spawn_child` 每次建箱必失败（`as_uid: refused: cannot write uid_map … Permission
denied`）—— 因为那条路径上有一个与内核无关的 Python 缺陷（`spawn_child` 的形参 `timeout_s`
遮蔽同名模块函数 ⇒ 子进程在第一次 `setresuid` 之前就 `os._exit(4)`，agent 于是打一个已死的
pid，僵尸的 id-map 文件属主是 root ⇒ EACCES）。要验真实路径请直接驱动
`envd_service.identity_grant.spawn_child`（`tmp/n80/spawn_child_probe.py` 是那种探针）。

必须在 worker 容器里跑（真实 seccomp 档 + 真实线程形态）：

    deploy/scripts/open-cluster-tunnel.sh
    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    kubectl -n sandlock exec -i e2b-worker-0 -- python3 - \
        < deploy/scripts/acceptance/probe_slot_clone3_shape.py
"""

import subprocess
import sys
import textwrap

CHILD = textwrap.dedent(
    """
    import os
    print("CHILD pre uid=%d euid=%d userns=%s"
          % (os.getuid(), os.geteuid(), os.readlink("/proc/self/ns/user")))
    print("CHILD pre uid_map=%r" % open("/proc/self/uid_map").read())
    for path, text in (
        ("/proc/self/uid_map", "0 %d 1\\n" % REAL_UID),
        ("/proc/self/setgroups", "deny\\n"),
        ("/proc/self/gid_map", "0 %d 1\\n" % REAL_GID),
    ):
        try:
            fd = os.open(path, os.O_WRONLY)
            os.write(fd, text.encode())
            os.close(fd)
            print("CHILD wrote %s" % path)
        except OSError as exc:
            print("CHILD write %s failed: errno=%d %s" % (path, exc.errno, exc.strerror))
    try:
        os.setresgid(0, 0, 0)
        os.setresuid(0, 0, 0)
        print("CHILD OK uid=%d" % os.getuid())
    except OSError as exc:
        print("CHILD setresuid failed: errno=%d %s" % (exc.errno, exc.strerror))
    """
)

PARENT = textwrap.dedent(
    """
    import ctypes, os, struct, sys, threading, time

    CLONE_NEWUSER, SIGCHLD, SYS_CLONE3 = 0x10000000, 17, 435
    real_uid, real_gid = os.getuid(), os.getgid()

    # The worker is multi-threaded; so is this probe before it clones.
    threading.Thread(target=lambda: time.sleep(30), daemon=True).start()
    time.sleep(0.3)

    child_code = CHILD_SOURCE.replace("REAL_UID", str(real_uid)).replace(
        "REAL_GID", str(real_gid)
    )

    libc = ctypes.CDLL(None, use_errno=True)

    def clone():
        args = struct.pack("<11Q", CLONE_NEWUSER, 0, 0, 0, SIGCHLD, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(args, len(args))
        ctypes.set_errno(0)
        pid = libc.syscall(SYS_CLONE3, ctypes.byref(buf), len(args))
        return pid, ctypes.get_errno()


    def arm(label, child):
        pid, err = clone()
        if pid < 0:
            print("%s: FAIL clone3 errno=%d (%s)" % (label, err, os.strerror(err)))
            return
        if pid == 0:
            child()
            os._exit(9)
        os.waitpid(pid, 0)


    def write_map_here():
        # In the namespace WITHOUT an exec: this is the shape the engine uses
        # today (sandbox.rs writes its own uid_map right after clone3).
        try:
            fd = os.open("/proc/self/uid_map", os.O_WRONLY)
            os.write(fd, b"0 %d 1\\n" % real_uid)
            os.close(fd)
            print("A no-exec: write uid_map OK", flush=True)
        except OSError as exc:
            print(
                "A no-exec: write uid_map errno=%d %s" % (exc.errno, exc.strerror),
                flush=True,
            )
        os._exit(0)


    arm("A no-exec", write_map_here)

    def exec_then_write():
        os.execvpe(sys.executable, [sys.executable, "-c", child_code], os.environ)
        os._exit(9)


    arm("B exec-first", exec_then_write)

    def wait_then_exec():
        # Arm C: the shape the slot would actually take -- clone3 puts us in the
        # namespace, this process does NOT exec, it polls for the identity the
        # parent is writing, then execs. Caps are still ours (arm A proved it),
        # so setresuid works once the map lands.
        import time

        deadline = time.monotonic() + 10
        while True:
            try:
                os.setresgid(real_uid, real_uid, real_uid)
                os.setresuid(real_uid, real_uid, real_uid)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    print("C wait-then-exec: identity never landed", flush=True)
                    os._exit(4)
                time.sleep(0.05)
        print(
            "C wait-then-exec: identity landed uid=%d, execing" % os.getuid(),
            flush=True,
        )
        os.execvpe(
            sys.executable,
            [sys.executable, "-c", "import os; print('C POST-EXEC uid=%d' % os.getuid())"],
            os.environ,
        )
        os._exit(9)


    # Arm C needs the parent to write the child's maps (the agent's job in the
    # real path: `as_uid` writes `<uid> <uid> 1`). The worker is the userns
    # owner, so writing its own ids is allowed -- same rule `as_uid` relies on.
    pid, err = clone()
    if pid < 0:
        print("C wait-then-exec: FAIL clone3 errno=%d (%s)" % (err, os.strerror(err)))
    elif pid == 0:
        wait_then_exec()
    else:
        try:
            with open("/proc/%d/setgroups" % pid, "w") as fh:
                fh.write("deny")
            with open("/proc/%d/uid_map" % pid, "w") as fh:
                fh.write("%d %d 1\\n" % (real_uid, real_uid))
            with open("/proc/%d/gid_map" % pid, "w") as fh:
                fh.write("%d %d 1\\n" % (real_gid, real_gid))
            print("C parent: maps written for pid=%d" % pid, flush=True)
        except OSError as exc:
            print(
                "C parent: map write errno=%d %s" % (exc.errno, exc.strerror),
                flush=True,
            )
        os.waitpid(pid, 0)
    """
)


def main() -> int:
    code = PARENT.replace("CHILD_SOURCE", repr(CHILD))
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    print("machine=%s uid=%d" % (__import__("os").uname().machine, __import__("os").getuid()))
    print((proc.stdout or "").strip())
    if proc.stderr.strip():
        print("stderr: " + proc.stderr.strip()[:400])
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
