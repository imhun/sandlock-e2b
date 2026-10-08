#!/usr/bin/env python3
"""What do the inbound traps cost an event loop that cannot use them?

Production shape: `net_isolation` + a mapped port + bind injection ON. The
mapping used to put `listen`/`accept4`/`poll`/`ppoll`/`epoll_wait`/
`epoll_pwait` in the notification table, so *every* event-loop wait in that
sandbox took a supervisor round trip that could only answer Continue (the
injected listener is not in `ns.inbound`, so there is nothing to synthesize).

Runs inside a worker pod and times a tight `epoll_wait(timeout=0)` loop in two
sandboxes: one with a port mapping (traps, before the A change) and one without
(never trapped -- the control).

Usage (either the shipped wheel, or a wheel copied in for a candidate build):
    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    kubectl -n sandlock exec -i e2b-worker-0 -c worker -- python3 - \
        < deploy/scripts/acceptance/probe_eventloop_trap_cost.py

Readings (2026-10-08, fleet arm64 / Rocky 6.12, 20000 rounds):
    shipped wheel (before A):  mapped+injection 16.33 us/wait | no mapping 0.49
    with A (same pod, wheel on PYTHONPATH): expected to close the gap -- the
    mapped shape no longer traps the event loop at all.
"""

import os
import sys

from sandlock import Sandbox

ROUNDS = int(os.environ.get("N88_ROUNDS", "20000"))
PORT = int(os.environ.get("N88_HOST_PORT", "50023"))

CHILD = r'''
import select
import socket
import sys
import time

rounds = int(sys.argv[1])
a, b = socket.socketpair()
ep = select.epoll()
ep.register(a.fileno(), select.EPOLLIN)
ep.poll(0)
start = time.perf_counter()
for _ in range(rounds):
    ep.poll(0)
elapsed = time.perf_counter() - start
print(f"epoll_wait(0) rounds={rounds} total_ms={elapsed * 1e3:.1f} per_us={elapsed / rounds * 1e6:.2f}")
'''

BASE = dict(
    fs_readable=["/usr", "/lib", "/lib64", "/bin", "/etc", "/proc", "/dev"],
    fs_writable=["/tmp"],
    pid_ns=True,
)


def run(label, **extra):
    sb = Sandbox(**BASE, **extra)
    result = sb.run(["python3", "-c", CHILD, str(ROUNDS)], timeout=120)
    out = (result.stdout or b"").decode("utf-8", "replace").strip()
    err = (result.stderr or b"").decode("utf-8", "replace").strip()
    print(f"{label}: {out or err}", flush=True)


def main():
    print(f"uid={os.getuid()} rounds={ROUNDS}", flush=True)
    run(
        "mapped+injection (production MCP shape)",
        net_isolation=True,
        port_mappings={PORT: 8080},
        net_bind_inject=True,
        net_allow_bind=[8080],
    )
    run("net_isolation, no mapping (no trap)", net_isolation=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
