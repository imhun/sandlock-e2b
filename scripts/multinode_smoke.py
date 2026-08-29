#!/usr/bin/env python3
"""Multi-node smoke test against a live compose deployment.

Usage (after ``docker compose -f docker-compose.multinode.yml up -d``):

    E2B_API_URL=http://127.0.0.1:3100 \\
    E2B_SANDBOX_URL=http://127.0.0.1:4100 \\
    E2B_API_KEY=local-key \\
    python scripts/multinode_smoke.py

Verifies: sandbox spread across nodes, commands/files/health/stdin through
the envd gateway, and per-node quota release after kill.
"""

from __future__ import annotations

import collections
import os
import sys
import time

import httpx


def main() -> None:
    api_url = os.environ["E2B_API_URL"]
    sandbox_url = os.environ["E2B_SANDBOX_URL"]
    os.environ.setdefault("E2B_API_KEY", "local-key")
    internal_key = os.environ.get("E2B_INTERNAL_API_KEY", "internal-key")

    from e2b import Sandbox

    def route_of(sandbox_id: str) -> str:
        resp = httpx.get(
            f"{api_url}/internal/routes/{sandbox_id}",
            headers={"X-Internal-Key": internal_key},
        )
        resp.raise_for_status()
        return resp.json()["address"]

    sandboxes = []
    try:
        for _ in range(4):
            sandboxes.append(Sandbox.create())
        time.sleep(1)
        dist = collections.Counter(route_of(sb.sandbox_id) for sb in sandboxes)
        print("NODE DISTRIBUTION:", dict(dist))
        assert len(dist) >= 2, "sandboxes should spread across multiple workers"

        for i, sb in enumerate(sandboxes):
            result = sb.commands.run(f"echo node-{i}-ok")
            assert result.stdout == f"node-{i}-ok\n", result.stdout
            sb.files.write(f"workspace/f{i}.txt", f"data-{i}")
            assert sb.files.read(f"workspace/f{i}.txt") == f"data-{i}"
            assert sb.is_running() is True
        print("ALL sandboxes: commands + files + health through gateway OK")

        proc = sandboxes[0].commands.run("cat", stdin=True, background=True)
        proc.send_stdin("multi-node-stdin\n")
        proc.close_stdin()
        result = proc.wait()
        assert result.stdout == "multi-node-stdin\n", result.stdout
        print("stdin through gateway OK")
    finally:
        for sb in sandboxes:
            sb.kill()
        time.sleep(1)
        nodes = httpx.get(
            f"{api_url}/internal/nodes", headers={"X-Internal-Key": internal_key}
        ).json()
        reserved = [
            (n["nodeID"], n["reservedMemoryMB"])
            for n in nodes
            if n["address"] != "local://"
        ]
        print("after kill reservations:", reserved)
        assert all(r == 0 for _, r in reserved), reserved
    print("MULTI-NODE SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

