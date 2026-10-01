"""Route-B: does the payload get seccomp notifications, and does statfs reach the handler?

Written for the SEC-K0S-007 correction (2026-10-01). The audit had concluded
"a route-B payload receives no seccomp notifications at all"; the shape here is
the one the `statfs` acceptance case builds
(``tests/security/conftest.py::route_b_sandbox(None, None)`` = pure rootfs +
route B), and the four readings in one run tell the whole story:

* ``HOST``/``STATVFS``: the payload's numbers, against the *host's* control the
  probe prints first. With the accounting reachable they differ (the ledger);
  before the fix they were byte-identical to the host's, which was the symptom.
* ``PROBE``: the payload's own pid/ppid, ``PR_GET_SECCOMP``, its ``uname``
  nodename (virtual under a mediator), its ``/proc/meminfo`` first line
  (synthesized by ``procfs.rs``, reachable only through a notif handler) and an
  ``inotify_add_watch`` on a host path (refused only by the mediator).
* ``SLOT-STDERR``/trace: with ``SANLOCK_EVENT_TRACE=1`` the fork logs one line
  per notification it receives (``notif nr=137 pid=<payload> -> return-value``),
  i.e. it names the payload as a notification source.
* ``LEDGER_FIFO=1``: replaces the ledger with a FIFO. If the accounting handler
  is reached its ``read`` blocks, so the payload's ``statfs`` never comes back
  (the probe reports a timeout); if it is shadowed -- the bug -- the payload gets
  the host's numbers immediately.

Usage (inside the lane container, from the repo root):
    python3 deploy/scripts/acceptance/routeb_statfs_probe.py
    SANLOCK_EVENT_TRACE=1 PROBE_KIND=min python3 deploy/scripts/acceptance/routeb_statfs_probe.py
    LEDGER_FIFO=1 PROBE_KIND=min python3 deploy/scripts/acceptance/routeb_statfs_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Run from the repo root (the lane container mounts it at /workspace); the
# script's own directory is sys.path[0], so the repo root has to be added by
# hand for `tests.security.conftest` to resolve. `E2B_HOST_PROJECT` is the host
# spelling and only helps when the paths happen to line up.
for _root in (os.getcwd(), os.environ.get("E2B_HOST_PROJECT", "")):
    if _root and os.path.isdir(_root) and _root not in sys.path:
        sys.path.insert(0, _root)

from tests.security.conftest import route_b_sandbox, run_sh, sandbox_tmpdir  # noqa: E402

PAYLOAD = r'''
import ctypes, json, os, platform

class _Statfs(ctypes.Structure):
    _fields_ = [
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    ]

libc = ctypes.CDLL("libc.so.6", use_errno=True)
out = {"pid": os.getpid(), "ppid": os.getppid()}
out["seccomp_mode"] = libc.prctl(21, 0, 0, 0, 0)  # PR_GET_SECCOMP

st = os.statvfs("/")
out["statvfs_root"] = [st.f_frsize, st.f_blocks, st.f_bfree, st.f_bavail]

# The fd-based sibling: fstatfs(2) is a *different* syscall, so a handler
# registered only for statfs(2) is not reached by `os.fstatvfs(fd)`.
fd = os.open("/", os.O_RDONLY)
fs = os.fstatvfs(fd)
out["fstatvfs_root"] = [fs.f_frsize, fs.f_blocks, fs.f_bfree, fs.f_bavail]
os.close(fd)

# Raw statfs: what `df -T` would print as the filesystem type.
raw = _Statfs()
if libc.statfs(b"/", ctypes.byref(raw)) == 0:
    out["statfs_root_f_type"] = hex(raw.f_type & 0xFFFFFFFF)
    out["statfs_root_f_namelen"] = raw.f_namelen
    out["statfs_root_f_files_eq_blocks"] = raw.f_files == raw.f_blocks
try:
    ws = _Statfs()
    if libc.statfs(b"/workspace", ctypes.byref(ws)) == 0:
        out["statfs_workspace_f_type"] = hex(ws.f_type & 0xFFFFFFFF)
except Exception as exc:  # noqa: BLE001
    out["statfs_workspace_err"] = str(exc)

# Independent notif-mediated path syscall (Landlock has no inotify right).
ifd = libc.inotify_init1(os.O_NONBLOCK)
ctypes.set_errno(0)
wd = libc.inotify_add_watch(ifd, b"/tmp", 0x2)
out["inotify_add_watch_tmp"] = [wd, ctypes.get_errno()]

try:
    out["proc_meminfo_line1"] = open("/proc/meminfo").readline().strip()
except OSError as exc:
    out["proc_meminfo_line1"] = f"ERR:{exc}"

out["nodename"] = os.uname().nodename
print("PROBE|" + json.dumps(out, sort_keys=True))
'''

# Minimal payload: one write (openat control) first, then statfs LAST so the
# slot's 8 KiB stderr tail still holds it when SANLOCK_EVENT_TRACE=1 floods it.
MIN_PAYLOAD = r'''
import os

with open("/workspace/touched", "w") as fh:
    fh.write("x")
st = os.statvfs("/")
print("STATVFS", st.f_frsize, st.f_blocks, st.f_bfree, flush=True)
'''


def main() -> int:
    # Control values from the worker side: what the *host* (container) says.
    host_st = os.statvfs("/")
    print(
        "HOST",
        json.dumps(
            {
                "hostname": os.uname().nodename,
                "meminfo": open("/proc/meminfo").readline().strip(),
                "statvfs_root": [
                    host_st.f_frsize,
                    host_st.f_blocks,
                    host_st.f_bfree,
                ],
            },
            sort_keys=True,
        ),
    )
    ws = sandbox_tmpdir()
    stats = ws / "disk-stats"
    ledger = os.environ.get("LEDGER", "10737418240 4294967296")
    fifo = os.environ.get("LEDGER_FIFO") == "1"
    if fifo:
        # A FIFO at the ledger path blocks the mediator's read_to_string the
        # moment the disk-stats handler is reached -- i.e. it tells "handler
        # registered and reading" apart from "no handler at all" without
        # touching the fork.
        os.mkfifo(stats, 0o666)
    else:
        stats.write_text(ledger + "\n")
    os.chmod(ws, 0o777)
    os.chmod(stats, 0o666)
    (ws / "p.py").write_text(
        MIN_PAYLOAD if os.environ.get("PROBE_KIND") == "min" else PAYLOAD
    )

    executor, workspace = route_b_sandbox(
        None, None, workspace=ws, disk_stats_path=str(stats)
    )
    try:
        async def _run():
            return await asyncio.wait_for(
                run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/p.py"),
                timeout=8.0,
            )

        try:
            code, out, err = asyncio.run(_run())
        except TimeoutError:
            print("EXIT timeout -- the payload's statfs never came back")
            print("SLOT-STDERR >>>", "n/a", "<<<")
            return 0
        print("EXIT", code)
        print("STDOUT", out.decode(errors="replace").strip())
        print("STDERR", err.decode(errors="replace").strip())
        # The ledger the mediator is supposed to report: 10 GiB total, 4 GiB used.
        print("LEDGER-PATH", stats, "fifo:", fifo)
        if not fifo:
            print("LEDGER", stats.read_text().strip())
        instance = getattr(executor, "_instance", None)
        if instance is not None and hasattr(instance, "request"):
            try:
                live = instance.request("config", {})
                blob = json.dumps(live)
                print("LIVE-CONFIG has disk_stats_path:", "disk_stats_path" in blob)
                if isinstance(live, dict):
                    data = live.get("data", live)
                    if isinstance(data, dict):
                        print("LIVE disc:", data.get("disk_stats_path"))
            except Exception as exc:  # noqa: BLE001
                print("LIVE-CONFIG failed:", exc)
        cfg = getattr(executor, "_route_b", None)
        tmp_root = getattr(cfg, "tmp_root", None)
        if tmp_root is not None:
            root = __import__("pathlib").Path(tmp_root)
            print("TMP-ROOT", root, "exists:", root.exists())
            for doc in sorted(root.rglob("*")):
                print("  TREE", doc)
            for doc in sorted(root.rglob("policy.json")):
                text = doc.read_text()
                print("POLICY-DOC", doc, "has disk_stats_path:", "disk_stats_path" in text)
                payload = json.loads(text)
                print("  disk_stats_path =", payload.get("disk_stats_path"))
        instance = getattr(executor, "_instance", None)
        tail = (
            instance.slot_stderr(6000)
            if instance is not None and hasattr(instance, "slot_stderr")
            else "no slot handle"
        )
        print("SLOT-STDERR >>>", tail, "<<<")
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
