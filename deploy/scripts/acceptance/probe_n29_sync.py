"""N29: what a client actually sees when a snapshot outlives the entry proxy.

The backlog says the client gets a 504 while the server finishes the copy, and
that a retry then gets 409 "already exists".  This probe drives the real path
(SDK for create/exec, raw HTTP for the snapshot endpoint so the status code is
visible) and records, in order:

* the first POST through the entry proxy -- status and wall time;
* the retry of the *same* request -- status and wall time;
* what the snapshot list says afterwards (so "did the server finish?" is a
  fact, not an inference).

Run with E2B_API_URL/E2B_SANDBOX_URL/E2B_API_KEY set, from the deploy host.
"""

import json
import os
import time
import urllib.error
import urllib.request

from e2b import Sandbox

BASE = os.environ["E2B_API_URL"].rstrip("/")
KEY = os.environ["E2B_API_KEY"]
ENTRY_TIMEOUT_S = 90

MAKE_TREE = r"""
python3 - <<'PY'
import os
root = "/home/user/big"
os.makedirs(root, exist_ok=True)
for i in range(2000):
    with open(f"{root}/f{i:04d}.bin", "wb") as fh:
        fh.write(b"x" * 512)
PY
"""


def call(method: str, path: str, body: dict | None = None, timeout: float = ENTRY_TIMEOUT_S):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace"), time.monotonic() - started
    except (urllib.error.URLError, OSError) as exc:
        return None, f"{exc.__class__.__name__}: {exc}", time.monotonic() - started


sb = Sandbox.create(timeout=1800)
try:
    sb.commands.run(MAKE_TREE, timeout=600)
    listed = sb.commands.run("find /home/user/big -type f | wc -l", timeout=120)
    print(f"files in the tree: {listed.stdout.strip()}")

    status, body, took = call(
        "POST", f"/sandboxes/{sb.sandbox_id}/snapshots", {"name": "n29-probe"}
    )
    print(f"1st POST  status={status} took={took:.1f}s body={body[:200]}")

    status2, body2, took2 = call(
        "POST", f"/sandboxes/{sb.sandbox_id}/snapshots", {"name": "n29-probe"}
    )
    print(f"retry     status={status2} took={took2:.1f}s body={body2[:200]}")

    status3, body3, took3 = call("GET", "/snapshots", timeout=30)
    try:
        items = json.loads(body3)
        names = [(s.get("snapshotID"), s.get("names")) for s in items]
    except Exception:
        names = body3[:300]
    print(f"snapshot list status={status3} ({took3:.1f}s): {names}")
finally:
    sb.kill()
