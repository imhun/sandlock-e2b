#!/usr/bin/env python3
"""Deployment-level multi-node smoke against the separated container images.

Usage (after ``docker compose -f docker-compose.prod.yml up -d --no-build``):

    E2B_API_URL=http://127.0.0.1:3000 \\
    E2B_SANDBOX_URL=http://127.0.0.1:49983 \\
    E2B_API_KEY=local-key \\
    python scripts/deployment_smoke.py

Covers what ``multinode_smoke.py`` does plus: filesystem migration across
workers (shared workspace => route switch only), network config echo/update,
and per-node quota release after kill.
"""

from __future__ import annotations

import os
import sys
import time

import httpx


def main() -> int:
    api_url = os.environ["E2B_API_URL"]
    sandbox_url = os.environ["E2B_SANDBOX_URL"]
    os.environ.setdefault("E2B_API_KEY", "local-key")
    internal_key = os.environ.get("E2B_INTERNAL_API_KEY", "internal-key")

    from e2b import Sandbox

    def route_of(sandbox_id: str) -> dict:
        resp = httpx.get(
            f"{api_url}/internal/routes/{sandbox_id}",
            headers={"X-Internal-Key": internal_key},
        )
        resp.raise_for_status()
        return resp.json()

    def nodes_state() -> dict:
        resp = httpx.get(
            f"{api_url}/internal/nodes",
            headers={"X-Internal-Key": internal_key},
        )
        resp.raise_for_status()
        return {
            n["nodeID"]: n["reservedMemoryMB"]
            for n in resp.json()
            if n["address"] != "local://"
        }

    sandboxes: list[Sandbox] = []
    try:
        # 1. Spread + commands + files through the gateway.
        for i in range(3):
            sandboxes.append(Sandbox.create())
        time.sleep(1)
        routes = {sb.sandbox_id: route_of(sb.sandbox_id) for sb in sandboxes}
        nodes = {r["nodeID"] for r in routes.values()}
        print("NODE DISTRIBUTION:", {r["address"] for r in routes.values()})
        assert len(nodes) >= 2, "sandboxes should spread across multiple workers"

        for i, sb in enumerate(sandboxes):
            result = sb.commands.run(f"echo deploy-{i}-ok")
            assert result.stdout == f"deploy-{i}-ok\n", result.stdout
            sb.files.write(f"workspace/deploy-{i}.txt", f"data-{i}")
            assert sb.files.read(f"workspace/deploy-{i}.txt") == f"data-{i}"
        print("OK: commands + files through gateway")

        # 2. Filesystem migration: route switches to another worker and the
        # shared workspace keeps the file (no archive transfer).
        target = sandboxes[0]
        before = route_of(target.sandbox_id)
        other = next(
            node_id
            for node_id, addr in {
                r["nodeID"]: r["address"] for r in routes.values()
            }.items()
            if node_id != before["nodeID"]
        )
        migrated = httpx.post(
            f"{api_url}/sandboxes/{target.sandbox_id}/migrate",
            headers={"X-API-Key": os.environ["E2B_API_KEY"]},
            json={"nodeID": other},
        )
        assert migrated.status_code == 200, migrated.text
        after = route_of(target.sandbox_id)
        assert after["nodeID"] == other, (before, after)
        # The shared workspace keeps the file after the route switch.
        assert target.files.read("workspace/deploy-0.txt") == "data-0"
        print(f"OK: migrated {before['nodeID']} -> {after['nodeID']}, files kept")

        # 3. Network API: echo, then a dynamic update is applied on the node.
        net = Sandbox.create(
            network={"allow_out": ["example.com"]}, **_sdk_opts(sandbox_url)
        )
        sandboxes.append(net)
        detail = httpx.get(
            f"{api_url}/sandboxes/{net.sandbox_id}",
            headers={"X-API-Key": os.environ["E2B_API_KEY"]},
        ).json()
        assert detail["network"]["allowOut"] == ["example.com"], detail["network"]
        net.update_network({"allow_internet_access": False})
        detail = httpx.get(
            f"{api_url}/sandboxes/{net.sandbox_id}",
            headers={"X-API-Key": os.environ["E2B_API_KEY"]},
        ).json()
        assert detail["network"].get("allowOut") is None
        assert detail["allowInternetAccess"] is False
        print("OK: network config echo + atomic update")
    finally:
        for sb in sandboxes:
            try:
                sb.kill()
            except Exception:
                pass
        time.sleep(1)
        reserved = nodes_state()
        print("after kill reservations:", reserved)
        assert all(r == 0 for r in reserved.values()), reserved
    print("DEPLOYMENT SMOKE OK")
    return 0


def _sdk_opts(sandbox_url: str) -> dict:
    return {
        "sandbox_url": sandbox_url,
        "api_key": os.environ["E2B_API_KEY"],
    }


if __name__ == "__main__":
    sys.exit(main())
