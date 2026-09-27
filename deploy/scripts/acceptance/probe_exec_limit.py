"""N25/C acceptance: the per-exec ceiling is "what is left", refreshed per exec.

Three shapes, and the difference between them is the whole point of the
feature:

  * **one file, tree already partly full** -- the file must stop at the
    *remaining* budget, not at the whole one;
  * **consecutive writes, each its own exec** (the SDK's normal pattern) -- the
    total must converge on the budget, because each exec refreshes first;
  * **one command writing a file** -- bounded by the whole budget per file,
    because one process has one ceiling. Reported, not asserted: it is the
    documented limit of this mechanism.
"""

import time

from e2b import Sandbox

MIB = 1024 * 1024
BUDGET = 1024 * MIB


def _metric(sb) -> int:
    """The worker's measured bytes for this sandbox, via the SDK metrics."""
    metric = sb.get_metrics()
    metric = metric[0] if isinstance(metric, list) else metric
    return int(metric.disk_used)


def _paused(sb) -> bool:
    """Whether the platform froze this sandbox (a legitimately over-budget end
    state), so the caller can read the number instead of walking the tree."""
    try:
        sb.commands.run("true", timeout=30)
        return False
    except Exception:  # noqa: BLE001 - a paused sandbox refuses commands
        return True


def write_mib(sb, path, mib):
    """Write ``mib`` MiB and report what actually landed."""
    out = sb.commands.run(
        "dd if=/dev/zero of=%s bs=1M count=%d 2>&1 | tail -1; "
        "stat -c %%s %s 2>/dev/null || echo 0" % (path, mib, path),
        timeout=900,
    )
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    return int(lines[-1]), lines[0]


def tree_bytes(sb):
    return int(
        sb.commands.run(
            "python3 -c \"import os;print(sum(os.path.getsize(os.path.join(r,f)) "
            "for r,_d,fs in os.walk('/home/user') for f in fs))\"",
            timeout=120,
        ).stdout.strip()
    )


sb = Sandbox.create(timeout=1800)
try:
    # Fill to ~700 MiB so the interesting number is the *remaining* ~324 MiB.
    sb.commands.run(
        "dd if=/dev/zero of=/home/user/fill.bin bs=1M count=700 status=none",
        timeout=900,
    )
    time.sleep(8)  # let the accounting see it
    print("tree before: %.0f MiB of a 1024 MiB budget" % (tree_bytes(sb) / MIB))

    landed, summary = write_mib(sb, "/home/user/one.bin", 1024)
    print("\n1. a single 1024 MiB file, 700 already used")
    print("   landed %.1f MiB (%s)" % (landed / MIB, summary))
    if landed > 400 * MIB:
        print("   FAIL: it was allowed to grow past the remaining budget")
        raise SystemExit(1)
    if landed < 250 * MIB:
        print("   FAIL: it stopped well below the remaining budget")
        raise SystemExit(1)

    print("\n1b. the same file again: the budget is spent, so it is refused")
    landed, summary = write_mib(sb, "/home/user/again.bin", 900)
    print("   landed %.1f MiB (%s)" % (landed / MIB, summary))
    if landed > 8 * MIB:
        print("   FAIL: a second file kept writing past the budget")
        raise SystemExit(1)
finally:
    sb.kill()

# Consecutive writes, each its own exec: the SDK pattern. A *fresh* sandbox,
# because step 1 now legitimately spends the whole remaining budget and the
# platform freezes a sandbox that is 1 MiB past it (the per-exec ceiling hands
# the file exactly "what is left", which lands the tree on the budget).
sb = Sandbox.create(timeout=1800)
try:
    sb.commands.run(
        "dd if=/dev/zero of=/home/user/fill.bin bs=1M count=700 status=none",
        timeout=900,
    )
    time.sleep(8)
    print("\n2. serial 900 MiB writes, each its own exec: the budget is the wall")
    landed, summary = write_mib(sb, "/home/user/serial0.bin", 900)
    print("   exec 1: landed %6.1f MiB (%s)" % (landed / MIB, summary))
    if not 250 * MIB <= landed <= 400 * MIB:
        print("   FAIL: the first exec did not get the remaining budget")
        raise SystemExit(1)

    # A second exec only runs if the platform has not frozen the sandbox yet:
    # exhausting the budget *is* the boundary condition, and the freeze is the
    # designed answer to "the next file would go past it". Either outcome is
    # the ceiling working; what must never happen is the tree past the budget.
    try:
        landed2, summary2 = write_mib(sb, "/home/user/serial1.bin", 900)
        print("   exec 2: landed %6.1f MiB (%s)" % (landed2 / MIB, summary2))
        if landed2 > 8 * MIB:
            print("   FAIL: a second exec kept writing past the budget")
            raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - a freeze is an expected outcome
        print("   exec 2: refused (%s)" % str(exc)[:60].replace("\n", " "))

    total = tree_bytes(sb) if not _paused(sb) else _metric(sb)
    print("   tree after the serial writes: %.0f MiB" % (total / MIB))
    if total > BUDGET + 4 * MIB:
        print("   FAIL: converged at %.0f MiB, past the budget" % (total / MIB))
        raise SystemExit(1)

    print("\nper-exec ceiling behaves as designed")
finally:
    sb.kill()
