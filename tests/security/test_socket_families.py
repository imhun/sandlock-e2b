"""The socket-family surface, measured in a live sandbox rather than read off a list.

`crates/sandlock-core/src/netlink/handlers.rs` gates `socket(2)` with an
allow list of four families (`AF_UNIX`, `AF_INET`, `AF_INET6`, `AF_NETLINK`),
and inside `AF_NETLINK` refuses every protocol except `NETLINK_ROUTE`. The fork
pins that with unit tests, but a unit test can only prove the code matches the
code's own idea of the numbers. This file proves the **running worker** agrees,
and it exists because of one specific failure mode: the refusals that matter
most are indistinguishable, from inside the sandbox, from a refusal the *host*
would have produced anyway.

That ambiguity is not hypothetical. Measured 2026-10-04 with this exact
method, the same probe run twice:

  probe                     in sandbox        in worker container (control)
  AF_RXRPC(33)              EAFNOSUPPORT      EAFNOSUPPORT     <- same
  AF_KEY(15)                EAFNOSUPPORT      EAFNOSUPPORT     <- same
  NETLINK_XFRM(6)           EAFNOSUPPORT      CREATED          <- sandbox refused
  NETLINK_KEY(16)           EAFNOSUPPORT      CREATED          <- sandbox refused
  NETLINK_ROUTE(0)          CREATED           CREATED          <- virtualized

Read the sandbox column alone and `AF_RXRPC` looks deliberately closed. Read
both columns and the truth is sharper: `AF_RXRPC` is refused by the fork *and*
happens to be unavailable here, so if a node ever loads rxrpc the structural
refusal is what remains -- and that is worth asserting, because it is the
difference between a policy and a coincidence. `NETLINK_XFRM` is the one that
only the sandbox refuses, so it is the one where the sandbox is doing real work.

`CVE-2026-43284` (IPsec/ESP "Dirty Frag") and `CVE-2026-43500` (RxRPC) are both
in CISA's KEV catalog as confirmed-exploited local privilege escalations with
container-escape potential. A sandbox that can open an `AF_RXRPC` socket, or an
`AF_NETLINK`/`NETLINK_XFRM` socket to add an ESP SA, has handed an attacker the
input path for one of them.

The suite runs one shape since N14 S5: the real root, through
`tests/security/conftest.py::own_identity_sandbox`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    own_identity_sandbox,
    run_sh,
)

IMAGE = "python:3.11-slim"

# The probe runs *inside* the sandbox, so it cannot import from this file. Keep
# the two copies in step by asserting the numbers here against the same literals.
AF_UNIX, AF_INET, AF_KEY, AF_NETLINK, AF_PACKET, AF_RXRPC, AF_ALG = 1, 2, 15, 16, 17, 33, 38
NETLINK_ROUTE, NETLINK_XFRM, NETLINK_KEY = 0, 6, 16

PROBE = r"""
import ctypes, errno as E, json, socket
libc = ctypes.CDLL(None, use_errno=True)
out = {}

AF_KEY, AF_NETLINK, AF_PACKET, AF_RXRPC, AF_ALG = 15, 16, 17, 33, 38
NETLINK_ROUTE, NETLINK_XFRM, NETLINK_KEY = 0, 6, 16

def mk(fam, typ, proto, label):
    ctypes.set_errno(0)
    fd = libc.socket(ctypes.c_int(fam), ctypes.c_int(typ), ctypes.c_int(proto))
    e = ctypes.get_errno()
    if fd >= 0:
        out[label] = "CREATED"
        libc.close(ctypes.c_int(fd))
    elif e == 97:
        out[label] = "EAFNOSUPPORT"
    elif e == 1:
        out[label] = "EPERM"
    elif e == 38:
        out[label] = "ENOSYS"
    else:
        out[label] = E.errorcode.get(e, "errno%d" % e)

mk(AF_KEY, socket.SOCK_RAW, 0, "AF_KEY")
mk(AF_KEY, socket.SOCK_DGRAM, 0, "AF_KEY/DGRAM")
mk(AF_RXRPC, socket.SOCK_DGRAM, 0, "AF_RXRPC")
mk(AF_RXRPC, socket.SOCK_STREAM, 0, "AF_RXRPC/STREAM")
mk(AF_NETLINK, socket.SOCK_RAW, NETLINK_XFRM, "NETLINK_XFRM")
mk(AF_NETLINK, socket.SOCK_RAW, NETLINK_KEY, "NETLINK_KEY")
mk(AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE, "NETLINK_ROUTE")
mk(AF_PACKET, socket.SOCK_RAW, 0, "AF_PACKET")
mk(AF_ALG, socket.SOCK_SEQPACKET, 0, "AF_ALG")
mk(socket.AF_UNIX, socket.SOCK_STREAM, 0, "AF_UNIX")

