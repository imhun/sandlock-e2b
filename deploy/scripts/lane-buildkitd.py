"""Start the lane's one long-lived buildkitd, or report the existing one.

The suite's ``buildkitd`` fixture starts a rootless buildkit container per
pytest *process* (container create + readiness poll; up to 90-120 s in its
failure paths). The lane runs several processes per run, so it creates the
daemon once, with the name, volume and port pinned, and every later process
reuses it through ``E2B_TEST_BUILDKIT_ADDR`` (``tests/conftest.py``).

Prints the daemon's TCP address on the last line; the container and its volume
are left running for the next run.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/workspace")

import tests.conftest as suite  # noqa: E402  (path first, on purpose)


def main() -> int:
    name = os.environ["E2B_TEST_BUILDKIT_NAME"]
    volume = os.environ["E2B_TEST_BUILDKIT_VOLUME"]
    port = os.environ["E2B_TEST_BUILDKIT_PORT"]

    running = suite.subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
    )
    if running.returncode == 0 and running.stdout.strip() == "true":
        if suite._port_accepting(int(port)):
            print(f"tcp://127.0.0.1:{port}")
            return 0

    address, _container, _cfg_dir = suite._start_buildkitd()
    print(address)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
