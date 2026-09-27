"""Live: is the 512MB box a hard limit, and what does an MCP gateway cost?

Step 1 -- allocate 400 MiB (fits) and 700 MiB (must be killed) in a plain box.
Step 2 -- open boxes that each start an MCP gateway whose stdio server holds
          N MiB at import; report whether the box still serves commands.
"""

from e2b import Sandbox
from e2b.sandbox.commands.command_handle import CommandExitException

ALLOC = (
    "import time\n"
    "buf = bytearray(%d * 1024 * 1024)\n"
    "for i in range(0, len(buf), 4096): buf[i] = 1\n"
    "print('got %d', flush=True)\n"
    "time.sleep(5)\n"
)

MCP_SERVER = (
    "BUF = bytearray({n} * 1024 * 1024)\n"
    "for i in range(0, len(BUF), 4096): BUF[i] = 1\n"
    "from mcp.server.mcpserver import MCPServer\n"
    "server = MCPServer('echo', version='1.0.0')\n"
    "@server.tool()\n"
    "async def echo(text: str) -> str:\n"
    "    return f'echo:{text}'\n"
    "server.run(transport='stdio')\n"
)


def py(script: str) -> str:
    return f"python3 -c \"{script}\""


def step1() -> None:
    sb = Sandbox.create()
    print("box:", sb.sandbox_id)
    for mb in (400, 700):
        try:
            res = sb.commands.run(py(ALLOC % (mb, mb)))
            print(f"alloc {mb} MiB -> exit 0 stdout={(res.stdout or '').strip()}")
        except CommandExitException as exc:
            print(f"alloc {mb} MiB -> DENIED exit={exc.exit_code} stderr={((exc.stderr or '').strip())[:80]!r}")
    print("meminfo:", (sb.commands.run("grep MemTotal /proc/meminfo").stdout or "").strip())
    sb.kill()


def step2() -> None:
    for n in (0, 100, 200, 300, 450):
        try:
            sb = Sandbox.create(mcp={"name": "echo", "command": "python3", "args": ["-c", MCP_SERVER.format(n=n)]})
        except Exception as exc:  # noqa: BLE001
            print(f"server {n:>3} MiB -> create failed: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        try:
            try:
                res = sb.commands.run("ps -eo rss,args | grep -c '[p]ython3'")
                print(f"server {n:>3} MiB -> gateway alive, box commands ok ({res.stdout.strip()} procs)")
            except CommandExitException as exc:
                print(f"server {n:>3} MiB -> DEAD exit={exc.exit_code} stderr={((exc.stderr or '').strip())[:150]!r}")
        finally:
            try:
                sb.kill()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    step1()
    step2()
