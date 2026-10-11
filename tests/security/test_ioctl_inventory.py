"""The measured ioctl surface, and the two preconditions the deny list leans on.

`seccomp_plan.rs` refuses `ioctl` request codes, but the *filter* only carries
those 12 historical entries plus the filesystem-layout codes. Two questions
this file answers against a live sandbox, so neither has to be argued from a
code comment:

1. **Which ioctls are actually reachable.** `LANDLOCK_ACCESS_FS_IOCTL_DEV` gates
   ioctl on *device* files by path, which cannot express "deny this request code
   but allow that one" on the same fd. So the request-code filter is the only
   place that granularity exists, and its value depends entirely on knowing what
   the reachable set is. Landlock's ABI is read to confirm the right is live at
   all.

2. **Why the terminal write family stays allowed.** `TCSETS`, `TCSETSW`,
   `TIOCSWINSZ` and the `TCSET*2` generation are all reachable, and denying
   them would break `openpty`, `tmux`, `vim`, `ssh` and the platform's own PTY
   endpoint. What they could attack is *not* reachable, and that is the part
   worth asserting:
     - a *foreign* `/dev/pts` node -- another session's pty, or the worker's
       own -- must not be openable from inside the sandbox;
     - `/dev/tty` must not be openable, so there is no controlling terminal to
       retarget.

   These are the load-bearing preconditions. The first one is asserted against
   the *measured* shape (2026-10-11): `/dev/pts` inside the sandbox **is** the
   worker's devpts instance bound in (`envd_service/executors/sandlock.py`
   `_minimal_dev_mounts` -> the fork's `minimal_dev`), so the slave nodes of
   every pty on the worker are visible *by name* -- a full-suite lane run
   leaves `0`/`1` behind from `tests/contract/test_pty_sandlock.py` and
   `tests/sdk/python/test_pty.py`. The protection is therefore DAC, not
   absence: the nodes carry the devpts `0620 root:tty` mode (owned by whoever
   opened the master, i.e. the worker), and the sandbox is a foreign uid, so
   opening one answers EACCES. A deployment that bound the *host's* devpts
   with looser ownership, or that ran the worker as the sandbox's own uid
   (the E5.1 shape `tests/security/test_worker_nonroot.py` exercises), turns
   `test_foreign_ptys_are_unreachable` red -- which is the shape this test is
   for. `test_controlling_terminal_is_unreachable` is unchanged.

   Also pinned here, because both were load-bearing during the audit that
   produced the list: `TIOCSTI`/`TIOCLINUX` stay refused on the one node where
   everything else answers, and the pty master and its slave do not agree --
   a code that is ENOTTY on the master can still be live on the slave, so
   measuring only the master understates the surface.

The suite runs one shape since N14 S5: the real root, through
`tests/security/conftest.py::own_identity_sandbox`.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    own_identity_sandbox,
    run_sh,
)

IMAGE = "python:3.11-slim"

# `TIOCSTI` and `TIOCLINUX` write into a terminal's *input* queue, so their
# reach is the terminal a descriptor names rather than the caller's own state --
# which is why they are denied while the termios write family is not. Every
# other code here is expected to answer on /dev/ptmx; if one silently stops
# answering, the deny list may have grown to cover it and the comment above
# needs revisiting rather than the test being loosened.
PTY_MASTER_INVENTORY = {
    "TIOCSTI": (0x5412, "refused"),
    "TIOCLINUX": (0x541C, "refused"),
    "TCGETS": (0x5401, "ok"),
    "TCSETS": (0x5402, "ok"),
    "TCSETSW": (0x5403, "ok"),
    "TCSETSF": (0x5404, "ok"),
    "TIOCGWINSZ": (0x5413, "ok"),
    "TIOCSWINSZ": (0x5414, "ok"),
    "TIOCGPTN": (0x80045430, "ok"),
    "TIOCSPTLCK": (0x40045431, "ok"),
    # The 64-bit termios generation. Denying TCSETS without these would be
    # theatre: they set the same attributes on the same fd.
    "TCGETS2": (0x802C542A, "ok"),
    "TCSETS2": (0x402C542B, "ok"),
    "TCSETSW2": (0x402C542C, "ok"),
    "TCSETSF2": (0x402C542D, "ok"),
    # Load-bearing for non-blocking fds; denying them breaks ordinary I/O.
    "FIONREAD": (0x541B, "ok"),
    "FIONBIO": (0x5421, "ok"),
}

PROBE = r"""
import ctypes, errno as E, json, os, pty

libc = ctypes.CDLL(None, use_errno=True)
libc.ioctl.restype = ctypes.c_int
libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
BUF = ctypes.create_string_buffer(4096)

CODES = %(codes)s

