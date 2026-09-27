"""What does each kind of thing cost in the sandlock ledger, in a plain box?

Every step is one foreground process that reports the box's own ledger
(MemFree = limit - mem_used), so the deltas are that process's charge.
"""

from e2b import Sandbox

LIMIT_MB = 512
REPORT = "print('USED_KB', int(open('/proc/meminfo').read().split('MemFree:')[1].split()[0]))"

CASES = {
    "bare python": "pass",
    "bare python + 1 thread": (
        "import threading, time\n"
        "threading.Thread(target=lambda: time.sleep(30), daemon=True).start()\n"
        "time.sleep(30)"
    ),
    "bare python + 3 threads": (
        "import threading, time\n"
        "[threading.Thread(target=lambda: time.sleep(30), daemon=True).start() for _ in range(3)]\n"
        "time.sleep(30)"
    ),
    "import mcp": "from mcp.server.mcpserver import MCPServer\nimport time\ntime.sleep(30)",
    "import mcp + 1 thread": (
        "from mcp.server.mcpserver import MCPServer\n"
        "import threading, time\n"
        "threading.Thread(target=lambda: time.sleep(30), daemon=True).start()\n"
        "time.sleep(30)"
    ),
    "import uvicorn": "import uvicorn\nimport time\ntime.sleep(30)",
    "50MiB touched": (
        "import time\n"
        "buf = bytearray(50 * 1024 * 1024)\n"
        "for i in range(0, len(buf), 4096): buf[i] = 1\n"
        "time.sleep(30)"
    ),
}

sb = Sandbox.create()
try:
    base = None
    for name, script in CASES.items():
        cmd = "python3 -c \"" + script + "\n" + REPORT + "\""
        res = sb.commands.run(cmd, timeout=60)
        line = [l for l in (res.stdout or "").splitlines() if l.startswith("USED_KB")][-1]
        used = LIMIT_MB - int(line.split()[1]) / 1024
        if base is None:
            base = used
        print(f"  {name:<26s} used={used:7.1f} MiB  delta_vs_bare={used - base:+7.1f}")
finally:
    sb.kill()
