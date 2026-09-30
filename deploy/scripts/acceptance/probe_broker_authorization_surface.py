#!/usr/bin/env python3
"""C1 broker 的**授权面**探针：确认它对"哪一个沙箱"没有任何概念。

RUNNABLE=no（2026-09-29，C3 Task 7；2026-09-30 的 N52 更进一步）：本探针的三个
phase 都要进 ``e2b-priv-broker`` pod，而那个 DaemonSet 已随 C1 的 socket 形态一起退役
（它的"worker 侧越权尝试"还要求 worker 跑 `E2B_PRIV_HELPER_TRANSPORT=socket`，
那个代码路径也删了），**N52 之后连 worker 侧的 `priv_helpers` 客户端半（
`active_helpers`/`broker_reclaim`）都删了 —— 所以这个文件按现在的代码跑连 import
都过不去**。**留着它是作为 N47 的现场证据，不是待跑的东西** ——
想复现这条链要在 C1 的镜像/清单上跑（git 历史里的 `deploy/k8s/priv-broker.yaml`）。

问题（docs/c3-privilege-relocation.md §14.3）：
  ``e2b-priv-broker`` 的授权只有两条 ——
    ① ``priv_peer_allowed()``：peer uid/gid == ``E2B_BROKER_PEER_UID/GID``（65534）；
    ② ``priv_resolve_allowed_path()``：``realpath`` 后落在**舰队级**的四个根之一。
  没有 per-sandbox、也没有 per-node 的判断。于是"worker 身份"（uid 65534，**全舰队同一个 uid**）
  可以要求 broker 把**任意 pool-uid 拥有的树** ``chown --worker`` 给自己 —— 之后就能读、能删。

  这不是 C3 引入的洞，是 C1 今天就有的；C3 只是**第一个能把它关掉**的形状（请求里第一次有了
  ``sandbox_id`` 这个可授权的对象）。

本探针把这条链在真集群上跑一遍。三个 phase 分别跑在**不同的容器**里：

  # ① 在 broker pod（root，PVC 可写）里建一份"像沙箱树"的 fixture：
  #    pool uid 拥有、0700、里面一个 0600 的 marker
  kubectl -n sandlock exec <broker-pod> -c broker -- \
      python3 probe_broker_authorization_surface.py --phase setup \
      --root /var/lib/e2b-sandboxes/workspaces/_c3authz_probe --owner 10000

  # ② 在 worker pod（uid 65534）里发起越权尝试（这一步才是判据）
  kubectl -n sandlock exec e2b-worker-0 -- \
      python3 probe_broker_authorization_surface.py --phase attempt \
      --root /var/lib/e2b-sandboxes/workspaces/_c3authz_probe

  # ③ 收尾（broker pod）
  kubectl -n sandlock exec <broker-pod> -c broker -- \
      python3 probe_broker_authorization_surface.py --phase cleanup --root ...

判据：``C1-AUTHZ-VERDICT=no-sandbox-authorization`` 表示 worker 身份接管了**不是它的**树并读到了内容。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

MARKER = "C1-AUTHZ-MARKER\n"


def _shape(p: Path) -> str:
    st = p.stat()
    return f"{oct(st.st_mode & 0o7777)} {st.st_uid}:{st.st_gid}"


def phase_setup(root: Path, owner: int) -> int:
    if os.geteuid() != 0:
        print(f"N-identity: FAILED (setup needs euid 0, got {os.geteuid()})", flush=True)
        return 2
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    marker = root / "marker.txt"
    marker.write_text(MARKER, encoding="utf-8")
    os.chown(marker, owner, owner)
    os.chmod(marker, 0o600)
    os.chown(root, owner, owner)
    os.chmod(root, 0o700)
    print(f"N-identity: euid={os.geteuid()}", flush=True)
    print(f"N-fixture: {root} -> {_shape(root)}  marker -> {_shape(marker)}", flush=True)
    print(f"SETUP-OK owner={owner}", flush=True)
    return 0


def phase_attempt(root: Path) -> int:
    from envd_service import priv_helpers

    marker = root / "marker.txt"
    print(f"N-identity: euid={os.geteuid()} gid={os.getegid()}", flush=True)
    print(f"N-tree-before: {_shape(root)}", flush=True)

    # ``active_helpers()`` is the singleton the *worker's own startup* installs;
    # a fresh process (this probe) has to bootstrap it the same way the worker
    # does, or it would report "no brokers" and skip the only interesting cell.
    from envd_service.config import Settings

    priv_helpers.configure_priv_helpers(Settings())
    helpers = priv_helpers.active_helpers()
    print(f"N-helpers: {helpers is not None}", flush=True)

    direct: str
    try:
        direct = marker.read_text(encoding="utf-8")
    except OSError as exc:
        direct = f"{type(exc).__name__}: {exc}"
    blocked = "PermissionError" in direct or "Errno 13" in direct
    print(
        f"T1-direct-read: {'EACCES (as expected: not our tree)' if blocked else direct!r}",
        flush=True,
    )

    if not helpers:
        print("T2-broker-reclaim: SKIPPED (no active helper in this container)", flush=True)
        print("C1-AUTHZ-VERDICT=inconclusive", flush=True)
        return 2

    try:
        # ``broker_reclaim`` == ``e2b-maint chown --worker --recursive``:
        # the gift-to-the-caller primitive the orphan sweep uses.
        priv_helpers.broker_reclaim(root)
        reclaim = "ok"
    except BaseException as exc:  # noqa: BLE001 - report, do not guess
        reclaim = f"{type(exc).__name__}: {exc}"
    print(f"T2-broker-reclaim: {reclaim}", flush=True)

    after = _shape(root)
    print(f"N-tree-after: {after}", flush=True)

    try:
        taken = marker.read_text(encoding="utf-8")
    except OSError as exc:
        taken = f"{type(exc).__name__}: {exc}"
    print(f"T3-read-after-reclaim: {taken!r}", flush=True)

    escalated = taken == MARKER
    print(
        f"C1-AUTHZ-VERDICT={'no-sandbox-authorization' if escalated else 'not-reproduced'}",
        flush=True,
    )
    return 0 if escalated else 1


def phase_cleanup(root: Path) -> int:
    if os.geteuid() != 0:
        print(f"N-identity: FAILED (cleanup needs euid 0, got {os.geteuid()})", flush=True)
        return 2
    shutil.rmtree(root, ignore_errors=False)
    print(f"N-cleanup: root_gone={not root.exists()}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["setup", "attempt", "cleanup"], required=True)
    ap.add_argument(
        "--root",
        default="/var/lib/e2b-sandboxes/workspaces/_c3authz_probe",
        help="fixture 目录（只动它，跑完 cleanup 删掉）",
    )
    ap.add_argument("--owner", type=int, default=10000, help="setup 时 fixture 的属主（池 uid）")
    args = ap.parse_args()
    root = Path(args.root)
    if args.phase == "setup":
        return phase_setup(root, args.owner)
    if args.phase == "attempt":
        return phase_attempt(root)
    return phase_cleanup(root)


if __name__ == "__main__":
    sys.exit(main())