def ioctl_verdict(fd, code):
    ctypes.set_errno(0)
    r = libc.ioctl(fd, ctypes.c_ulong(code), ctypes.byref(BUF))
    e = ctypes.get_errno()
    if r >= 0:
        return "ok"
    # ENOTTY is the kernel saying the request code is not implemented for this
    # node. That is a different fact from "refused": it means the surface is not
    # there, which is why the two are kept apart everywhere in this file.
    if e == 25:
        return "not_implemented"
    return E.errorcode.get(e, "errno%%d" %% e).lower()

out = {}

# --- Landlock ABI: IOCTL_DEV only exists from ABI 5 ------------------------
ctypes.set_errno(0)
abi = libc.syscall(ctypes.c_long(444), ctypes.c_void_p(0), ctypes.c_size_t(0),
                   ctypes.c_uint32(1))
out["landlock_abi"] = abi if abi > 0 else None

# --- the device surface ----------------------------------------------------
out["dev_entries"] = sorted(os.listdir("/dev"))
try:
    out["pts_before_any_pty"] = sorted(os.listdir("/dev/pts"))
except OSError as exc:
    out["pts_before_any_pty"] = "errno%%d" %% exc.errno
# Every node here other than ptmx belongs to somebody else on this worker (its
# own pty, or another sandbox's session): /dev/pts is the worker's devpts bound
# into the sandbox, so those names are visible either way. What has to hold is
# that this uid cannot open one -- that is the whole isolation, and it is what
# the caller asserts.
out["pts_foreign"] = {}
if isinstance(out["pts_before_any_pty"], list):
    for name in out["pts_before_any_pty"]:
        if name == "ptmx":
            continue
        try:
            os.close(os.open("/dev/pts/" + name, os.O_RDWR | os.O_NOCTTY))
            out["pts_foreign"][name] = "opened"
        except OSError as exc:
            out["pts_foreign"][name] = E.errorcode.get(
                exc.errno, "errno%%d" %% exc.errno
            ).lower()
try:
    os.close(os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY))
    out["ctty_open"] = True
except OSError as exc:
    out["ctty_open"] = False
    out["ctty_errno"] = exc.errno

master, slave = pty.openpty()
try:
    out["slave_path"] = os.ttyname(slave)
    out["pts_after_one_pty"] = sorted(os.listdir("/dev/pts"))
    out["master"] = {n: ioctl_verdict(master, c) for n, (c, _w) in CODES.items()}
    out["slave"] = {n: ioctl_verdict(slave, c) for n, (c, _w) in CODES.items()}
finally:
    os.close(slave)
    os.close(master)