# What a workload sees through the one netlink protocol that is answered, next
# to what its own netns actually has. The pair is the measurement: netlink must
# not disclose an interface the sandbox's own /proc/net/dev does not show.
try:
    out["if_nameindex"] = socket.if_nameindex()
except OSError as exc:
    out["if_nameindex"] = E.errorcode.get(exc.errno)
try:
    out["proc_net_dev"] = sorted(
        ln.split(":")[0].strip() for ln in open("/proc/net/dev").read().splitlines()[2:]
        if ":" in ln
    )
except OSError as exc:
    out["proc_net_dev"] = E.errorcode.get(exc.errno)

print("BEGIN_JSON")
print(json.dumps(out, sort_keys=True))
print("END_JSON")
"""


@pytest.fixture(scope="module")
def surface():
    """One sandbox, probed once -- each of these costs a sandbox creation."""
    rootfs = resolve_test_rootfs(IMAGE)
    executor, workspace = own_identity_sandbox(IMAGE, rootfs)
    require_mediation_capable(executor)
    b64 = base64.b64encode(PROBE.encode()).decode()
    _code, out, err = asyncio.run(
        run_sh(executor, workspace, f"printf %s {b64} | base64 -d | python3 -")
    )
    text = out.decode("utf-8", "replace")
    assert "BEGIN_JSON" in text, f"probe produced no readings: {err[-800:]!r}"
    return json.loads(text.split("BEGIN_JSON", 1)[1].split("END_JSON", 1)[0])


@pytest.mark.usefixtures("require_sandlock")
async def test_family_numbers_are_the_kernels():
    """Cross-check the literals this file and the probe both hardcode.

    A wrong family number would make every refusal below pass for the wrong
    reason -- the same class of silent no-op as a mistyped seccomp JEQ.

    Compared against the `socket` module rather than `ctypes.CDLL(None)`: the
    `AF_*` values are preprocessor macros, not exported symbols, so a ctypes
    lookup raises `AttributeError` rather than reading them.
    """
    for name, ours, theirs in (
        ("AF_UNIX", AF_UNIX, socket.AF_UNIX),
        ("AF_INET", AF_INET, socket.AF_INET),
        ("AF_NETLINK", AF_NETLINK, socket.AF_NETLINK),
    ):
        assert ours == theirs, f"{name}: file says {ours}, socket says {theirs}"
    # AF_KEY and AF_RXRPC are not exported by every Python build; when they are
    # absent there is nothing to compare against, and saying so beats skipping
    # silently.
    for name, ours in (("AF_KEY", AF_KEY), ("AF_RXRPC", AF_RXRPC)):
        theirs = getattr(socket, name, None)
        if theirs is not None:
            assert ours == theirs, f"{name}: file says {ours}, socket says {theirs}"


@pytest.mark.usefixtures("require_sandlock")
async def test_rxrpc_family_is_refused(surface):
    """CVE-2026-43500's input path.

    Not merely "not created": a refusal has to be the fork's. The probe reports
    which errno came back, and the fork answers `EAFNOSUPPORT` because that is
    the kernel's own code for an unknown family -- so a workload cannot tell a
    sandbox refusal from a genuinely unsupported platform.
    """
    for probe in ("AF_RXRPC", "AF_RXRPC/STREAM"):
        assert surface[probe] == "EAFNOSUPPORT", (
            f"{probe} -> {surface[probe]!r}; an RxRPC socket is the input path for "
            f"CVE-2026-43500 (Dirty Frag, KEV-confirmed-exploited). Note this probe "
            f"cannot tell a sandbox refusal from the module being absent, which is "
            f"why the structural refusal is pinned in the fork's own tests too"
        )


@pytest.mark.usefixtures("require_sandlock")
async def test_key_family_is_refused(surface):
    """The keyring subsystem's own socket family.

    `add_key`/`request_key`/`keyctl` are already refused at the blocklist; this
    is the socket door onto the same subsystem.
    """
    for probe in ("AF_KEY", "AF_KEY/DGRAM"):
        assert surface[probe] == "EAFNOSUPPORT", (
            f"{probe} -> {surface[probe]!r}; the keyring netlink family should be "
            f"refused by the socket allow list"
        )


@pytest.mark.usefixtures("require_sandlock")
async def test_xfrm_netlink_is_refused(surface):
    """CVE-2026-43284's input path, and the one the sandbox uniquely refuses.

    This is the strongest of the three: the control run outside the sandbox
    *creates* this socket, so the refusal here is attributable to the sandbox and
    not to the module being missing. With no XFRM socket there is no way to add
    an ESP SA, and the in-place-decrypt-on-shared-pages bug has nothing to
    decrypt.
    """
    assert surface["NETLINK_XFRM"] == "EAFNOSUPPORT", (
        f"NETLINK_XFRM -> {surface['NETLINK_XFRM']!r}; this is the input path for "
        f"CVE-2026-43284 (Dirty Frag, IPsec/ESP, KEV-confirmed-exploited). Unlike "
        f"AF_RXRPC this one is created outside the sandbox, so the refusal is "
        f"the sandbox's own"
    )


@pytest.mark.usefixtures("require_sandlock")
async def test_key_netlink_protocol_is_refused(surface):
    assert surface["NETLINK_KEY"] == "EAFNOSUPPORT", (
        f"NETLINK_KEY -> {surface['NETLINK_KEY']!r}; same keyring subsystem as the "
        f"refused add_key/request_key/keyctl, reached over AF_NETLINK"
    )


@pytest.mark.usefixtures("require_sandlock")
async def test_netlink_route_stays_answered(surface):
    """The one netlink protocol that is allowed must keep working.

    This is the cost side of the trade, asserted so nobody "hardens" it away:
    `NETLINK_ROUTE` is virtualized into synthesized replies, and glibc's
    `getifaddrs()` -- which `if_nameindex()` sits on -- goes through it. Refusing
    it would be a user-visible regression in exchange for no disclosure, because
    the disclosure is measured to be already gone (next test).
    """
    assert surface["NETLINK_ROUTE"] == "CREATED", (
        f"NETLINK_ROUTE -> {surface['NETLINK_ROUTE']!r}; it is the one protocol the "
        f"supervisor answers synthetically, and blocking it would break "
        f"getifaddrs()/if_nameindex() for every workload"
    )


@pytest.mark.usefixtures("require_sandlock")
async def test_netlink_route_discloses_no_host_interface(surface):
    """Why hardening NETLINK_ROUTE is not worth doing: nothing is disclosed.

    An earlier version of this test compared `if_nameindex()` against
    `/proc/net/dev` and failed in the local test lane (`eth0` via netlink, `lo`
    via procfs). **The comparison was wrong, not the code.** Both channels are
    synthesized, but with different scopes:

      * `/proc/net/dev` is generated by `procfs.rs::generate_proc_net_dev`,
        which shows loopback only *unconditionally*;
      * the netlink responder's scope follows `net_isolation`, which
        `handlers.rs` passes as `loopback_only`:
          - net_isolation on (the k0s deployment): the sandbox has its own
            netns holding only `lo`, so the responder reports `lo`;
          - net_isolation off (the local lane): the sandbox shares the worker's
            netns, and the responder adds a **fabricated** non-loopback
            interface so `AI_ADDRCONFIG` and `ip addr` see something. It is
            TEST-NET-1 `192.0.2.0/24` per `synth.rs`, not a real interface --
            which is why it appears as `eth0` there and nowhere in production.

    Comparing a mode-aware view against a mode-blind one measures the modes, not
    a leak. The property that actually matters is absolute: the netlink view may
    contain the loopback the kernel gives every netns plus the one fabricated
    interface, and **nothing else**. The worker's real interfaces are the leak
    shape, and they are named things like `docker0`, `veth*`, `br-*`.

    This is also the second of the two independent legs that hide host
    interface names; the first is the `SIOCGIF*` ioctl deny list in
    `seccomp_plan.rs`. Neither implies the other, so both are asserted.
    """
    names = {n for _idx, n in surface["if_nameindex"]}
    allowed = {"lo", "eth0"}          # lo: the kernel's; eth0: synth.rs's TEST-NET-1 fiction
    leaked = sorted(names - allowed)
    assert not leaked, (
        f"if_nameindex() reports {sorted(names)!r}; {leaked!r} is neither the "
        f"kernel's loopback nor the fabricated TEST-NET-1 interface `synth.rs` "
        f"adds in the shared-netns mode. A real host interface here is the leak "
        f"-- the worker container lists lo, tunl0, eth0, docker0 and veth* "
        f"against this same probe"
    )
    # And the procfs channel is loopback-only by construction, in both modes.
    assert surface["proc_net_dev"] == ["lo"], (
        f"/proc/net/dev reports {surface['proc_net_dev']!r}; "
        f"procfs.rs::generate_proc_net_dev synthesizes loopback only regardless "
        f"of net_isolation, so anything else means that virtualization lapsed"
    )
