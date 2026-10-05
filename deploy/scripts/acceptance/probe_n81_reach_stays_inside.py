#!/usr/bin/env python3
"""N81 钉子：沙箱 `stat` 得到的每一样东西，都在它自己的文件系统上。

放行 stat 之后，"能看见什么元数据"完全等于"沙箱的挂载命名空间里有什么"。
这条钉子把那个前提变成一个可观测判据：**凡是沙箱 `stat` 成功的路径，`st_dev`
必须等于它自己根目录（`/`）的 `st_dev`**。宿主自己的文件在**另一个设备**上，
所以任何一天有 worker / 平台路径混进沙箱的挂载命名空间，这条会当场变红 ——
而不是等到有人把 `/proc/<宿主pid>` 读出来。

三条 worker 路径今天必须是 ENOENT（它们不在沙箱的 rootfs 里），
`/etc/os-release` 是反向对照：必须成功，且与 `/` 同一个 dev。

用法：
    export E2B_API_URL=http://<入口>:3000 E2B_SANDBOX_URL=http://<入口>:3000
    export E2B_API_KEY=...
    python deploy/scripts/acceptance/probe_n81_reach_stays_inside.py
"""

from __future__ import annotations

import re
import sys
from textwrap import dedent

from e2b import Sandbox

WORKER_PATHS = (
    "/var/lib/e2b-images",
    "/var/lib/e2b-sandboxes/state",
    "/var/lib/e2b/workspaces",
)

PROBE = dedent(
    """
    import os

    for path in ("/", "/etc/os-release", "/etc/shadow",
                 "/var/lib/e2b-images", "/var/lib/e2b-sandboxes/state",
                 "/var/lib/e2b/workspaces"):
        try:
            st = os.stat(path)
        except OSError as exc:
            print(f"STAT {path} errno={exc.errno} {exc.strerror}")
        else:
            print(f"STAT {path} OK dev={st.st_dev}")
    """
)


def main() -> int:
    box = Sandbox.create()
    try:
        # `/tmp` is not writable in the deployed (rootfs) shape; the cwd is.
        out = box.commands.run(
            "cat > reach.py <<'PYEOF'\n" + PROBE + "PYEOF\npython3 reach.py"
        )
        text = ((out.stdout or "") + (out.stderr or "")).strip()
        print(text)

        root = re.search(r"STAT / OK dev=(\d+)", text)
        control = re.search(r"STAT /etc/os-release OK dev=(\d+)", text)
        ok_devs = dict(re.findall(r"STAT (\S+) OK dev=(\d+)", text))
        unreachable = {
            path
            for path in WORKER_PATHS
            if f"STAT {path} errno=" in text
        }

        same_device = bool(root) and all(
            dev == root.group(1) for dev in ok_devs.values()
        )
        control_ok = bool(root and control and control.group(1) == root.group(1))
        worker_paths_absent = unreachable == set(WORKER_PATHS)

        ok = same_device and control_ok and worker_paths_absent
        print(
            "REACH",
            "OK" if ok else "FAIL",
            f"everything_statable_is_on_the_sandbox_device={same_device} "
            f"control_ok={control_ok} worker_paths_absent={worker_paths_absent}",
        )
        return 0 if ok else 1
    finally:
        box.kill()


if __name__ == "__main__":
    sys.exit(main())