print("BEGIN_JSON")
print(json.dumps(out, sort_keys=True))
print("END_JSON")
"""


@pytest.fixture(scope="module")
def readings():
    """One sandbox, probed once -- each of these costs a sandbox creation."""
    rootfs = resolve_test_rootfs(IMAGE)
    executor, workspace = own_identity_sandbox(IMAGE, rootfs)
    require_mediation_capable(executor)
    codes = {name: list(spec) for name, spec in PTY_MASTER_INVENTORY.items()}
    script = PROBE % {"codes": json.dumps(codes)}
    b64 = base64.b64encode(script.encode()).decode()
    _code, out, err = asyncio.run(
        run_sh(executor, workspace, f"printf %s {b64} | base64 -d | python3 -")
    )
    text = out.decode("utf-8", "replace")
    assert "BEGIN_JSON" in text, f"probe produced no readings: {err[-800:]!r}"
    return json.loads(text.split("BEGIN_JSON", 1)[1].split("END_JSON", 1)[0])


@pytest.mark.usefixtures("require_sandlock")
async def test_landlock_ioctl_dev_is_enforced(readings):
    """ABI 5+ must be live, or the device-node gate is not in force at all."""
    abi = readings["landlock_abi"]
    assert abi is not None and abi >= 5, (
        f"Landlock ABI {abi} predates LANDLOCK_ACCESS_FS_IOCTL_DEV (ABI 5); the "
        f"device-node ioctl gate the design relies on is not available, so ioctl "
        f"would be bounded by nothing at all"
    )


@pytest.mark.usefixtures("require_sandlock")
async def test_foreign_ptys_are_unreachable(readings):
    """The precondition: no other process's terminal is reachable from here.

    `/dev/pts` is the worker's devpts bound in, so foreign slave nodes are
    visible by name (see the module docstring for the measurement); what has
    to hold is that this sandbox's uid cannot *open* one.
    """
    before = readings["pts_before_any_pty"]
    assert isinstance(before, list), (
        f"/dev/pts could not be listed at all ({before!r}); that is not the "
        f"same fact as 'it is empty', and an unlistable devpts is a shape of "
        f"its own"
    )
    foreign = readings["pts_foreign"]
    # EACCES is what a *present* foreign node answers (measured 2026-10-11 with
    # a pty held open in the runner container: /dev/pts/0 owned by root, the
    # sandbox uid refused). ENOENT is allowed because another session can close
    # its master between the listing and this probe -- a node that is gone is
    # not a node we can reach, so it cannot mask anything.
    reachable = {
        name: verdict
        for name, verdict in foreign.items()
        if verdict not in ("eacces", "enoent")
    }
    assert reachable == {}, (
        f"another process's pty is reachable from inside this sandbox: "
        f"{reachable!r} (every foreign node: {foreign!r}). The nodes are "
        f"root:tty 0620 and this sandbox is a foreign uid, so open() must "
        f"answer EACCES -- that DAC boundary is the only thing standing between "
        f"the allowed terminal write family and another tenant's terminal"
    )
    after = readings["pts_after_one_pty"]
    own = readings["slave_path"].rsplit("/", 1)[-1]
    # The listing is a *shared* space (the worker's devpts), so it can change
    # for reasons that have nothing to do with this sandbox -- a concurrent
    # session's openpty shows up here too, and this used to be asserted as
    # "exactly one new entry". What must hold is that our own openpty
    # materialized *our* node and that the kernel handed us that node.
    assert own not in before, (own, before)
    assert own in after, (own, after)


@pytest.mark.usefixtures("require_sandlock")
async def test_controlling_terminal_is_unreachable(readings):
    """The other precondition: nothing to retarget outside the sandbox."""
    assert readings["ctty_open"] is False, (
        "/dev/tty opened successfully. With a controlling terminal reachable the "
        "allowed TIOCSWINSZ/TCSETS family would have a terminal outside the "
        "sandbox to act on, and the deny list would need the write family back"
    )
    assert readings["ctty_errno"] == 6, (
        f"/dev/tty refused with errno {readings['ctty_errno']} rather than "
        f"ENXIO(6); a refusal for another reason is not the invariant this "
        f"precondition rests on"
    )


@pytest.mark.usefixtures("require_sandlock")
async def test_terminal_injection_codes_stay_refused(readings):
    """TIOCSTI/TIOCLINUX on the one node where every other code answers."""
    master = readings["master"]
    answering = [
        name for name, verdict in master.items() if verdict == "ok"
    ]
    assert answering, (
        f"no ioctl answered on the pty master, so the refusal of the injection "
        f"codes below proves nothing: {master!r}"
    )
    for name in ("TIOCSTI", "TIOCLINUX"):
        assert master[name] != "ok", (
            f"{name} answered on /dev/ptmx -- terminal injection is reachable and "
            f"the deny list is not holding"
        )


@pytest.mark.usefixtures("require_sandlock")
async def test_terminal_write_family_is_reachable(readings):
    """The other half of the trade: if these stop working, the design is wrong.

    Not a wish for the surface -- it is the thing that makes denying the write
    family untenable. `openpty` callers set termios and window size on the pty
    they just made, and this repo's own PTY endpoint does too
    (`envd_service/own_identity.py`, `envd_service/executors/local.py`). If a future
    change made these unreachable, the blocklist would be the right answer again
    and this test is what would notice.
    """
    for node in ("master", "slave"):
        for name in ("TCSETS", "TCSETSW", "TIOCSWINSZ", "TCSETS2"):
            assert readings[node][name] == "ok", (
                f"{name} did not answer on the pty {node} "
                f"({readings[node][name]!r}); the terminal write family is "
                f"unreachable, so it is no longer load-bearing and should be "
                f"added to the ioctl deny list"
            )


@pytest.mark.usefixtures("require_sandlock")
async def test_pty_master_and_slave_disagree(readings):
    """A code can be ENOTTY on the master and live on the slave.

    Found by measurement, not anticipated: TIOCNOTTY answers ENOTTY on the
    master and is refused on the slave. An inventory taken from the master alone
    would have recorded it as non-existent. This asserts the asymmetry exists so
    the deny list's reasoning stays honest about which node it was read from.
    """
    master, slave = readings["master"], readings["slave"]
    disagreements = {
        name: (master[name], slave[name])
        for name in master
        if master[name] != slave[name]
    }
    assert disagreements, (
        "the pty master and slave now answer identically for every probed code; "
        "the last measurement disagreed on TIOCNOTTY, so this file's per-node "
        "readings should be re-derived before trusting either"
    )


@pytest.mark.usefixtures("require_sandlock")
async def test_device_surface_is_only_the_expected_nodes(readings):
    """The device set is part of the argument; keep it from drifting silently."""
    entries = readings["dev_entries"]
    unexpected = set(entries) - {"null", "ptmx", "pts", "tty", "urandom", "zero"}
    assert not unexpected, (
        f"/dev now exposes {sorted(unexpected)!r} beyond the six known-benign "
        f"nodes. A granted device node carries LANDLOCK_ACCESS_FS_IOCTL_DEV, "
        f"and the request-code filter is a fixed list -- a new node is a new "
        f"ioctl surface that nothing in the policy has reviewed"
    )
