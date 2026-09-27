"""Repeat the exact shape of `probe_exec_limit` step 1b, and measure the gap.

Fill to 700 MiB, let it settle, write one 1024 MiB file (which lands whatever
is left, ~324 MiB), then *immediately* write a second file with no wait in
between. The second file must land 0 bytes. Each iteration reports what it
landed, so a race shows up as a distribution instead of a single run.
"""

import sys
import time

from e2b import Sandbox

MIB = 1024 * 1024


def one_iteration(index: int) -> int:
    sb = Sandbox.create(timeout=1800)
    try:
        sb.commands.run(
            "dd if=/dev/zero of=/home/user/fill.bin bs=1M count=700 status=none",
            timeout=900,
        )
        time.sleep(8)
        first = sb.commands.run(
            "dd if=/dev/zero of=/home/user/one.bin bs=1M count=1024 2>&1 | tail -1; "
            "stat -c %s /home/user/one.bin",
            timeout=900,
        )
        landed_one = int(first.stdout.strip().splitlines()[-1])
        second = sb.commands.run(
            "dd if=/dev/zero of=/home/user/again.bin bs=1M count=900 2>&1 | tail -1; "
            "stat -c %s /home/user/again.bin 2>/dev/null || echo 0",
            timeout=900,
        )
        lines = [line for line in second.stdout.splitlines() if line.strip()]
        landed_two = int(lines[-1])
        print(
            f"  {index}: one.bin={landed_one / MIB:.1f} MiB  "
            f"again.bin={landed_two / MIB:.1f} MiB  ({lines[0][:48]})",
            flush=True,
        )
        print(f"      first: {first.stdout.strip()[:60]}", flush=True)
        return landed_two
    finally:
        sb.kill()


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    over = 0
    for index in range(1, rounds + 1):
        landed = one_iteration(index)
        if landed > 8 * MIB:
            over += 1
    print(f"\n{over}/{rounds} iterations let a second file past the budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
