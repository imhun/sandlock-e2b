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

import asyncio
import ipaddress
import json
import socket
import threading
from pathlib import Path

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
    from tests.security.conftest import own_identity_sandbox, run_sh, sandbox_tmpdir

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

    # N15: the pure shape is mediated now, so a hand-built in-process executor
    # on a root worker is the shape the fork refuses (SL-1). The probe goes
    # through the deployment's entry point instead, and runs from a file: the
    # probe source carries quotes and newlines that a `sh -c` string would eat.
    ws = Path(sandbox_tmpdir())
    (ws / "loopback_probe.py").write_text(probe)
    executor, workspace = own_identity_sandbox(
        None,
        None,
        workspace=ws,
        allow_internet_access=True,
        enable_network=True,
        network={"allowInternetAccess": True},
        network_deny_cidrs=DEFAULT_NETWORK_DENY_CIDRS,
    )
    try:
        code, out, err = asyncio.run(
            run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/loopback_probe.py")
        )
        assert code == 0, err
        assert json.loads(out.decode()) == {
            "v4/0.0.0.0": "DENIED",
            "v4/0.0.0.1": "DENIED",
            "v4/127.0.0.1": "DENIED",
            "v6/::": "DENIED",
            "v6/::1": "DENIED",
        }, out.decode()
    finally:
        for sock in servers:
            sock.close()


@pytest.mark.usefixtures("require_sandlock")
def test_explicit_allowout_cannot_reach_a_protected_range():
    """SEC-K0S-004: an explicit allowlist is bounded by the same protected set.

    The allowlist branch is a *different* model from the implicit denylist, and
    it used to be unfiltered -- so a tenant could name the cluster's own pod
    CIDR (``allowOut: ["10.244.0.0/16"]``) or an internal address, and the
    connect, which the worker performs in its **own** network namespace,
    reached the control plane and the worker envd.

    Two spellings are covered here, because they fail for different reasons:

    * a literal (``127.0.0.1:PORT``) is dropped before the fork sees it;
    * a **hostname** (``localhost:PORT``) is matched against the *name*, so
      only the deny filter on the resolved address keeps it out -- the shape a
      DNS name that answers with a protected address would take.
    """
    from tests.security.conftest import own_identity_sandbox, run_sh, sandbox_tmpdir

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Ephemeral port: this file has another test that owns ``LISTEN_PORT``, and
    # sharing it makes the pair order-dependent.
    server.bind(("127.0.0.1", 0))
    listen_port = server.getsockname()[1]
    server.listen(8)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                conn.recv(64)
                conn.sendall(b"WORKER-INTERNAL")
            finally:
                conn.close()

    threading.Thread(target=serve, daemon=True).start()

    probe = """
import json, socket
PORT = %d
out = {}
for tag, host in (("literal", "127.0.0.1"), ("hostname", "localhost")):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect((host, PORT))
        out[tag] = "REACHED"
    except OSError:
        out[tag] = "DENIED"
    finally:
        s.close()
print(json.dumps(out, sort_keys=True))
""" % listen_port

    ws = Path(sandbox_tmpdir())
    (ws / "allowlist_probe.py").write_text(probe)
    executor, workspace = own_identity_sandbox(
        None,
        None,
        workspace=ws,
        allow_internet_access=True,
        enable_network=True,
        # Both spellings name the same worker-local listener, and both are
        # inside the protected set.
        network={"allowOut": [f"127.0.0.1:{listen_port}", f"localhost:{listen_port}"]},
        network_deny_cidrs=DEFAULT_NETWORK_DENY_CIDRS,
    )
    try:
        code, out, err = asyncio.run(
            run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/allowlist_probe.py")
        )
        assert code == 0, err
        assert json.loads(out.decode()) == {
            "hostname": "DENIED",
            "literal": "DENIED",
        }, out.decode()
    finally:
        server.close()
