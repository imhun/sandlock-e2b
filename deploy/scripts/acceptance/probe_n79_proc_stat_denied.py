#!/usr/bin/env python3
"""N79 验收钉子：stat 单列预算之后，`/proc` 的 stat 仍然被中介拒（EACCES）。

选项 ① 换的是"这些通知算谁的预算"，不是"拦不拦"。所以拦截语义必须原样：
任何 `/proc/<n>`（含宿主 pid）的 stat 族调用都还在 `handle_proc_stat_family`
手里，一律 EACCES；非 /proc 的 stat 照常放行。

用法（对着已部署的集群跑）：

    export E2B_API_URL=http://<入口>:3000 E2B_SANDBOX_URL=http://<入口>:3000
    export E2B_API_KEY=...
    python deploy/scripts/acceptance/probe_n79_proc_stat_denied.py

判据是**两行精确匹配**（不是"包含"）：宿主 pid 那条必须 `errno=13`，
`/etc/os-release` 那条必须 `ALLOWED` —— 后一半是这条钉子与"一律拒绝"的区别。
"""

from __future__ import annotations

import sys
from textwrap import dedent

from e2b import Sandbox

PROBE = dedent(
    """
    import os

    for path in ("/proc/1", "/etc/os-release"):
        try:
            os.stat(path)
        except OSError as exc:
            print(f"{path} errno={exc.errno} {exc.strerror}")
        else:
            print(f"{path} ALLOWED")
    """
)


def main() -> int:
    box = Sandbox.create()
    try:
        # A heredoc, not `python3 -c repr(...)`: the command goes through a
        # shell on the sandbox side, and the escaped newlines of a repr survive
        # as literal backslash-n (observed: SyntaxError). `/tmp` is not
        # writable in the deployed (rootfs) shape; the sandbox's cwd is.
        out = box.commands.run(
            "cat > proc_stat_denied.py <<'PYEOF'\n"
            + PROBE
            + "PYEOF\npython3 proc_stat_denied.py"
        )
        text = (out.stdout or "") + (out.stderr or "")
        print(text.strip())
        # `/proc/1` is the *host* pid 1: the sandbox must not be answered for
        # it. `/etc/os-release` is an ordinary stat and must stay allowed.
        ok = text.splitlines() == [
            "/proc/1 errno=13 Permission denied",
            "/etc/os-release ALLOWED",
        ]
        print("PROC-PIN", "OK" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        box.kill()


if __name__ == "__main__":
    sys.exit(main())
