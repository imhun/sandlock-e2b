#!/usr/bin/env python3
"""Deployment-level multi-node smoke against the separated container images.

Usage (after ``docker compose -f docker-compose.prod.yml up -d --no-build``):

    E2B_API_URL=http://127.0.0.1:3000 \\
    E2B_SANDBOX_URL=http://127.0.0.1:3000 \\
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
    volumes: list = []
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

        # Free the global quota (4 x 100% CPU = the 400% cap) before the
        # volume/template sections create more sandboxes.
        for sb in sandboxes:
            try:
                sb.kill()
            except Exception:
                pass
        sandboxes.clear()

        # 4. Volume mount through the shared store + sibling-volume isolation.
        os.environ["E2B_VOLUME_API_URL"] = api_url
        from e2b import Volume

        vol = Volume.create(
            "smoke-vol",
            api_url=api_url,
            api_key=os.environ["E2B_API_KEY"],
        )
        sibling = Volume.create(
            "smoke-sibling",
            api_url=api_url,
            api_key=os.environ["E2B_API_KEY"],
        )
        volumes.extend([vol, sibling])
        vol.write_file("own.txt", b"volume-data")
        sibling.write_file("secret.txt", b"top-secret")

        mounted = Sandbox.create(
            volume_mounts={"mnt/data": vol.volume_id}, **_sdk_opts(sandbox_url)
        )
        sandboxes.append(mounted)
        assert mounted.commands.run("cat mnt/data/own.txt").stdout == "volume-data"
        # The sibling volume lives next to the mounted one under the shared
        # root but is NOT mounted into this sandbox: it must stay unreachable
        # (sandlock fs isolation), even via the worker's absolute path.
        iso = mounted.commands.run(
            "if cat /var/lib/e2b-sandboxes/_volumes/"
            f"{sibling.volume_id}/secret.txt 2>/dev/null; "
            "then echo LEAKED; else echo ISOLATED; fi"
        )
        assert "ISOLATED" in iso.stdout
        assert "top-secret" not in iso.stdout
        print("OK: volume mounted remotely + sibling volume isolated")

        # 5. Template build -> registry push -> worker OCI pull -> image
        # rootfs execution (full distribution path, no local daemon).
        from e2b import Template

        template = Template().from_dockerfile(
            "FROM python:3.11-slim\nRUN echo smoke-template > /smoke-marker"
        )
        info = Template.build(
            template,
            "smoke-template",
            api_url=api_url,
            api_key=os.environ["E2B_API_KEY"],
        )
        assert info.template_id.startswith("tpl_")
        tpl_sb = Sandbox.create(
            template="smoke-template", **_sdk_opts(sandbox_url)
        )
        sandboxes.append(tpl_sb)
        result = tpl_sb.commands.run("cat /smoke-marker")
        assert result.stdout == "smoke-template\n"
        print("OK: template built -> registry push -> worker pull -> image rootfs")

        # 6. MCP: gateway inside the sandbox, streamable HTTP through the
        # proxy (per-sandbox port, session headers forwarded).
        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from httpx2 import AsyncClient

        echo_server = (
            "import asyncio\n"
            "from mcp.server.mcpserver import MCPServer\n"
            "server = MCPServer('echo', version='1.0.0')\n"
            "@server.tool()\n"
            "async def echo(text: str) -> str:\n"
            "    return f'echo:{text}'\n"
            "server.run(transport='stdio')"
        )
        mcp_sb = Sandbox.create(
            mcp={"name": "echo", "command": "python3", "args": ["-c", echo_server]},
            **_sdk_opts(sandbox_url),
        )
        sandboxes.append(mcp_sb)
        token = mcp_sb.get_mcp_token()
        assert token
        mcp_url = f"{sandbox_url.rstrip('/')}/mcp"
        mcp_headers = {
            "E2b-Sandbox-Id": mcp_sb.sandbox_id,
            "Authorization": f"Bearer {token}",
        }

        # The gateway starts as a background sandbox process; wait for the
        # proxy route to answer before driving the MCP session.
        import httpx as _httpx

        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                probe = _httpx.get(mcp_url, headers=mcp_headers, timeout=2)
                if probe.status_code < 500:
                    break
            except _httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise AssertionError("mcp-gateway did not start listening")

        async def _mcp_call() -> str:
            async with AsyncClient(headers=mcp_headers) as http:
                async with streamable_http_client(mcp_url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert [t.name for t in tools.tools] == ["echo"]
                        result = await session.call_tool("echo", {"text": "hello"})
                        return result.content[0].text

        assert asyncio.run(_mcp_call()) == "echo:hello"
        print("OK: MCP gateway inside sandbox + streamable HTTP through proxy")
    finally:
        for sb in sandboxes:
            try:
                sb.kill()
            except Exception:
                pass
        for v in volumes:
            try:
                Volume.destroy(
                    v.volume_id,
                    api_url=api_url,
                    api_key=os.environ["E2B_API_KEY"],
                )
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
