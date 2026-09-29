#!/usr/bin/env python3
"""The stand-in for ``W1SlotPool``: a worker-side child at a chosen container pid.

Judgment 13 needs two workers on one host that each have a child at the **same
container pid**, so that the ``NSpid`` chain alone cannot tell them apart. This
script is the worker-side half of that rig: run inside a worker container as the
worker's own uid, it forks until one of its children is the requested pid, then
lets that child do what the production child does -- ``unshare(CLONE_NEWUSER)``,
report readiness, then poll ``setresuid`` until *somebody else* writes its map
(C3's agent, over the real CP hop).

Two lines of evidence it exists to produce:

* ``HARNESS-READY container_pid=<n> pidns=<pid:[..]>`` -- the pid the worker can
  name, and the worker's own pid namespace identity (the value the control plane
  records and the agent matches on).
* the report's own ``REPORT-IDS`` / ``REPORT-POLICY`` lines, printed **after**
  the grant lands -- i.e. what the slot process really is, once the agent has
  written its map (``c3_accept13_slot_report.py``).

The child is the **production** child program, not a copy of it:
``spawn_child`` execs ``python3 -m envd_service.slot_identity --uid X
--unshared-fd N -- python3 <report>``, exactly ``W1SlotPool``'s argv shape (the
report stands in for ``sandlock-supervise``, whose first act is the same
``policy.json`` read). Everything the child does with its identity after that
is the shipped code's doing; this file only decides *when* to report, and with
which pid.

Usage (standard input is this file; the worker has no checkout mounted):

    docker exec -i -u 65534:65534 <worker> sh -c 'cat > /tmp/h.py' < this file
    docker exec -d -u 65534:65534 <worker> sh -c \\
        'nohup python3 /tmp/h.py <pid-floor> <tag> <probe-dir> <slot-uid> <report.py> \\
         >/tmp/h-<tag>.log 2>&1 &'
"""

from __future__ import annotations

import os
import sys
import time

def _log(tag: str, message: str) -> None:
    print(f"[{tag}] {message}", flush=True)


def child_argv(uid: int, report: str, tag: str, probe_dir: str, write_fd: int) -> list[str]:
    """The production child's argv (``envd_service.slot_identity``'s own shape)."""
    return [
        sys.executable,
        "-m",
        "envd_service.slot_identity",
        "--uid",
        str(uid),
        "--unshared-fd",
        str(write_fd),
        "--",
        sys.executable,
        report,
        tag,
        probe_dir,
    ]


def main() -> int:
    floor = int(sys.argv[1])
    tag = sys.argv[2]
    probe_dir = sys.argv[3]
    uid = int(sys.argv[4])
    report = sys.argv[5]
    with open(f"/tmp/c3h-{tag}.pid", "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    # A negative floor means "take the next pid this namespace hands out": the
    # arms that need a *specific* number use the floor, the ones that only need
    # "a pid this worker has and another does not" do not care which.
    target = None if floor < 0 else floor
    _log(tag, f"HARNESS-WORKER pid={os.getpid()} uid={os.getuid()} "
               f"pidns={os.readlink('/proc/self/ns/pid')}")
    attempts = 0
    while attempts < 4000:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            if target is not None and os.getpid() != target:
                # Not the pid we are after: leave without touching namespaces.
                os._exit(0)
            os.set_inheritable(write_fd, True)
            os.execv(
                sys.executable,
                child_argv(uid, report, tag, probe_dir, write_fd),
            )
            os._exit(0)
        os.close(write_fd)
        attempts += 1
        if target is None or pid == target:
            data = os.read(read_fd, 1)          # wait for the child's unshare
            os.close(read_fd)
            if data != b"x":
                _log(tag, "HARNESS-FAIL child died before unsharing")
                return 1
            _log(tag, f"HARNESS-READY container_pid={pid} "
                       f"pidns={os.readlink('/proc/self/ns/pid')}")
            time.sleep(900.0)
            return 0
        os.close(read_fd)
        os.waitpid(pid, 0)
    _log(tag, f"HARNESS-FAIL never reached pid {floor} (last {pid})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
