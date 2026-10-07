"""Probe: which shape did a route-B sandbox actually get?

The pid_ns acceptance (`tests/contract/test_nonroot_own_identity.py::
test_route_b_restores_guest_root_with_and_without_pid_ns`) asserts `id -u` == 0,
but that is also what the *default* (pid_ns off) shape reports -- so a green
run is only evidence for pid_ns if the run really enabled it. This probe prints
the three observables side by side, to be run twice in the prod-shaped lane:

    E2B_PID_NS=1 ... ./deploy/scripts/test-prod-shaped.sh tmp/pidns-shape-probe.py -k pidns_shape_probe
    E2B_PID_NS=  ... ./deploy/scripts/test-prod-shaped.sh tmp/pidns-shape-probe.py -k pidns_shape_probe

  * `guest-uid` from the worker's route-B ready line (`uid-0-in-userns` is the
    F18 self-map the fork reports through `stats`);
  * `id -u` inside the sandbox (the guest identity);
  * `nspid_levels` -- the kernel's own NSpid depth for the sandbox's own
    process: 2 in the shared host pid namespace, 3 when the sandbox has its own
    (host level, then the namespace level). This is the one observable that
    separates the two shapes, and it comes from the kernel, not from us.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import httpx
import pytest

from tests.contract.test_nonroot_route_b import _make_apps, _ready_fields
from tests.contract.test_uid_permissions import _result, _run_cmd

ROUTE_B_LOGGER = "envd_service.executors.sandlock"

#
# The pid-namespace observable is a `kill(2)` probe of a pid that is outside the
# sandbox: the kernel answers `ESRCH` only when the caller is in its own pid
# namespace (see the fork's `test_pid_ns_default_off_keeps_host_pid_view` /
# `pid_ns_kill_host_pid_is_esrch`). `/proc/self/status` (NSpid depth) would be
# the other one, but the sandbox's own fs policy denies it here.


def shape_cmd(host_pid: int) -> str:
    program = (
        "import os,errno\n"
        "def probe():\n"
        "    try:\n"
        f"        os.kill({host_pid},0); return 'ok'\n"
        "    except OSError as e:\n"
        "        return errno.errorcode.get(e.errno, str(e.errno))\n"
        "print('kill_host='+probe())\n"
    )
    return 'id -u; python3 -c "' + program + '"'


@pytest.fixture()
def probe_workspace() -> Path:
    path = Path(tempfile.mkdtemp(prefix="pidns-probe-"))
    path.chmod(0o711)
    return path


async def test_pidns_shape_probe(probe_workspace, caplog) -> None:
    caplog.set_level(logging.INFO, logger=ROUTE_B_LOGGER)
    control, envd = _make_apps(probe_workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        created = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key", "X-Sandbox-Id": "sbx_pidns_shape"},
            json={"templateID": "base", "timeout": 300},
        )
        assert created.status_code == 201, created.text
        payload = created.json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as envd_client:
        # This process *is* the worker, so its pid is a live host pid that the
        # sandbox must not be able to address when it has its own pid namespace.
        messages = await _run_cmd(envd_client, payload, shape_cmd(os.getpid()))
    code, out, err = _result(messages)

    ready = [r.getMessage() for r in caplog.records if "route-B instance ready" in r.getMessage()]
    assert ready, "the worker never logged a route-B ready line"
    fields = _ready_fields(ready[-1])
    print(f"PROBE guest-uid={fields['guest_uid']} slot-uid={fields['uid']}")
    print(f"PROBE id -u -> code={code} out={out!r} err={err!r}")
