#!/usr/bin/env python3
"""N46 on the fleet: does an *unnamed* async copy hold the fleet-wide lease?

Before the fix it could not: `create_snapshot` took the claim only for a named
id, and released it as soon as the record existed -- so during an unnamed async
copy there was nothing in the shared store saying "somebody is copying this",
which is exactly what a peer's startup pass consults. The lease (value
``token:owner``, renewed while the copy runs, TTL 30 s) is that marker.

What this asserts:

1. an async snapshot posted *without* an id answers 202 and its record says
   ``creating``;
2. while it is copying, ``e2b:snapshot:copy:<id>`` exists in Redis, and its
   value names an owner -- this is the pre-fix impossibility;
3. the copy finishes ``completed`` and the key is gone afterwards (the lease is
   released, so a later pass can settle a genuinely dead owner).

The tree is sized to stay under the worker call's own 120 s timeout (2000 files
measured ~76 s), because a copy that dies of the timeout would prove nothing
about the lease. Credentials come from the cluster Secret and are never printed.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

NS = "sandlock"
FILES = int(os.environ.get("TREE_FILES", "2000"))
KEY = os.environ["E2B_API_KEY"]

MAKE_TREE = f"""
python3 - <<'PY'
import os
root = "/home/user/lease"
os.makedirs(root, exist_ok=True)
for i in range({FILES}):
    with open(f"{{root}}/f{{i:04d}}.bin", "wb") as fh:
        fh.write(b"x" * 512)
PY
"""


def sh(*args: str) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"command failed: {' '.join(args)}")
    return proc.stdout


def redis(*args: str) -> str:
    password = base64.b64decode(
        sh("kubectl", "-n", NS, "get", "secret", "e2b-secrets",
           "-o", "jsonpath={.data.E2B_REDIS_PASSWORD}")
    ).decode()
    pod = sh("kubectl", "-n", NS, "get", "pod", "-l", "app=redis", "-o", "name")
    pod = pod.strip().splitlines()[0]
    return sh("kubectl", "-n", NS, "exec", pod, "--", "redis-cli", "-a", password,
              "--no-auth-warning", *args)


def http(
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    url = os.environ["E2B_API_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "X-API-Key": KEY,
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def main() -> int:
    from e2b import Sandbox

    sandbox = Sandbox.create(timeout=1800)
    sandbox_id = sandbox.sandbox_id
    try:
        print(f"sandbox {sandbox_id}")
        sandbox.commands.run(MAKE_TREE, timeout=600)
        print(f"tree of {FILES} files written")

        # `Prefer: respond-async` matters twice over: without it this is the
        # *synchronous* path (the copy answers only when it is done -- >60 s for
        # the tree above), and it is also the shape whose lease N46 is about.
        code, body = http(
            "POST",
            f"/sandboxes/{sandbox_id}/snapshots",
            {"name": "lease-probe"},
            {"Prefer": "respond-async"},
        )
        print(f"trigger: {code} {body[:160]}")
        if code != 202:
            print("N46 LEASE PROBE FAIL: the async trigger did not answer 202")
            return 1
        snapshot_id = json.loads(body)["snapshotID"]

        deadline = time.monotonic() + 90
        lease_seen, lease_value, status = False, "", ""
        while time.monotonic() < deadline:
            value = redis("GET", f"e2b:snapshot:copy:{snapshot_id}").strip()
            if value and value != "(nil)":
                lease_seen, lease_value = True, value
            _, record = http("GET", f"/snapshots/{snapshot_id}")
            status = json.loads(record).get("status", "")
            if status != "creating":
                break
            time.sleep(1)

        print(f"lease held during the copy: {lease_seen} value={lease_value!r}")
        print(f"final status: {status}")
        after = redis("GET", f"e2b:snapshot:copy:{snapshot_id}").strip()
        print(f"lease after completion: {after!r}")

        if not lease_seen:
            print("N46 LEASE PROBE FAIL: an unnamed async copy held no lease")
            return 1
        if status != "completed":
            print(f"N46 LEASE PROBE FAIL: the copy ended {status}")
            return 1
        if after and after != "(nil)":
            print("N46 LEASE PROBE FAIL: the lease outlived the copy")
            return 1
        print("N46 LEASE PROBE OK: unnamed async copy held and released a lease")
        return 0
    finally:
        print(f"kill rc={sandbox.kill()}")


if __name__ == "__main__":
    sys.exit(main())
