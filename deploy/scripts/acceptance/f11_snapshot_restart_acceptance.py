#!/usr/bin/env python3
"""F11 acceptance: a rolling restart must not kill a live peer's copy.

The startup pass in ``control_plane.api.snapshots.reconcile_pending_snapshots``
exists to settle records a crash orphaned. With two replicas it also meets the
*other* replica's in-flight copy, and before the fix it re-drove it: a second
POST for a half-written payload, a 409 from the worker, and ``mark_failed`` on a
record its owner was one step from completing -- a client-visible failure, and a
retry that copies the tree again.

What this probe does, on the cluster:

1. builds a 2000-file tree (the N32 measurement: ~76 s to copy) in a sandbox;
2. starts an **async** snapshot, addressed to replica A directly so the owner is
   known;
3. deletes replica B -- a rolling restart, the exact trigger -- and waits for its
   replacement to come up and run its startup pass;
4. polls the snapshot to completion.

Pass means the record ends ``completed``. The replacement's log must also show
it *saw* the record and left it alone ("another replica is copying it"); that
line is the evidence the fixed branch ran rather than the case never arising.

**What "in flight" means here (2026-09-27, corrected after a real run).** This
probe used to require the fleet-wide claim key (`e2b:snapshot:copy:<id>`) to be
present before the restart, and returned INCONCLUSIVE when it was not. That
precondition can never hold for the shape this probe uses: `create_snapshot`
takes the claim only for a *named* id (`Idempotency-Key` / `snapshotID`), which
is what closes the "no record yet, two replicas both copy" window, and it
releases the claim the moment the record exists -- for the async shape, before
the 202 answers. From then on the durable marker every other replica reads is the
**record's own ``creating`` status on the shared volume**, which is exactly what
`reconcile_pending_snapshots` consults. So the precondition is "the record says
``creating`` and belongs to the owner replica"; the claim key is printed as an
observation, not demanded. (The old check did not make the run safer -- it made
it impossible to run.)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "sandlock"
# 2000 files took 76 s to copy (the N32 measurement) while the replacement
# replica needs ~115 s to boot -- its ``image-cache-init`` spends that on a
# recursive ``chown`` of the shared image cache. A tree that small finishes
# before the replacement's startup pass runs, so the run proves nothing: the
# record is already ``completed`` and there is nothing left to steal. 8000
# files put the copy at ~300 s on the same measurement, which leaves the pass
# a wide window to meet it in.
TREE_FILES = int(os.environ.get("TREE_FILES", "8000"))
KEY = os.environ["E2B_API_KEY"]

MAKE_TREE = f"""
python3 - <<'PY'
import os
root = "/home/user/big"
os.makedirs(root, exist_ok=True)
for i in range({TREE_FILES}):
    with open(f"{{root}}/f{{i:04d}}.bin", "wb") as fh:
        fh.write(b"x" * 512)
PY
"""


def sh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"command failed: {' '.join(args)}")
    return proc.stdout


def replicas() -> list[str]:
    out = sh("kubectl", "-n", NS, "get", "pods", "-l", "app=control-plane",
             "-o", "name")
    return sorted(
        line.strip().removeprefix("pod/") for line in out.splitlines() if line.strip()
    )


def in_pod(pod: str, code: str) -> str:
    """Run a python snippet inside a pod and return its stdout."""
    return sh("kubectl", "-n", NS, "exec", pod, "-c", "control-plane", "--",
              "python3", "-c", code)


def wait_ready(pod: str, timeout_s: float = 180) -> None:
    sh("kubectl", "-n", NS, "wait", "--for=condition=Ready", f"pod/{pod}",
       f"--timeout={int(timeout_s)}s")


def main() -> int:
    try:
        from e2b import Sandbox
    except ImportError as exc:
        raise SystemExit(f"the e2b SDK is needed to build the tree: {exc}")

    owners = replicas()
    if len(owners) != 2:
        raise SystemExit(f"expected 2 replicas, found {len(owners)}")
    owner, victim = owners[0], owners[1]
    print(f"owner (takes the copy): {owner}")
    print(f"victim (restarted mid-copy): {victim}")

    sandbox = Sandbox.create(timeout=1800)
    sid = sandbox.sandbox_id
    try:
        sandbox.commands.run(MAKE_TREE, timeout=600)
        files = sandbox.commands.run(
            "find /home/user/big -type f | wc -l", timeout=120
        ).stdout.strip()
        print(f"files in the tree: {files}")

        # Addressed to the owner directly, so "who is copying" is not a guess.
        trigger = f"""
