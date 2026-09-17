"""SEC-001: every loopback spelling must be denied by the default denylist.

The implicit full-egress branch (``allowInternetAccess`` with no explicit
``allowOut``/``denyOut``) is a *denylist* model: default-allow, minus the
private ranges. That makes any spelling the list does not cover a hole, and
two of them reached worker-local services:

* ``connect(0.0.0.0)`` -- ``0.0.0.0`` is INADDR_ANY, which Linux routes to
  the local host, so it reached a listener bound to ``127.0.0.1`` while the
  ``127.0.0.0/8`` rule was doing its job.
* ``::1`` -- IPv6 loopback carried no rule at all (``IpCidr`` never matches
  across address families, so ``127.0.0.0/8`` cannot cover it).

Both reached the worker's own envd API. A per-sandbox netns does **not**
mitigate: the fork performs outbound connects on the sandbox's behalf inside
the worker's network namespace (``fd_inject_connect``), so the denylist is the
only thing standing between a sandbox and the worker's loopback.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading

import pytest

from envd_service.config import DEFAULT_NETWORK_DENY_CIDRS

#: Destination spellings that must all be refused. The first two are the
#: SEC-001 reproducers; the rest are the reason the list grew (each is a
#: different family or range, and no single rule covers them).
DENIED_SPELLINGS = (
    ("v4_unspecified", socket.AF_INET, "0.0.0.0"),
    ("v4_unspecified_neighbour", socket.AF_INET, "0.0.0.1"),
    ("v4_loopback", socket.AF_INET, "127.0.0.1"),
    ("v4_metadata", socket.AF_INET, "169.254.169.254"),
    ("v6_unspecified", socket.AF_INET6, "::"),
    ("v6_loopback", socket.AF_INET6, "::1"),
    ("v6_link_local", socket.AF_INET6, "fe80::1"),
)

LISTEN_PORT = 47231


def test_default_denylist_covers_every_loopback_spelling():
    """The list itself must contain a rule for each spelling -- the runtime
    probe below only proves it for one listener it happens to own."""
    nets = [ipaddress.ip_network(c, strict=False) for c in DEFAULT_NETWORK_DENY_CIDRS]
    for name, _fam, host in DENIED_SPELLINGS:
        addr = ipaddress.ip_address(host)
        assert any(addr in net for net in nets), f"{name} ({host}) is not denied"


def test_default_denylist_keeps_public_internet_open():
    """Guard against over-correction: the branch is default-allow, so a
    public destination must stay outside every rule."""
    nets = [ipaddress.ip_network(c, strict=False) for c in DEFAULT_NETWORK_DENY_CIDRS]
    for host in ("1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"):
        addr = ipaddress.ip_address(host)
        assert not any(addr in net for net in nets), f"{host} would be denied"


@pytest.mark.usefixtures("require_sandlock")
def test_loopback_spellings_cannot_reach_a_worker_local_listener():
    """The reproducer, as a live assertion: a listener bound to the worker's
    loopback must be unreachable from the sandbox under every spelling."""
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    from tests.security.conftest import sandbox_tmpdir

    servers: list[socket.socket] = []

    def serve(sock: socket.socket) -> None:
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                conn.recv(64)
                conn.sendall(b"WORKER-INTERNAL")
            finally:
                conn.close()

    for fam, addr in ((socket.AF_INET, ("127.0.0.1", LISTEN_PORT)),
                      (socket.AF_INET6, ("::1", LISTEN_PORT))):
        sock = socket.socket(fam, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(addr)
        sock.listen(8)
        threading.Thread(target=serve, args=(sock,), daemon=True).start()
        servers.append(sock)

    probe = """
import json, socket
PORT = %d
FAMILIES = [("v4", socket.AF_INET, ["0.0.0.0", "0.0.0.1", "127.0.0.1"]),
            ("v6", socket.AF_INET6, ["::", "::1"])]
out = {}
for tag, fam, hosts in FAMILIES:
    for host in hosts:
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(2.0)
        try:
            s.connect((host, PORT, 0, 0) if fam == socket.AF_INET6 else (host, PORT))
            out["%%s/%%s" %% (tag, host)] = "REACHED"
        except OSError:
            out["%%s/%%s" %% (tag, host)] = "DENIED"
        finally:
            s.close()
print(json.dumps(out, sort_keys=True))
""" % LISTEN_PORT

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=True,
        enable_network=True,
        network={"allowInternetAccess": True},
        network_deny_cidrs=DEFAULT_NETWORK_DENY_CIDRS,
    )
    try:
        result = executor._build_sandbox(
            ExecConfig(
                cmd=["/usr/local/bin/python3", "-c", probe],
                env={},
                cwd=ws,
                stdin_enabled=False,
            )
        ).run(["/usr/local/bin/python3", "-c", probe])
        assert result.exit_code == 0, result.error
        assert json.loads(result.stdout.decode()) == {
            "v4/0.0.0.0": "DENIED",
            "v4/0.0.0.1": "DENIED",
            "v4/127.0.0.1": "DENIED",
            "v6/::": "DENIED",
            "v6/::1": "DENIED",
        }, result.stdout.decode()
    finally:
        for sock in servers:
            sock.close()
