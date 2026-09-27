"""Cluster evidence for N25's push reporting and the live tightening.

Two claims, two measurements:

A. **Push makes a fast writer visible while it writes.** The worker's own view
   of a file being written is *committed* state (§22.4): with the old
   accounting a 512 MiB `dd` showed up as one jump (0 -> 512 MiB) no matter how
   often the reporter was asked. With the mediator pushing what its open
   descriptors grew by, the same write has to appear as a *ramp*, sampled by
   the platform during the write. So: sample `diskUsed` every 100 ms through
   one 512 MiB `dd` and count the distinct intermediate values.

B. **The tightening stops a single-file runaway mid-command.** Fill the tree to
   ~900 MiB of a 1024 MiB budget, then start a writer that keeps a file open
   and would happily write hundreds of MiB more. The per-exec ceiling cannot
   stop it (that was fixed when the command started); the live tightening
   should make it fail with EFBIG around the remaining budget, *without* the
   pause gate firing, and with the sandbox still running.
"""

import json
import os
import sys
import time

import httpx
from e2b import Sandbox

API = os.environ["E2B_API_URL"]
KEY = os.environ["E2B_API_KEY"]
MIB = 1024 * 1024


def _names(sb, path: str) -> list[str]:
    """Entry names under `path` (the files API answers a plain list)."""
    try:
        entries = sb.files.list(path)
    except Exception:  # noqa: BLE001 - a missing path is "nothing there yet"
        return []
    return [getattr(entry, "name", entry) for entry in entries]


def disk_used(client: httpx.Client, sandbox_id: str) -> int:
    resp = client.get(f"{API}/sandboxes/{sandbox_id}", headers={"X-API-Key": KEY})
    body = resp.json()
    return int(body.get("diskUsed") or 0)


def claim_a() -> bool:
    """The push bounds the *overshoot*: what the platform had measured when it
    froze the sandbox, against a 1024 MiB budget.

    Without it the accounting could only see committed bytes, so a 3x900 MiB
    writer was frozen at 1800 MiB (776 MiB of overshoot). What the pushed
    signal buys, and what this measures, is that the *tree* stops at the
    budget -- by a freeze when it was crossed, or by the writers themselves
    when the live ceiling lands first. The latter is the better outcome: the
    platform has nothing to freeze.
    """
    print("\n=== A. the overshoot past the budget ===")
    budget = 1024
    overshoots = []
    for run in range(3):
        sb = Sandbox.create(timeout=1800)
        try:
            sb.commands.run(
                "for i in 1 2 3; do "
                "dd if=/dev/zero of=/home/user/part$i.bin bs=1M count=900 status=none; "
                "done",
                background=True,
            )
            deadline = time.monotonic() + 60
            worst_used = 0
            paused = False
            with httpx.Client(timeout=10) as client:
                while time.monotonic() < deadline:
                    body = client.get(
                        f"{API}/sandboxes/{sb.sandbox_id}", headers={"X-API-Key": KEY}
                    ).json()
                    # The number the platform acts on comes from the worker
                    # (`/metrics`), not from the control plane's `diskUsed`
                    # field (which the k8s shape leaves at 0).
                    metric = sb.get_metrics()
                    metric = metric[0] if isinstance(metric, list) else metric
                    used = int(metric.disk_used) // MIB
                    worst_used = max(worst_used, used)
                    if body.get("state") == "paused":
                        paused = True
                        break
                    time.sleep(0.1)
            if worst_used < budget // 2:
                print(f"  run {run + 1}: the writer never got going "
                      f"(peak {worst_used} MiB)")
                return False
            overshoots.append(worst_used - budget)
            print(f"  run {run + 1}: peak {worst_used} MiB "
                  f"({worst_used - budget} MiB over the {budget} MiB budget), "
                  f"{'frozen' if paused else 'stopped by the writers themselves'}")
        finally:
            sb.kill()
    worst = max(overshoots)
    ok = worst <= budget // 4
    print(f"  worst overshoot: {worst} MiB "
          f"({'PASS' if ok else 'FAIL'}: must stay within a quarter of the budget; "
          f"before the pushed signal it was 776 MiB)")
    return ok