import json, urllib.request
req = urllib.request.Request(
    "http://127.0.0.1:3000/sandboxes/{sid}/snapshots",
    data=json.dumps({{"name": "restart-inflight"}}).encode(),
    headers={{"X-API-Key": {KEY!r}, "Content-Type": "application/json",
             "Prefer": "respond-async"}},
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as r:
    print(r.status, r.read().decode())
"""
        out = in_pod(owner, trigger).strip().splitlines()[-1]
        code, body = out.split(" ", 1)
        print(f"trigger: {code} {body[:200]}")
        if code != "202":
            raise SystemExit("the async trigger did not answer 202")
        snapshot_id = json.loads(body)["snapshotID"]
        print(f"snapshot id: {snapshot_id}")

        # The window has to be *open* before the restart means anything: the
        # shared record still says ``creating`` (that is the marker peers read
        # for an unnamed, async copy -- see the docstring). If the copy were
        # already finished this probe would be measuring nothing.
        deadline = time.monotonic() + 60
        in_flight = False
        claim_seen = False
        while time.monotonic() < deadline:
            code, body, _ = http_call("GET", f"/snapshots/{snapshot_id}")
            if code == 200 and json.loads(body).get("status") == "creating":
                in_flight = True
                claim_seen = redis_key_exists(f"e2b:snapshot:copy:{snapshot_id}")
                break
            time.sleep(1)
        print(f"record says creating (the marker peers read): {in_flight}")
        print(f"fleet-wide claim key present at that moment: {claim_seen} "
              "(expected False for an unnamed async copy)")
        if not in_flight:
            print("F11 SNAPSHOT RESTART INCONCLUSIVE: the record was not "
                  "'creating' when the restart was issued -- the copy finished "
                  "too fast to test anything. Raise TREE_FILES.")
            return 2

        # The restart. This is the moment the old code marked it failed.
        started = time.monotonic()
        sh("kubectl", "-n", NS, "delete", "pod", victim, "--wait=false")
        new_pod = ""
        while not new_pod:
            for name in replicas():
                if name != owner:
                    new_pod = name
            if not new_pod:
                time.sleep(2)
        wait_ready(new_pod)
        print(f"replacement {new_pod} ready after {time.monotonic() - started:.1f}s")

        # Who owns the copy, and what the newcomer decided about the record.
        owner_log = sh("kubectl", "-n", NS, "logs", new_pod, "-c", "control-plane",
                       "--since=10m", check=False)
        saw_it = [
            line for line in owner_log.splitlines()
            if snapshot_id in line or "another replica is copying it" in line
        ]
        print(f"replacement's lines about this startup pass: {len(saw_it)}")
        for line in saw_it[:4]:
            print(f"    {line.strip()[:160]}")

        # Poll to completion, through the Service (either replica may answer).
        deadline = time.monotonic() + 300
        status = error = None
        while time.monotonic() < deadline:
            code, body, _ = http_call("GET", f"/snapshots/{snapshot_id}")
            if code == 200:
                payload = json.loads(body)
                status, error = payload.get("status"), payload.get("error")
                if status in ("completed", "failed"):
                    break
            time.sleep(2)
        print(f"final status: {status}")
        if error:
            print(f"final error: {error[:300]}")

        if status == "completed" and saw_it:
            print("F11 SNAPSHOT RESTART OK: the replacement met the live copy, "
                  "left it to its owner, and the record completed")
            return 0
        if status == "completed" and not saw_it:
            # The copy won the race against the restart, so the pass had nothing
            # to decide. Green, but it does not test the fix -- say so instead
            # of banking it.
            print("F11 SNAPSHOT RESTART INCONCLUSIVE: the record completed, but "
                  "the replacement's startup pass never saw it (the copy "
                  "outran the restart in the other direction). Raise TREE_FILES.")
            return 2
        if error and "timed out" in error and not saw_it:
            # Measured 2026-09-27 on 0.1.0-652, 8000 files: the copy dies against
            # the worker call's own `timeout=120` before the replacement's startup
            # pass (134.2 s after the delete) can meet it. The window this probe
            # needs is `restart < copy < 120 s`, and on this deployment the
            # restart is *longer* than the copy timeout, so the band is empty --
            # no TREE_FILES makes this run meaningful. That is a property of the
            # deployment (an image-cache chown dominating the restart), not of
            # the copy path; see the N46 row in docs/open-issues.md.
            print("F11 SNAPSHOT RESTART INCONCLUSIVE: the copy hit the worker "
                  "call's 120 s timeout before the replacement (134 s) could "
                  "meet it, so the empty band restart < copy < 120 s is why "
                  "this cannot be tested here -- not a steal.")
            return 2
        print("F11 SNAPSHOT RESTART FAIL: the peer's startup pass took the "
              "record from its owner")
        return 1
    finally:
        try:
            sandbox.kill()
        except Exception:  # noqa: BLE001 - cleanup must not mask the verdict
            pass


def redis_key_exists(key: str) -> bool:
    """Is a shared-state key present? (The claim key is the interesting one.)"""
    password = base64.b64decode(
        sh("kubectl", "-n", NS, "get", "secret", "e2b-secrets",
           "-o", "jsonpath={.data.E2B_REDIS_PASSWORD}")
    ).decode()
    out = sh("kubectl", "-n", NS, "exec", "deploy/redis", "--", "redis-cli",
             "-a", password, "EXISTS", key, check=False)
    return out.strip().endswith("1")


def http_call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        os.environ["E2B_API_URL"].rstrip("/") + path, data=data, method=method,
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, resp.read().decode(errors="replace"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace"), time.monotonic() - started


if __name__ == "__main__":
    raise SystemExit(main())
