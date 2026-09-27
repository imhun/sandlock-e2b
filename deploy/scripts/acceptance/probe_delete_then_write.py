"""Can one command free space and then write, while the sandbox is full?

The pattern is ordinary -- `rm -rf cache && mkdir cache && ...`, log
rotation, a build that cleans first. This measures it directly: fill the
sandbox to its budget, then run a *single* command that deletes the biggest
file and immediately writes a new one, and finally check whether the same
sequence works from a *second* command (after the accounting round noticed).
"""

import os
import sys
import time

from e2b import Sandbox

API = os.environ["E2B_API_URL"]
MIB = 1024 * 1024


def main() -> int:
    sb = Sandbox.create(timeout=1800)
    print("sandbox", sb.sandbox_id, flush=True)

    fill = sb.commands.run(
        "dd if=/dev/zero of=/home/user/fill.bin bs=1M count=1024 status=none"
    )
    print(f"  fill rc={fill.exit_code}", flush=True)
    time.sleep(4)

    # One command: delete 900 MiB, then write a small file.
    one = sb.commands.run(
        "rm -f /home/user/fill.bin; "
        "dd if=/dev/zero of=/home/user/after.bin bs=1M count=1 status=none; "
        "echo rc=$?",
        timeout=180,
    )
    print(f"  one command: {one.stdout.strip()!r}", flush=True)
    print(f"  after.bin size: "
          f"{sb.commands.run('stat -c %s /home/user/after.bin 2>/dev/null || echo missing').stdout.strip()}",
          flush=True)

    # And after the accounting has caught up (a second command).
    two = sb.commands.run(
        "dd if=/dev/zero of=/home/user/second.bin bs=1M count=1 status=none; "
        "echo rc=$?",
        timeout=180,
    )
    print(f"  second command: {two.stdout.strip()!r}", flush=True)
    sb.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
