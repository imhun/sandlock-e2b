#!/usr/bin/env python3
"""Why does a *restored* sandbox not tick? (2026-09-25, cluster probe)

The acceptance gets as far as "the worker restored the process into a session",
and the counter file is still there with the value it had at the pause -- but it
never advances again. This probe reproduces that and leaves the sandbox alive so
the node can be inspected (process states, the restored pid).

Run with KUBECONFIG + E2B_API_URL + E2B_API_KEY set; it prints the sandbox id
and does NOT kill it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import httpx

API = os.environ["E2B_API_URL"]
KEY = os.environ["E2B_API_KEY"]
INTERNAL = os.environ.get("E2B_INTERNAL_API_KEY", "internal-key")
KUBECONFIG = os.environ["KUBECONFIG"]

PROGRAM = (
    "import time\n"
    "n = 0\n"
    "while True:\n"
    "    n += 1\n"
    "    with open('/home/user/tick', 'w') as fh:\n"
    "        fh.write(str(n))\n"
    "    time.sleep(1)\n"
)


def step(name, **fields):
    print(json.dumps({"step": name, **fields}, ensure_ascii=False), flush=True)


def kubectl(*args, check=True):
    return subprocess.run(
        ["kubectl", "-n", "sandlock", *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": KUBECONFIG},
    )


def route(sandbox_id):
    resp = httpx.get(
        f"{API}/internal/routes/{sandbox_id}",
        headers={"X-Internal-Key": INTERNAL},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    from e2b import Sandbox

    sandbox = Sandbox.create(api_url=API, sandbox_url=API, api_key=KEY)
    sid = sandbox.sandbox_id
    pod = route(sid)["nodeID"]
    step("created", sandboxID=sid, pod=pod)

    sandbox.files.write("tick.py", PROGRAM)
    sandbox.commands.run("python3 -u /home/user/tick.py", background=True)
    deadline = time.monotonic() + 60
    while True:
        try:
            if int(sandbox.files.read("tick")) >= 3:
                break
        except Exception:
            pass
        assert time.monotonic() < deadline, "the counter never started"
        time.sleep(0.5)
    step("running", tick=sandbox.files.read("tick"))
    step("pause", paused=sandbox.pause())
    time.sleep(3)
    step("paused", tick=sandbox.files.read("tick"))

    uid_before = kubectl("get", "pod", pod, "-o", "jsonpath={.metadata.uid}").stdout
    kubectl("delete", "pod", pod)
    step("worker_deleted", pod=pod)
    deadline = time.monotonic() + 240
    while True:
        fields = kubectl(
            "get",
            "pod",
            pod,
            "-o",
            "jsonpath={.metadata.uid} {.status.containerStatuses[0].ready}",
            check=False,
        ).stdout.split()
        if len(fields) == 2 and fields[0] != uid_before and fields[1] == "true":
            break
        assert time.monotonic() < deadline, "the replacement worker never became ready"
        time.sleep(2)
    time.sleep(20)  # let it register + heartbeat
    step("worker_back", route=route(sid))

    Sandbox.connect(sid, api_url=API, sandbox_url=API, api_key=KEY)
    step("resumed")
    time.sleep(3)
    step("tick_after", tick=sandbox.files.read("tick"))
    step("KEEP", sandboxID=sid, pod=pod)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
