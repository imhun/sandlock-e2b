#!/usr/bin/env python3
"""验收钉子：沙箱里 `stat /proc/<宿主 pid>` **不可解析**。

这条性质跨了两次改动，答它的人换了，性质没换：

* N79 之前 / 期间：`handle_proc_stat_family` 把它拒成 **EACCES**；
* N81 之后：中介整族退出通知表，内核在沙箱自己的**空 `/proc`** 上答
  **ENOENT**（`probe_n81_proc_stat_shape.py` 是那条前提的取证：`stat /proc`
  与 `stat /` 同 `st_dev`，`/proc/uptime` 之类的非数字路径本来就由内核答 ENOENT）。

两种都满足判据：**这个路径拿不到宿主进程的元数据**。所以钉子接受这两个 errno
之一，但要求非 `/proc` 的 stat 照常成功 —— 后一半让它不是"一律拒绝"的橡皮图章。

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
        # it -- by the mediator (EACCES, pre-N81) or by the kernel on the
        # sandbox's own empty /proc (ENOENT, N81+). `/etc/os-release` is an
        # ordinary stat and must stay allowed; that half is what makes this a
        # pin rather than a blanket denial.
        lines = text.splitlines()
        host_pid_unresolvable = bool(lines) and lines[0] in (
            "/proc/1 errno=13 Permission denied",
            "/proc/1 errno=2 No such file or directory",
        )
        ok = host_pid_unresolvable and lines[1:] == ["/etc/os-release ALLOWED"]
        print("PROC-PIN", "OK" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        box.kill()


if __name__ == "__main__":
    sys.exit(main())
