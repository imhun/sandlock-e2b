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
     - `/dev/pts` must be a per-sandbox devpts instance, so no other sandbox's
       ptys are visible;
     - `/dev/tty` must not be openable, so there is no controlling terminal to
       retarget.

   These are the load-bearing preconditions. If a deployment ever mounts the
   host devpts instead of a private instance, `test_devpts_is_a_private_instance`
   and `test_controlling_terminal_is_unreachable` go red and the deny list needs
   the write family back. That is the point of the file: the gap is closed by a
   test on the preconditions rather than by a blocklist that would break the
   product.

   Also pinned here, because both were load-bearing during the audit that
   produced the list: `TIOCSTI`/`TIOCLINUX` stay refused on the one node where
   everything else answers, and the pty master and its slave do not agree --
   a code that is ENOTTY on the master can still be live on the slave, so
   measuring only the master understates the surface.

The suite runs one shape since N14 S5: the real root, through
`tests/security/conftest.py::route_b_sandbox`.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    route_b_sandbox,
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
    executor, workspace = route_b_sandbox(IMAGE, rootfs)
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
async def test_devpts_is_a_private_instance(readings):
    """The precondition: no other sandbox's ptys are in scope."""
    before = readings["pts_before_any_pty"]
    assert before == ["ptmx"], (
        f"/dev/pts holds {before!r} before the sandbox has allocated a pty. A "
        f"shared or host-mounted devpts would expose other tenants' terminals, "
        f"which is precisely what makes the allowed terminal write family safe"
    )
    after = readings["pts_after_one_pty"]
    # Exactly one new entry, and it is this process's own pty number.
    own = readings["slave_path"].rsplit("/", 1)[-1]
    assert set(after) - set(before) == {own}, (
        f"/dev/pts gained {sorted(set(after) - set(before))!r} for one openpty()"
    )


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
    (`envd_service/route_b.py`, `envd_service/executors/local.py`). If a future
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
