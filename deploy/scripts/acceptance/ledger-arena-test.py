"""Is the 72 MiB/thread cost glibc's per-thread malloc arena?

If yes, MALLOC_ARENA_MAX=1 removes the reservation (all threads share the
main arena) and the charge collapses.
"""

from e2b import Sandbox

LIMIT_MB = 512
REPORT = "print('USED_KB', int(open('/proc/meminfo').read().split('MemFree:')[1].split()[0]))"
THREE_THREADS = (
    "import threading, time\n"
    "[threading.Thread(target=lambda: time.sleep(30), daemon=True).start() for _ in range(3)]\n"
    "time.sleep(30)"
)
MCP_ONE_THREAD = (
    "from mcp.server.mcpserver import MCPServer\n"
    "import threading, time\n"
    "threading.Thread(target=lambda: time.sleep(30), daemon=True).start()\n"
    "time.sleep(30)"
)

CASES = [
    ("bare", "pass", ""),
    ("3 threads (default arenas)", THREE_THREADS, ""),
    ("3 threads (MALLOC_ARENA_MAX=1)", THREE_THREADS, "MALLOC_ARENA_MAX=1 "),
    ("mcp + 1 thread (default)", MCP_ONE_THREAD, ""),
    ("mcp + 1 thread (arena=1)", MCP_ONE_THREAD, "MALLOC_ARENA_MAX=1 "),
]

sb = Sandbox.create()
try:
    base = None
    for name, script, prefix in CASES:
        cmd = prefix + "python3 -c \"" + script + "\n" + REPORT + "\""
        res = sb.commands.run(cmd, timeout=60)
        line = [l for l in (res.stdout or "").splitlines() if l.startswith("USED_KB")][-1]
        used = LIMIT_MB - int(line.split()[1]) / 1024
        if base is None:
            base = used
        print(f"  {name:<32s} used={used:7.1f} MiB  delta={used - base:+7.1f}")
finally:
    sb.kill()
