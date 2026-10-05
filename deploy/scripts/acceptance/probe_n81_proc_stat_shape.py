#!/usr/bin/env python3
"""N81 取证/钉子：真根形态下沙箱的 `/proc` 不是宿主 procfs。

这条判据是"彻底放行 stat"（N79 选项 ②）的前提。三条读数：

1. `stat /proc` 的 `st_dev` **等于** `stat /` 的 `st_dev` ⇒ `/proc` 不是独立挂载；
2. `stat /proc/uptime|version|meminfo|cpuinfo` 全部 `ENOENT` —— 这四条是**非数字**
   路径，`handle_proc_stat_family` 对它们本来就 `Continue`，所以这是**内核自己在答**：
   内核眼里 `/proc` 是个空目录；
3. 对照：`stat /proc/1` 是 `EACCES`（那是中介对数字 pid 的拒绝，不是内核的答案）。

三者合起来 = "沙箱的 `/proc` 是它自己文件系统里的普通空目录"，于是内核**不可能**把
`/proc/<宿主pid>` 解析到宿主进程上 —— 今天那条 EACCES 是冗余的（内核会给 ENOENT）。

用法：
    export E2B_API_URL=http://<入口>:3000 E2B_SANDBOX_URL=http://<入口>:3000
    export E2B_API_KEY=...
    python deploy/scripts/acceptance/probe_n81_proc_stat_shape.py

判据是**逐行精确匹配**（不是"包含"），并且要求 `/proc` 与 `/` 的 `st_dev` 相等。
"""

from __future__ import annotations

import re
import sys
from textwrap import dedent

from e2b import Sandbox

PROBE = dedent(
    """
    import os

    for path in ("/", "/proc", "/proc/uptime", "/proc/version",
                 "/proc/meminfo", "/proc/cpuinfo", "/proc/1"):
        try:
            st = os.stat(path)
        except OSError as exc:
            print(f"STAT {path} errno={exc.errno} {exc.strerror}")
        else:
            print(f"STAT {path} OK dev={st.st_dev} mode={oct(st.st_mode)}")
    """
)


def main() -> int:
    box = Sandbox.create()
    try:
        # `/tmp` is not writable in the deployed (rootfs) shape; the cwd is.
        out = box.commands.run(
            "cat > proc_shape.py <<'PYEOF'\n" + PROBE + "PYEOF\npython3 proc_shape.py"
        )
        text = ((out.stdout or "") + (out.stderr or "")).strip()
        print(text)
        lines = {line.split()[1]: line for line in text.splitlines()}

        root = re.search(r"STAT / OK dev=(\d+)", text)
        proc = re.search(r"STAT /proc OK dev=(\d+)", text)
        same_mount = bool(root and proc and root.group(1) == proc.group(1))
        kernel_says_empty = all(
            lines.get(path, "").endswith("errno=2 No such file or directory")
            for path in ("/proc/uptime", "/proc/version", "/proc/meminfo", "/proc/cpuinfo")
        )
        numeric_denied = lines.get("/proc/1", "").endswith("errno=13 Permission denied")

        ok = same_mount and kernel_says_empty and numeric_denied
        print(
            "PROC-SHAPE",
            "OK" if ok else "FAIL",
            f"same_st_dev={same_mount} kernel_sees_empty_dir={kernel_says_empty} "
            f"numeric_denied_by_mediator={numeric_denied}",
        )
        return 0 if ok else 1
    finally:
        box.kill()


if __name__ == "__main__":
    sys.exit(main())
