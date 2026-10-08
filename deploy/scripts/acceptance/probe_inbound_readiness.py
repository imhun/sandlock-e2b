#!/usr/bin/env python3
"""The two inbound-mapping shapes, measured inside a worker pod (N89 re-read).

Runs INSIDE a worker pod (or any container with the sandlock wheel) as the
worker's own uid. It builds a `net_isolation` sandbox with a host-listener port
mapping and drives a small series of 4-byte echo round trips from the
container's own network namespace.

Arms:

* **host-listener** (`net_bind_inject=false`): the mapping is served from the
  supervisor's host listener, and the sandbox accepts from it with a
  **blocking** `accept()`. That is the only server shape this arm supports
  since N89 retired the readiness synthesis (`network/readiness.rs`): an
  event-loop server waits on its own listener, which never becomes readable
  from a connection queued supervisor-side, so it would hang with no error
  anywhere. E2B therefore refuses to build a mapped sandbox with injection off
  (`MAPPED_SANDBOX_NEEDS_INJECTION`); this arm measures what still works, which
  is the blocking/threaded server.
* **bind-injection** (`net_bind_inject=true`, the fleet default): the sandbox's
  own listener *is* a host-loopback socket, so an `asyncio` (epoll) server is
  woken by the kernel and every wait is ordinary kernel work. The injected arm
  is the one production uses, and N89's T5 re-reads its round-trip.

`pid_ns=True` is not decoration in this image: the shipped worker seccomp
profile (`sandlock-worker.json`) allows `clone3` but **not** `unshare`, so a
`net_isolation` sandbox can only get its netns from the single `clone3` call
that makes a pid-ns leader. A direct `net_isolation` create without it fails
with `sandlock_create failed` in that pod (`sandbox_shape_matrix.py` pins
which shapes a pod can build). The fleet's own worker the env sets
`E2B_PID_NS=true` alongside `E2B_ENABLE_NET_ISOLATION=true`, which is why its
sandboxes take that path.

Usage:
    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    kubectl -n sandlock exec -i e2b-worker-0 -c worker -- python3 - \
        < deploy/scripts/acceptance/probe_inbound_readiness.py

Readings (2026-10-08, fleet arm64 / Rocky 6.12, e2b-worker-0, 5 rounds each,
**before** N89; the fleet's MCP path ships `E2B_NET_BIND_INJECT=true`, so the
second row is what production actually uses):
    host-listener + asyncio (the retired synthesis): 72.1 / 81.3 / 81.2 / 81.2 / 81.2 ms
    bind-injection (E2B default)                  : 0.5 / 0.4 / 0.3 / 0.3 / 0.3 ms
The 81 ms row is history: N89 deleted the synthesis that produced it, and that
shape is refused at create now. Re-run this probe to record the
blocking-accept row for the host-listener arm, which is what the shape supports
after N89.
"""

import os
import select
import socket
import sys
import time

from sandlock import Sandbox, StdioMode

HOST_PORT = int(os.environ.get("N88_HOST_PORT", "50021"))
SANDBOX_PORT = int(os.environ.get("N88_SANDBOX_PORT", "8080"))
ROUNDS = int(os.environ.get("N88_ROUNDS", "5"))

SERVER = r'''
import asyncio
import selectors
import socket
import sys

port = int(sys.argv[1])

# This kernel's own epoll fdinfo spelling. N89 retired the supervisor-side
# reader (`network/readiness.rs`), so this is now only an observation of the
# kernel under test -- kept because it is cheap and it is the file any future
# reader would parse.
a, b = socket.socketpair()
sel = selectors.DefaultSelector()
sel.register(a, selectors.EVENT_READ)
with open(f"/proc/self/fdinfo/{sel.fileno()}") as fh:
    sys.stdout.write("FDINFO-BEGIN\n" + fh.read() + "FDINFO-END\n")
sys.stdout.flush()
sel.close()
a.close()
b.close()


async def handle(reader, writer):
    data = await reader.read(4)
    writer.write(data)
    await writer.drain()
    writer.close()


async def main():
    await asyncio.start_server(handle, "127.0.0.1", port)
    await asyncio.get_running_loop().create_future()


asyncio.run(main())
'''

