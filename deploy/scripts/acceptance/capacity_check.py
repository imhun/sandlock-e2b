"""Capacity + per-sandbox memory check.

Creates 8 sandboxes at once (the new fleet ceiling), prints the node each one
landed on and the memory limit the sandbox itself sees, then kills them.
"""

import os

import httpx
from e2b import Sandbox

LIMIT_CMD = (
    "python3 -c \"import os;p='/sys/fs/cgroup/memory.max';"
    "print('MEMLIMIT', open(p).read().strip() if os.path.exists(p) else 'no-cgroup')\""
)


def main() -> int:
    api_url = os.environ["E2B_API_URL"]
    internal_key = os.environ["E2B_INTERNAL_API_KEY"]
    boxes = []
    try:
        for i in range(8):
            sb = Sandbox.create()
            boxes.append(sb)
            route = httpx.get(
                f"{api_url}/internal/routes/{sb.sandbox_id}",
                headers={"X-Internal-Key": internal_key},
            ).json()["address"]
            print(f"create {i}: ok -> {route}")
        res = boxes[0].commands.run(LIMIT_CMD)
        print("first sandbox:", (res.stdout or "").strip(), (res.stderr or "").strip()[:80])
        info = boxes[0].get_info()
        print("sdk info memoryMB:", getattr(info, "memory_mb", None))
        print("ALL 8 CREATED OK")
    except Exception as exc:  # noqa: BLE001 - report whatever the API says
        print("FAILED after", len(boxes), "creates:", type(exc).__name__, exc)
        return 1
    finally:
        for sb in boxes:
            try:
                sb.kill()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