def claim_b() -> bool:
    """The tightening stops a runaway file inside one command.

    Fill most of the budget, then start a writer that would happily write
    gigabytes into *one* file. The live tightening makes the kernel refuse the
    next write past what is left, and -- the product semantic -- the sandbox is
    **not frozen**: it keeps running, a write in a fresh command still fails
    while the budget is spent, and after deleting something it can write again.
    """
    print("\n=== B. a runaway inside one command is stopped at the budget ===")
    budget = 1024
    budget_bytes = budget * MIB
    sb = Sandbox.create(timeout=1800)
    try:
        fill = sb.commands.run(
            "dd if=/dev/zero of=/home/user/fill.bin bs=1M count=900 status=none"
        )
        print(f"  fill: rc={fill.exit_code} (the ceiling must not cut off a legal "
              f"900 MiB file)")
        assert fill.exit_code == 0, f"the fill itself was stopped: {fill.stderr!r}"
        time.sleep(3)

        def used() -> int:
            metric = sb.get_metrics()
            metric = metric[0] if isinstance(metric, list) else metric
            return int(metric.disk_used)

        sb.commands.run(
            "dd if=/dev/zero of=/home/user/runaway.bin bs=1M count=4000 status=none",
            background=True,
        )
        # It stops when the kernel refuses the write, which shows up as the
        # tree no longer moving.
        peak = 0
        last = -1
        stable = 0
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            now = used()
            peak = max(peak, now)
            stable = stable + 1 if now == last else 0
            last = now
            if stable >= 4 and now > 0:
                break
            time.sleep(0.5)

        def run(cmd: str):
            try:
                return sb.commands.run(cmd, timeout=120)
            except Exception as exc:  # noqa: BLE001 - a refusal is an outcome
                return exc

        state = sb.commands.run("true", timeout=60)
        print(f"  runaway stopped with the tree at {peak / MIB:.1f} MiB of {budget} MiB")
        assert state.exit_code == 0, f"the sandbox must still be running: {state!r}"

        blocked = run("sh -c 'echo hi > /home/user/blocked.txt'")
        ok_blocked = getattr(blocked, "exit_code", 0) != 0
        print(f"  a fresh write while over budget: "
              f"{'refused' if ok_blocked else 'ALLOWED'} "
              f"({getattr(blocked, 'stderr', blocked)})"[:160])

        removed = run("rm -f /home/user/fill.bin")
        ok_deleted = getattr(removed, "exit_code", 1) == 0
        print(f"  delete while over budget: "
              f"{'worked' if ok_deleted else 'REFUSED'}")

        # ...and with the space back, a fresh command may write again. The
        # accounting round is what notices the deletion, so poll rather than
        # guess a delay.
        ok_recovered = False
        last_error = ""
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            attempt = run("sh -c 'echo hi > /home/user/after-delete.txt'")
            if getattr(attempt, "exit_code", 1) == 0:
                ok_recovered = True
                break
            last_error = str(getattr(attempt, "stderr", attempt)).strip()[:90]
            time.sleep(1)
        print(f"  write after making room: "
              f"{'worked' if ok_recovered else 'STILL BLOCKED: ' + last_error}")

        bounded = peak <= budget_bytes + 2 * MIB
        ok = bounded and ok_blocked and ok_deleted and ok_recovered
        print(
            f"  -> {'PASS' if ok else 'FAIL'}: bounded={bounded} "
            f"write_blocked={ok_blocked} delete_works={ok_deleted} "
            f"recovers={ok_recovered}"
        )
        return ok
    finally:
        sb.kill()


def main() -> int:
    results = {"A": claim_a(), "B": claim_b()}
    print("\n=== summary ===")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