# The host-listener arm's server: a blocking `accept()`. The supervisor owns the
# host socket and hands the accepted connection over as this accept's result,
# which is the contract N89 left in place.
SERVER_BLOCKING = r'''
import socket
import sys

port = int(sys.argv[1])
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port))
listener.listen(8)
sys.stdout.write("listening\n")
sys.stdout.flush()

while True:
    conn, _ = listener.accept()
    data = conn.recv(4)
    conn.sendall(data)
    conn.close()
'''


def drain(stream, seconds):
    """Read whatever is ready on `stream` for at most `seconds`."""
    fd = stream.fileno()
    out = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            if out:
                break
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        out += chunk
    return out


def one_shape(label, host_port, bind_inject, server=SERVER):
    sb = Sandbox(
        fs_readable=["/usr", "/lib", "/lib64", "/bin", "/etc", "/proc", "/dev"],
        fs_writable=["/tmp"],
        net_isolation=True,
        # See the module docstring: the worker seccomp profile has no `unshare`.
        pid_ns=True,
        port_mappings={host_port: SANDBOX_PORT},
        net_bind_inject=bind_inject,
        net_allow=[f"127.0.0.1:{SANDBOX_PORT}"],
        net_allow_bind=[SANDBOX_PORT],
    )

    conn = None
    with sb.popen(
        [sys.executable, "-c", server, str(SANDBOX_PORT)],
        stdout=StdioMode.PIPED,
        stderr=StdioMode.PIPED,
    ) as proc:
        try:
            deadline = time.time() + 20
            last = None
            while time.time() < deadline:
                try:
                    conn = socket.create_connection(("127.0.0.1", host_port), timeout=2)
                    break
                except OSError as exc:  # the mapped listener comes up with listen()
                    last = exc
                    time.sleep(0.05)
            if conn is None:
                print(f"{label}: HOST-CONNECT-FAILED {last}", flush=True)
            else:
                lat = []
                for i in range(ROUNDS):
                    if i:
                        conn = socket.create_connection(("127.0.0.1", host_port), timeout=5)
                    conn.settimeout(30)
                    t0 = time.perf_counter()
                    conn.sendall(b"ping")
                    data = conn.recv(4)
                    lat.append((time.perf_counter() - t0) * 1e3)
                    conn.close()
                    if data != b"ping":
                        print(f"{label}: BAD-ECHO {data!r}", flush=True)
                        break
                print(
                    f"{label}: rounds={len(lat)} "
                    + " ".join(f"{v:.1f}ms" for v in lat)
                    + f"  min={min(lat):.1f} median={sorted(lat)[len(lat) // 2]:.1f}",
                    flush=True,
                )
            out = drain(proc.stdout, 5)
            err = drain(proc.stderr, 2)
        finally:
            if conn is not None:
                conn.close()

    body = out.decode("utf-8", "replace").strip()
    if body:
        print("CHILD-STDOUT-BEGIN", flush=True)
        sys.stdout.write(body + "\n")
        print("CHILD-STDOUT-END", flush=True)
    if err.strip():
        print("CHILD-STDERR-BEGIN", flush=True)
        sys.stdout.write(err.decode("utf-8", "replace"))
        print("CHILD-STDERR-END", flush=True)


def main():
    print(f"uid={os.getuid()} sandbox_port={SANDBOX_PORT}", flush=True)
    # The surviving host-listener shape: a blocking accept() (N89).
    one_shape(
        "host-listener (blocking accept)   ",
        HOST_PORT,
        bind_inject=False,
        server=SERVER_BLOCKING,
    )
    # The fleet's shipped default: the sandbox's own listener is a host socket,
    # so an event loop is served by the kernel.
    one_shape("bind-injection + asyncio       ", HOST_PORT + 1, bind_inject=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
