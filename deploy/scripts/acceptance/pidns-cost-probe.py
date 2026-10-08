"""Probe: what does `pid_ns` cost on the syscalls it traps?

N2 in docs/task-backlog.md ("pid_ns 的 syscall 代价"): with pid_ns the fork has
to mediate `newfstatat`/`statx`/`faccessat`/`faccessat2`/`readlinkat` (plus the
legacy ABI), because seccomp cannot filter on the path -- every call enters the
supervisor and reads the path out of the child. All of them are hot paths, and
the netns lesson (a readiness-synthesis interception cost 390 ms per request,
which killed that rollout) says: measure before enabling.

Run in the prod-shaped lane, both shapes, same image:

    E2B_PID_NS=1 ... ./deploy/scripts/test-prod-shaped.sh tmp/pidns-cost-probe.py -k pidns_cost_probe -s
    E2B_PID_NS=0 ... ./deploy/scripts/test-prod-shaped.sh tmp/pidns-cost-probe.py -k pidns_cost_probe -s

Three workloads, 2000 iterations x 3 rounds each, inside one sandbox exec (so
process startup is amortised and only the syscall path is compared):

  * `noop`     -- pure Python loop (the per-iteration loop cost, the floor);
  * `open`     -- `open()`+`close()` on the same file: NOT trapped by pid_ns, so
                  it is the control for "a syscall that stays in the kernel";
  * `stat`     -- `os.stat()`: trapped by pid_ns (`newfstatat`);
  * `access`   -- `os.access()`: trapped (`faccessat`/`faccessat2`);
  * `readlink` -- `os.readlink()`: trapped (`readlinkat`).

The number that matters is `stat - open` in each shape, and its difference
between the shapes: that isolates the per-call supervisor round-trip from the
Python-loop and file-cache noise.
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from pathlib import Path

import httpx
import pytest

from tests.contract.test_nonroot_own_identity import _make_apps, _ready_fields
from tests.contract.test_uid_permissions import _result, _run_cmd

OWN_IDENTITY_LOGGER = "envd_service.executors.sandlock"

PROGRAM = '''\
import os, statistics, time

# 5000 iterations keep a ~20 us/call effect (~100 ms) well above the run-to-run
# noise, while the whole program still fits inside the command channel's 30 s
# budget (each trapped call costs ~0.2 ms in this shape).
N = 5000
ROUNDS = 4


def timed(fn, n=N):
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) * 1000.0


def noop():
    pass


def open_close():
    open("/etc/passwd", "rb").close()


def stat_passwd():
    os.stat("/etc/passwd")


def access_passwd():
    os.access("/etc/passwd", os.R_OK)


def readlink_python():
    os.readlink("/usr/local/bin/python3")


workloads = [
    ("noop", noop),
    ("open", open_close),
    ("stat", stat_passwd),
    ("access", access_passwd),
    ("readlink", readlink_python),
]
for name, fn in workloads:
    try:
        fn()
    except OSError as e:
        print("%s_ms=SKIP(%s)" % (name, e.__class__.__name__))
        continue
    samples = [timed(fn) for _ in range(ROUNDS)]
    print(
        "%s_ms=%s median=%.1f"
        % (name, ",".join("%.1f" % s for s in samples), statistics.median(samples))
    )
'''


def cost_cmd() -> str:
    """Ship the program as base64 (no shell metacharacters to mis-quote)."""
    blob = base64.b64encode(PROGRAM.encode()).decode()
    return (
        f"printf %s {blob} | base64 -d > pidns-cost.py; "
        "python3 pidns-cost.py; rm -f pidns-cost.py"
    )


@pytest.fixture()
def probe_workspace() -> Path:
    path = Path(tempfile.mkdtemp(prefix="pidns-cost-"))
    path.chmod(0o711)
    return path


async def test_pidns_cost_probe(probe_workspace, caplog) -> None:
    caplog.set_level(logging.INFO, logger=OWN_IDENTITY_LOGGER)
    control, envd = _make_apps(probe_workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        created = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key", "X-Sandbox-Id": "sbx_pidns_cost"},
            json={"templateID": "base", "timeout": 300},
        )
        assert created.status_code == 201, created.text
        payload = created.json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as envd_client:
        messages = await _run_cmd(envd_client, payload, cost_cmd())
    code, out, err = _result(messages)

    ready = [r.getMessage() for r in caplog.records if "own-identity instance ready" in r.getMessage()]
    assert ready, "the worker never logged an own-identity ready line"
    fields = _ready_fields(ready[-1])
    print(f"PROBE guest-uid={fields['guest_uid']} slot-uid={fields['uid']} pid_ns={os.environ.get('E2B_PID_NS', '<unset>')}")
    print(f"PROBE code={code} err={err!r}")
    for line in out.decode().strip().splitlines():
        print(f"PROBE {line}")
