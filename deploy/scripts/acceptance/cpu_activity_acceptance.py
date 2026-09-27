#!/usr/bin/env python3
"""Cluster acceptance for E9.1 blind spot 2: a CPU-only sandbox is not idle.

`docs/resource-contention.md` §6 (option i): a sandbox that only burns CPU --
no requests, no egress -- used to look empty, so eviction would pause it and
release its reservation. The worker samples each sandbox's CPU (`/proc` summed
by owning uid) and marks it active above a percentage of one core; the mark
rides the existing `sandboxActivity` heartbeat.

Two properties of the *observable* shape the test, and both were learned the
hard way (see `docs/deploy-clusters.md`):

1. `GET /sandboxes` is a **read**, and reads are deliberately not activity.
   So the list view is a passive observer of this feature -- polling it can
   neither create the signal nor hide it.
2. That view is served from the shared store, and activity is written through
   to it at most every `E2B_ACTIVITY_PERSIST_INTERVAL_S` (default 30 s, and
   unset on this cluster). So `lastActiveAt` lags its true value by up to that
   interval, and a window shorter than it proves nothing.

Hence **two** windows, each longer than the write-through interval:

* sandbox A runs a tight CPU loop (one `commands.run`, then silence) and
  sandbox B is left completely alone after creation;
* window 1 lets A's *own* creation request -- which does count as activity --
  reach the store; the value read at the end of it is the baseline;
* window 2 has no requests at all, so anything that moves A's baseline can
  only be the CPU sampler. A must move; B must not, or nothing could ever be
  evicted.
"""

from __future__ import annotations

import json
import os
import time

import httpx

API = os.environ.get("E2B_API_URL", "http://172.18.78.49:3000")
KEY = os.environ["E2B_API_KEY"]

#: One window, twice. Must exceed `E2B_ACTIVITY_PERSIST_INTERVAL_S` (30 s here)
#: so that each window is guaranteed to contain a write-through, and several of
#: the worker's 5 s sampling intervals.
WINDOW_S = float(os.environ.get("CPU_ACCEPT_WINDOW_S", "45"))


def step(name: str, **fields) -> None:
    print(json.dumps({"step": name, **fields}, ensure_ascii=False), flush=True)


def last_active(sandbox_id: str) -> str:
    """`lastActiveAt` from the list view (a read: never counted as activity)."""
    resp = httpx.get(f"{API}/sandboxes", headers={"X-API-Key": KEY}, timeout=30)
    resp.raise_for_status()
    for record in resp.json():
        if record["sandboxID"] == sandbox_id:
            return str(record["lastActiveAt"])
    raise AssertionError(f"sandbox {sandbox_id} is not in the list view")


def main() -> int:
    from e2b import Sandbox

    burner = Sandbox.create(api_url=API, sandbox_url=API, api_key=KEY)
    quiet = Sandbox.create(api_url=API, sandbox_url=API, api_key=KEY)
    try:
        # One request, then silence: from here on the only thing that can keep
        # the sandbox out of the idle set is its own CPU time. `exec` so the
        # session child is python, and no output/stdio traffic afterwards.
        burner.commands.run(
            "exec python3 -c 'while True: pass' > /dev/null 2>&1",
            background=True,
        )
        time.sleep(2)
        created = {
            "burner": last_active(burner.sandbox_id),
            "quiet": last_active(quiet.sandbox_id),
        }
        # Window 1: the burn command's own request is activity, so the burner's
        # baseline here is expected to be *later* than its creation -- which is
        # exactly why window 2, and not this, is what proves the sampler.
        time.sleep(WINDOW_S)
        baseline = {
            "burner": last_active(burner.sandbox_id),
            "quiet": last_active(quiet.sandbox_id),
        }
        step(
            "baseline",
            burner_id=burner.sandbox_id,
            quiet_id=quiet.sandbox_id,
            seconds=WINDOW_S,
            created=created,
            baseline=baseline,
        )
        # Window 2: nothing at all happens but the reads this script makes.
        time.sleep(WINDOW_S)
        after = {
            "burner": last_active(burner.sandbox_id),
            "quiet": last_active(quiet.sandbox_id),
        }
        step("after_silent_window", seconds=WINDOW_S, **after)
        assert after["burner"] > baseline["burner"], (
            "the CPU sampler must keep a burning sandbox active: lastActiveAt "
            f"stayed at {baseline['burner']} through a {WINDOW_S}s window with "
            "no requests"
        )
        assert after["quiet"] == baseline["quiet"], (
            "a sandbox that did nothing must stay idle -- otherwise nothing can "
            f"ever be evicted: lastActiveAt moved to {after['quiet']}"
        )
        step(
            "OK",
            burner_baseline=baseline["burner"],
            burner_after=after["burner"],
            quiet_after=after["quiet"],
        )
        return 0
    finally:
        for sandbox in (burner, quiet):
            try:
                sandbox.kill()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
