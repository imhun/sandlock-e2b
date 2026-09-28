#!/usr/bin/env python3
"""C3「uid_map 代写」探针：**非父进程、跨 pid namespace** 能不能给一个已 unshare 的子进程
写恒等 uid/gid 映射，从而让 worker 在不持有任何特权的前提下拿到 host uid = X 的进程。

要回答的问题（docs/c3-privilege-relocation.md §14.2.7）：
  今天 `e2b-slot-spawn`（`cap_setuid,cap_setgid+ep`）替 worker 做 `setuid(X)`，
  代价是 **worker 保留一个特权二进制**。替代方案是：

    worker  fork C（65534）→ C `unshare(CLONE_NEWUSER)`        # 无特权
    agent   写 /proc/<C 的 host pid>/uid_map 与 gid_map        # 唯一有特权的一步
    C       setresuid(X) → exec sandlock-supervise             # C 是自己 userns 的创建者

  如果这条成立，**进程树与 cgroup 都留在 worker**（fork 的是 worker），而 worker 零特权。

两个 role 分别跑在**两个 pod** 里（真实拓扑）：

  # ① forker：在 worker 那样的非特权容器里（uid 65534）
  python3 probe_c3_userns_map_handoff.py --role forker --token <TOKEN> --uid 10000 \
      --marker /var/lib/e2b-sandboxes/state/.c3map-probe/<TOKEN>.txt

  # ② agent：在 root + hostPID 的容器里（模拟每节点 agent）
  python3 probe_c3_userns_map_handoff.py --role agent --token <TOKEN> --uid 10000 \
      --map identity          # 或 --map zero （对照臂：写 `0 X 1`）

forker 侧的控制臂（证明这道门是真的）：`--parent-tries-write` —— 让**父进程自己**
（非特权）去写 C 的映射，必须失败。

判据：
  ``C3-MAPHANDOFF-VERDICT=agent-can-map``  非父进程写成了恒等映射，且 C 的 `geteuid()==X`
  ``C3-MAPHANDOFF-VERDICT=blocked``        写被拒（方案不成立）
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

CLONE_NEWUSER = 0x10000000
LIB = ctypes.CDLL(None, use_errno=True)


def _say(marker: str, text: str) -> None:
    print(f"{marker}: {text}", flush=True)


def _set_comm(token: str) -> None:
    """``prctl(PR_SET_NAME)`` — not a /proc write.

    Writing ``/proc/self/comm`` returned EACCES inside the **hostPID** pod
    (measured 2026-09-28), while it worked in the worker pod; ``prctl`` has no
    such check and is the same thing the kernel stores as the comm.
    """
    PR_SET_NAME = 15
    name = token[:15].encode()
    if LIB.prctl(PR_SET_NAME, ctypes.c_char_p(name), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NAME) failed")


def _host_pid_by_nspid(container_pid: int, token: str | None = None) -> int | None:
    """Resolve a *container* pid to a **host** pid via the ``NSpid:`` field.

    This is the生产-grade rendezvous (as opposed to the comm scan): the worker
    reports the pid **it** sees, and the agent -- which mounts the host's /proc
    -- finds the task whose ``NSpid`` line ends with that number. ``token``
    (the comm) disambiguates when several tasks share the innermost pid.
    """
    hits: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        nspid: list[int] | None = None
        for line in status.splitlines():
            if line.startswith("NSpid:"):
                nspid = [int(x) for x in line.split()[1:]]
                break
        if not nspid or len(nspid) < 2 or nspid[-1] != container_pid:
            continue
        if token is not None:
            try:
                if (entry / "comm").read_text(encoding="utf-8").strip() != token:
                    continue
            except OSError:
                continue
        hits.append(int(entry.name))
    return hits[0] if len(hits) == 1 else None


def _cgroup_of(host_pid: int) -> str:
    try:
        return Path(f"/proc/{host_pid}/cgroup").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"<unreadable: {exc}>"


class _CapHdr(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


_CAP_VERSION_3 = 0x20080522
_PR_SET_KEEPCAPS = 8


def _capset(effective: int, permitted: int) -> None:
    hdr = _CapHdr(_CAP_VERSION_3, 0)
    data = (_CapData * 2)()
    data[0].effective = effective & 0xFFFFFFFF
    data[0].permitted = permitted & 0xFFFFFFFF
    data[1].effective = (effective >> 32) & 0xFFFFFFFF
    data[1].permitted = (permitted >> 32) & 0xFFFFFFFF
    if LIB.capset(ctypes.byref(hdr), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "capset failed")


def _read_caps() -> tuple[int, int]:
    """(permitted, effective) from /proc/self/status."""
    perm = eff = 0
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("CapPrm:"):
            perm = int(line.split()[1], 16)
        elif line.startswith("CapEff:"):
            eff = int(line.split()[1], 16)
    return perm, eff


def _drop_to_keep_caps(uid: int) -> None:
    """Become ``uid`` but keep CAP_SETUID/CAP_SETGID effective.

    This is the shape of ``newuidmap`` on the target machine (file-cap
    ``cap_setuid=ep`` at mode 0755, so it runs **as the caller's uid** and
    merely *also* holds CAP_SETUID) -- as opposed to a plain root writer.
    ``prctl(PR_SET_KEEPCAPS)`` keeps the permitted set across ``setresuid``,
    then ``capset`` re-raises it into the effective set.
    """
    if LIB.prctl(_PR_SET_KEEPCAPS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_KEEPCAPS) failed")
    os.setresgid(uid, uid, uid)
    os.setresuid(uid, uid, uid)
    permitted, _ = _read_caps()
    max_uid, max_gid = 1 << 7, 1 << 6  # CAP_SETUID, CAP_SETGID
    _capset(permitted & (max_uid | max_gid), permitted & (max_uid | max_gid))


# --------------------------------------------------------------------- forker


def role_forker(args) -> int:
    if args.drop_uid is not None:
        # Used by the *isolation* arm: run the forker as an unprivileged uid
        # inside an otherwise-root pod, so the only variable left versus the
        # cross-pod arm is the pid namespace.
        os.setgroups([])
        os.setgid(args.drop_uid)
        os.setuid(args.drop_uid)
    _say("N-forker-identity", f"euid={os.geteuid()} gid={os.getegid()}")

    child_r = None
    child_w = None
    child_r, child_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # ------------------------------------------------------- child
        os.close(child_r)
        try:
            _set_comm(args.token)
            _say("C-comm", args.token)
            if LIB.unshare(CLONE_NEWUSER) != 0:
                err = ctypes.get_errno()
                os.write(child_w, f"C-unshare-failed errno={err}\n".encode())
                os._exit(1)
            _say("C-unshare", f"ok pid_in_this_ns={os.getpid()}")
            # 映射由别人写；写到之前 setresuid 会一直失败。
            deadline = time.monotonic() + args.deadline
            last = None
            while time.monotonic() < deadline:
                if LIB.setresuid(args.uid, args.uid, args.uid) == 0:
                    _say(
                        "C-setresuid",
                        f"ok euid={os.geteuid()} uid={os.getuid()} "
                        f"(inside the namespace)",
                    )
                    marker = Path(args.marker)
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(
                        f"euid={os.geteuid()} token={args.token}\n", encoding="utf-8"
                    )
                    st = marker.stat()
                    _say(
                        "C-marker",
                        f"{marker} written; stat seen from inside: "
                        f"uid={st.st_uid} mode={oct(st.st_mode & 0o7777)}",
                    )
                    os.write(child_w, b"C-done\n")
                    os._exit(0)
                last = ctypes.get_errno()
                time.sleep(0.2)
            _say("C-setresuid", f"TIMEOUT (last errno={last})")
            os.write(child_w, f"C-timeout errno={last}\n".encode())
            os._exit(2)
        except BaseException as exc:  # noqa: BLE001
            os.write(child_w, f"C-exception {type(exc).__name__}: {exc}\n".encode())
            os._exit(3)

    # ------------------------------------------------------------------ parent
    os.close(child_w)
    _say("N-parent", f"forked pid_in_this_ns={pid}")
    if args.pids_file:
        pf = Path(args.pids_file)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(f"{os.getpid()} {pid}\n", encoding="utf-8")
        _say("N-pids-file", f"{pf} <- self={os.getpid()} child={pid} (container pids)")

    if args.parent_tries_write:
        for name, value in (("uid_map", f"{args.uid} {args.uid} 1\n"),
                            ("gid_map", f"{args.uid} {args.uid} 1\n")):
            try:
                with open(f"/proc/{pid}/setgroups", "w") as fh:
                    fh.write("deny")
            except OSError:
                pass
            try:
                Path(f"/proc/{pid}/{name}").write_text(value, encoding="utf-8")
                _say("N-parent-write", f"{name}: unexpectedly OK")
            except OSError as exc:
                _say(
                    "N-parent-write",
                    f"{name}: refused ({type(exc).__name__}: {exc.strerror}) "
                    f"<- the gate is real",
                )

    msg = os.read(child_r, 4096).decode().strip()
    os.close(child_r)
    os.waitpid(pid, 0)
    _say("N-child-final", msg)
    ok = msg == "C-done"
    print(f"C3-MAPHANDOFF-VERDICT={'forker-ok' if ok else 'forker-failed'}", flush=True)
    return 0 if ok else 1


# ---------------------------------------------------------------------- agent


def _host_pid_for_token(token: str) -> int | None:
    """Find the process whose comm is ``token`` **and** whose uid_map is empty.

    Empty ``uid_map`` is the signature of "unshared CLONE_NEWUSER, map not
    written yet" -- it is what makes this the right target and not some other
    process that happens to carry the token.
    """
    hits: list[tuple[int, bool]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if comm != token:
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "uid_map").read_text(encoding="utf-8")
        except OSError:
            continue
        hits.append((pid, raw.strip() == ""))
    unshared = [pid for pid, empty in hits if empty]
    if len(unshared) == 1:
        return unshared[0]
    return None


def role_agent(args) -> int:
    _say("N-agent-identity", f"euid={os.geteuid()} (hostPID container)")
    _say("N-agent-proc", f"/proc/1/comm = {Path('/proc/1/comm').read_text().strip()!r}")
    rendezvous_target: int | None = None
    if args.pids_file:
        raw = Path(args.pids_file).read_text(encoding="utf-8").split()
        worker_cpid, child_cpid = int(raw[0]), int(raw[1])
        worker_hpid = _host_pid_by_nspid(worker_cpid)
        child_hpid = _host_pid_by_nspid(child_cpid, args.token)
        _say(
            "R-nspid",
            f"worker container={worker_cpid} -> host={worker_hpid}; "
            f"child container={child_cpid} -> host={child_hpid}",
        )
        if worker_hpid is not None and child_hpid is not None:
            wcg, ccg = _cgroup_of(worker_hpid), _cgroup_of(child_hpid)
            same = wcg == ccg
            _say("R-cgroup-worker", wcg)
            _say("R-cgroup-child", ccg)
            _say(
                "R-cgroup-verdict",
                f"{'SAME -- (d) preserves cgroup attribution' if same else 'DIFFERENT'}",
            )
            print(
                f"C3-CGROUP={'worker' if same else 'other'}",
                flush=True,
            )
        rendezvous_target = child_hpid
    if args.agent_keep_caps_uid is not None:
        # "agent 与 worker 同 uid 但握着 CAP_SETUID" -- the newuidmap shape.
        # Note the *target* is the worker's natively-65534 (dumpable) child, so
        # its /proc entries are owned by 65534 and this writer can open them.
        _drop_to_keep_caps(args.agent_keep_caps_uid)
        permitted, effective = _read_caps()
        _say(
            "N-agent-shape",
            f"uid={os.getuid()} euid={os.geteuid()} "
            f"CapPrm={permitted:#x} CapEff={effective:#x} (dropped from root, caps kept)",
        )

    if args.pid is not None or rendezvous_target is not None:
        # Same-pid-namespace arm: the forker's reported pid *is* the host pid,
        # so no scan is needed (and the scan was measured unreliable here).
        target = args.pid if args.pid is not None else rendezvous_target
        try:
            comm = Path(f"/proc/{target}/comm").read_text(encoding="utf-8").strip()
            size = len(Path(f"/proc/{target}/uid_map").read_text(encoding="utf-8"))
        except OSError as exc:
            _say("A-target", f"/proc/{target} unreadable: {exc}")
            print("C3-MAPHANDOFF-VERDICT=blocked (target unreadable)", flush=True)
            return 2
        _say(
            "A-target",
            f"explicit pid={target} comm={comm!r} uid_map_bytes={size} "
            f"(0 bytes == unshared, map not written yet)",
        )
        if comm != args.token:
            _say("A-target", f"comm mismatch (want {args.token!r}) -- refusing")
            print("C3-MAPHANDOFF-VERDICT=blocked (wrong target)", flush=True)
            return 2
    else:
        deadline = time.monotonic() + args.deadline
        target = None
        while time.monotonic() < deadline:
            target = _host_pid_for_token(args.token)
            if target is not None:
                break
            time.sleep(0.2)
    if target is None:
        _say("A-target", f"NOT FOUND for token {args.token!r}")
        print("C3-MAPHANDOFF-VERDICT=blocked (target not visible)", flush=True)
        return 2
    _say("A-target", f"host pid={target} (found by comm + empty uid_map)")

    inside = args.uid if args.map == "identity" else 0
    uid_line = f"{inside} {args.uid} 1\n"
    for name, value in (("uid_map", uid_line), ("gid_map", uid_line)):
        try:
            Path(f"/proc/{target}/{name}").write_text(value, encoding="utf-8")
            _say("A-write", f"{name} <- {value.strip()!r}: ok")
        except OSError as exc:
            _say("A-write", f"{name}: FAILED ({type(exc).__name__}: {exc.strerror})")
            print("C3-MAPHANDOFF-VERDICT=blocked (write refused)", flush=True)
            return 1
    _say("A-map", f"map={args.map} (inside {inside} -> host {args.uid})")
    print("C3-MAPHANDOFF-VERDICT=agent-can-map", flush=True)
    return 0


# --------------------------------------------------------------------- matrix


def _unshared_uid_map_is_empty(pid: int) -> bool:
    """``unshare(CLONE_NEWUSER)`` makes the map empty — that IS the signal.

    A process that has *not* unshared reads ``0 0 4294967295`` from its own
    uid_map, so "empty" is an unambiguous "unshared, map not written yet".
    """
    try:
        return Path(f"/proc/{pid}/uid_map").read_text(encoding="utf-8").strip() == ""
    except OSError:
        return False


_PR_SET_DUMPABLE = 4


def _matrix_child(
    uid: int, drop: int | None = None, dumpable: str = "keep"
) -> None:
    """Child body: unshare, then wait for someone else to write the map."""
    try:
        if drop is not None:
            # Bisect variable: the *target's* euid (root vs unprivileged).
            os.setgroups([])
            os.setgid(drop)
            os.setuid(drop)
            try:
                # ``setuid`` to the *same* uid is a no-op and would not clear
                # caps the writer kept on purpose; shed them explicitly.
                _capset(0, 0)
            except OSError:
                pass
        if dumpable in ("0", "1"):
            # Second bisect variable: ``dumpable`` is *separate* from euid --
            # a ``setuid`` transition clears it, and a non-dumpable task's
            # /proc entries are owned by root.
            if LIB.prctl(_PR_SET_DUMPABLE, int(dumpable), 0, 0, 0) != 0:
                os._exit(4)
        try:
            dmp = Path("/proc/self/status").read_text(encoding="utf-8")
            dmp = next(ln for ln in dmp.splitlines() if ln.startswith("CoreDumping")
                       or ln.startswith("Uid:"))
        except Exception:  # noqa: BLE001
            dmp = "?"
        _set_comm("c3map-matrix")
        os.write(
            1,
            f"C-euid={os.geteuid()} dumpable={dumpable} {dmp.strip()}\n".encode(),
        )
        if LIB.unshare(CLONE_NEWUSER) != 0:
            os._exit(1)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if LIB.setresuid(uid, uid, uid) == 0:
                os._exit(0)
            time.sleep(0.1)
        os._exit(2)
    except BaseException:  # noqa: BLE001
        os._exit(3)


def _try_write_map(target: int, uid: int) -> str:
    """Report **where** it fails: ``open`` vs ``write``.

    Those two are different walls in the kernel:
      * ``open`` -> ``proc_pid_permission()`` -> ``ptrace_may_access(task,
        PTRACE_MODE_READ_FSCREDS)``: needs uid-equality with the target **or**
        ``CAP_SYS_PTRACE`` in the target's user namespace;
      * ``write`` -> ``map_write()`` -> ``new_idmap_permitted()``: needs
        ``CAP_SETUID``/``CAP_SETGID`` over the target namespace's parent (or a
        single-entry self-map).
    Splitting them is what turns "EPERM" into an explanation.
    """
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not _unshared_uid_map_is_empty(target):
        time.sleep(0.05)
    line = f"{uid} {uid} 1\n"
    st = os.stat(f"/proc/{target}/uid_map")
    for name in ("uid_map", "gid_map"):
        try:
            fd = os.open(f"/proc/{target}/{name}", os.O_WRONLY)
        except OSError as exc:
            return (
                f"{name}:OPEN:{type(exc).__name__}({exc.strerror}) "
                f"[inode {st.st_uid}:{st.st_gid} {oct(st.st_mode & 0o7777)}]"
            )
        try:
            os.write(fd, line.encode())
        except OSError as exc:
            os.close(fd)
            return f"{name}:WRITE:{type(exc).__name__}({exc.strerror})"
        os.close(fd)
    return "ok"


def _wait_child(pid: int) -> str:
    _, status = os.waitpid(pid, 0)
    return "setresuid-ok" if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 else (
        f"setresuid-failed(exit={os.WEXITSTATUS(status) if os.WIFEXITED(status) else '?'})"
    )


def role_matrix(args) -> int:
    """Who is allowed to write a child's identity map: parent / sibling / grandparent?

    All three run here as **root with CAP_SETUID in the initial user namespace**
    (verified by ``N-agent-identity``), so the only variable is the relationship
    between the writer and the target.
    """
    _say(
        "N-root-writer",
        f"euid={os.geteuid()} uid_map={Path('/proc/self/uid_map').read_text().strip()!r}",
    )
    _say("N-target-drop", str(args.matrix_drop))
    if args.matrix_writer_uid is not None:
        _drop_to_keep_caps(args.matrix_writer_uid)
        permitted, effective = _read_caps()
        _say(
            "N-writer-shape",
            f"uid={os.getuid()} euid={os.geteuid()} "
            f"CapPrm={permitted:#x} CapEff={effective:#x} "
            f"(same-uid writer keeping CAP_SETUID/SETGID -- the newuidmap shape)",
        )
    child_drop = (
        args.matrix_writer_uid if args.matrix_writer_uid is not None else args.matrix_drop
    )
    rows: list[tuple[str, str, str]] = []

    # 1) parent writes
    pid = os.fork()
    if pid == 0:
        _matrix_child(args.uid, child_drop, args.matrix_target_dumpable)
    rows.append(("parent", _try_write_map(pid, args.uid), _wait_child(pid)))

    # 2) sibling writes (a different process in the same process tree)
    pipe_r, pipe_w = os.pipe()
    sib = os.fork()
    if sib == 0:
        os.close(pipe_w)
        target = int(os.read(pipe_r, 32).decode())
        os.write(1, f"S-sibling-write: {_try_write_map(target, args.uid)}\n".encode())
        os._exit(0)
    os.close(pipe_r)
    pid = os.fork()
    if pid == 0:
        _matrix_child(args.uid, child_drop, args.matrix_target_dumpable)
    os.write(pipe_w, str(pid).encode())
    os.close(pipe_w)
    os.waitpid(sib, 0)
    rows.append(("sibling", "(see S-sibling-write)", _wait_child(pid)))

    # 3) grandparent writes
    pipe_r, pipe_w = os.pipe()
    gp = os.fork()
    if gp == 0:
        os.close(pipe_r)
        cpid = os.fork()
        if cpid == 0:
            _matrix_child(args.uid, child_drop, args.matrix_target_dumpable)
        os.write(pipe_w, str(cpid).encode())
        os.close(pipe_w)
        os.waitpid(cpid, 0)
        os._exit(0)
    os.close(pipe_w)
    target = int(os.read(pipe_r, 32).decode())
    os.close(pipe_r)
    rows.append(("grandparent", _try_write_map(target, args.uid), "(see M rows)"))
    os.waitpid(gp, 0)

    _say("M-uid", str(args.uid))
    for who, write, outcome in rows:
        _say(f"M-{who}", f"write={write} child={outcome}")
    verdict = "parent-only" if rows[0][1] == "ok" and rows[1][1] != "ok" else "see-rows"
    print(f"C3-UIDMAP-WRITER={verdict}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["forker", "agent", "matrix"], required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--uid", type=int, default=10000)
    ap.add_argument("--map", choices=["identity", "zero"], default="identity")
    ap.add_argument("--marker", default="/var/lib/e2b-sandboxes/state/.c3map-probe/m.txt")
    ap.add_argument("--deadline", type=float, default=60.0)
    ap.add_argument("--parent-tries-write", action="store_true")
    ap.add_argument(
        "--drop-uid",
        type=int,
        default=None,
        help="forker 侧先降权到这个 uid（本机对照臂用；需要以 root 启动）",
    )
    ap.add_argument(
        "--pid",
        type=int,
        default=None,
        help="agent 侧直接指定宿主 pid（同 pid namespace 的对照臂用，跳过扫描）",
    )
    ap.add_argument(
        "--matrix-drop",
        type=int,
        default=None,
        help="matrix 臂：让被映射的子进程先降权到这个 uid（二分目标身份这个变量）",
    )
    ap.add_argument(
        "--matrix-writer-uid",
        type=int,
        default=None,
        help="matrix 臂：让**写者**降到这个 uid 但保留 CAP_SETUID/SETGID"
        "（'agent 与 worker 同 uid' 那一档，即 newuidmap 的形态）",
    )
    ap.add_argument(
        "--agent-keep-caps-uid",
        type=int,
        default=None,
        help="agent 臂：先降到这个 uid 再写（保留 CAP_SETUID/SETGID）——跨 pod 的同 uid 形态",
    )
    ap.add_argument(
        "--matrix-target-dumpable",
        choices=["keep", "0", "1"],
        default="keep",
        help="matrix 臂：目标 unshare 前先设 prctl(PR_SET_DUMPABLE)（二分 euid 与 dumpable）",
    )
    ap.add_argument(
        "--pids-file",
        default=None,
        help="forker 写出 `self_pid child_pid`（容器 pid）；agent 读它并用 NSpid 解析成宿主 pid"
        "——生产级 rendezvous，同时打印两者的 cgroup 以验证归属",
    )
    args = ap.parse_args()
    if args.role == "forker":
        return role_forker(args)
    if args.role == "agent":
        return role_agent(args)
    return role_matrix(args)


if __name__ == "__main__":
    sys.exit(main())
