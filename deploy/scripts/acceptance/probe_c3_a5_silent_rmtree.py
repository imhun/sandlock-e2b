#!/usr/bin/env python3
"""C3 §13.7 / A5 探针：CP 的「配对删除」在非属主身份下**静默失败**。

问题（docs/c3-privilege-relocation.md §13.7）：
  ``control_plane/api/sandboxes.py::_remove_local_tree_confirming`` 里，
  沙箱树走 broker 的**确认式**路径（``priv_helpers.remove_tree(..., on_error="raise")``，
  W7 评审专门为"静默半删"修的），但和它配对的 ``_runtime/<id>`` 目录用的是**裸**
  ``shutil.rmtree(..., ignore_errors=True)``，而且之后**无条件 ``return True``**。

  那个目录由 ``RuntimeRegistry._ensure_runtime_dir`` 建成 ``0700``，属主是跑 worker 的
  身份（生产是 65534）。于是当 CP 不是 root、也不是该属主时，这一步删不掉 ——
  而 ``ignore_errors=True`` 让它**不报错**，函数照样返回 True。

本探针调用**真实函数**，不复制逻辑、不模拟。

cell（每个 cell 一份全新 fixture，只建在 ``--root`` 下）：
  A-root     root 身份调用真实函数        -> 目录消失、返回 True（今天为什么看不见这个 bug）
  A-as-uid   uid X 身份调用真实函数       -> **目录仍在、却返回 True**（这就是 A5）
  A-strict   uid X 用 ignore_errors=False -> 抛 PermissionError（证明失败是真的，只是被掩盖）
  A-owner    属主身份调用真实函数         -> 目录消失（说明「CP 用同一个 uid」是一种可行修法）

另有 ``N-fixture-shape``：先断言每个 fixture 确实是 ``0700 owner:owner``，
否则后面的判据不成立（fail closed，不猜）。

用法（在**挂了共享 PVC 的 root 容器**里跑，例如 control-plane pod 主容器）：
  python3 probe_c3_a5_silent_rmtree.py --root /var/lib/e2b-sandboxes/_probes/c3-a5
  python3 probe_c3_a5_silent_rmtree.py --root DIR --owner 65534 --test-uid 65533

判据：``C3-A5-VERDICT=reproduced`` 表示 A5 成立（静默失败可复现）；``not-reproduced`` 表示
本机/本挂载上不成立。跑完 ``--root`` 整个删掉（探针自己删，并复验）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def _run_as(uid: int, gid: int, fn) -> dict:
    """Fork; drop to (uid, gid) in the child; run ``fn``; bring back a JSON verdict."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        os.close(read_fd)
        payload: dict
        try:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
            if os.getuid() != uid:  # pragma: no cover - fail closed, never guess
                raise RuntimeError(f"setuid({uid}) did not take: uid={os.getuid()}")
            payload = {"ok": True, "result": fn()}
        except BaseException as exc:  # noqa: BLE001 - the point is to report it
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            os.write(write_fd, json.dumps(payload).encode())
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    chunks: list[bytes] = []
    while True:
        block = os.read(read_fd, 65536)
        if not block:
            break
        chunks.append(block)
    os.close(read_fd)
    os.waitpid(pid, 0)
    raw = b"".join(chunks)
    if not raw:  # pragma: no cover - child died without a verdict
        return {"ok": False, "error": "child produced no verdict"}
    return json.loads(raw)


def _make_fixture(root: Path, sandbox_id: str, owner: int):
    """Build ``<root>/state/_runtime/<id>/`` exactly as the registry does.

    ``RuntimeRegistry._ensure_runtime_dir``: ``mkdir`` -> ``chmod 0o700`` ->
    ``chown(geteuid, getegid)``. The tree (``<workspace_base>/<id>``) is left
    **absent** on purpose: that is the teardown case where the tree is already
    gone and only the paired runtime dir remains.

    The ``_runtime`` **parent** is given production's measured shape too
    (``0711 owner:owner``): without it an ``rmdir`` of the child is refused by
    the parent's own mode, and the cell would "fail" for the wrong reason.
    That mistake was made and caught in the first run of this probe.
    """
    from gateway_common.paths import sandbox_runtime_dir

    workspace_base = root / "workspaces"
    state_base = root / "state"
    workspace_base.mkdir(parents=True, exist_ok=True)
    runtime_dir = sandbox_runtime_dir(workspace_base, sandbox_id, state_base=state_base)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "command-logs.jsonl").write_text("{}\n", encoding="utf-8")
    os.chown(runtime_dir, owner, owner)
    os.chmod(runtime_dir, 0o700)
    runtime_parent = runtime_dir.parent  # <state>/_runtime -- production: 0711 owner
    os.chown(runtime_parent, owner, owner)
    os.chmod(runtime_parent, 0o711)
    return workspace_base, state_base, runtime_dir


def _shape(runtime_dir: Path) -> dict:
    st = runtime_dir.stat()
    parent = runtime_dir.parent.stat()
    return {
        "mode": oct(st.st_mode & 0o7777),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "parent_mode": oct(parent.st_mode & 0o7777),
        "parent_uid": parent.st_uid,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root",
        default="/var/lib/e2b-sandboxes/_probes/c3-a5",
        help="fixture 根（只在这里建东西，跑完自己删）",
    )
    ap.add_argument("--owner", type=int, default=65534, help="runtime dir 的属主（worker uid）")
    ap.add_argument("--test-uid", type=int, default=65533, help="非属主、非 root 的测试身份")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print(f"N-root: FAILED (need euid 0, got {os.geteuid()})", flush=True)
        return 2

    from control_plane.api.sandboxes import _remove_local_tree_confirming

    root = Path(args.root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    class _State:  # the only two attributes the function reads
        pass

    verdicts: dict[str, str] = {}

    def fixture(tag: str):
        sandbox_id = f"sbx_c3a5{tag}"
        workspace_base, state_base, runtime_dir = _make_fixture(
            root, sandbox_id, args.owner
        )
        state = _State()
        state.workspace_base = workspace_base
        state.state_base = state_base
        return sandbox_id, state, runtime_dir

    # --- N-fixture-shape: 判据的前提，先钉住 ---
    _, _, probe_dir = fixture("shape")
    shape = _shape(probe_dir)
    ok = shape == {
        "mode": "0o700",
        "uid": args.owner,
        "gid": args.owner,
        "parent_mode": "0o711",
        "parent_uid": args.owner,
    }
    verdicts["N-fixture-shape"] = (
        f"{'ok' if ok else 'FAILED'} (dir={shape['mode']} {shape['uid']}:{shape['gid']}, "
        f"parent={shape['parent_mode']} {shape['parent_uid']}, "
        f"want 0o700 + 0o711 both owned by {args.owner})"
    )
    shutil.rmtree(probe_dir)

    # --- A-root: 今天的形态（CP 是 root）---
    sandbox_id, state, runtime_dir = fixture("root")
    res = _run_as(0, 0, lambda: _remove_local_tree_confirming(state, sandbox_id))
    survived = runtime_dir.exists()
    verdicts["A-root"] = (
        f"returned={res.get('result')!r} survived={survived} "
        f"-> {'ok (invisible today)' if (not survived and res.get('result') is True) else 'UNEXPECTED'}"
    )

    # --- A-as-uid: 非属主、非 root（A5 的正题）---
    sandbox_id, state, runtime_dir = fixture("asuid")
    res = _run_as(
        args.test_uid, args.test_uid, lambda: _remove_local_tree_confirming(state, sandbox_id)
    )
    survived = runtime_dir.exists()
    silent = survived and res.get("ok") and res.get("result") is True
    verdicts["A-as-uid"] = (
        f"returned={res.get('result')!r} survived={survived} child_error={res.get('error')!r} "
        f"-> {'SILENT FAILURE' if silent else 'no silent failure'}"
    )

    # --- A-strict: 同一个身份，但不用 ignore_errors ---
    sandbox_id, state, runtime_dir = fixture("strict")
    res = _run_as(
        args.test_uid, args.test_uid, lambda: shutil.rmtree(runtime_dir, ignore_errors=False)
    )
    survived = runtime_dir.exists()
    verdicts["A-strict"] = (
        f"raised={res.get('error')!r} survived={survived} "
        f"-> {'ok (the failure is real, only masked)' if (res.get('error') and survived) else 'UNEXPECTED'}"
    )

    # --- A-owner: 属主身份（「CP 用同一个 uid」这条修法）---
    sandbox_id, state, runtime_dir = fixture("owner")
    res = _run_as(
        args.owner, args.owner, lambda: _remove_local_tree_confirming(state, sandbox_id)
    )
    survived = runtime_dir.exists()
    verdicts["A-owner"] = (
        f"returned={res.get('result')!r} survived={survived} "
        f"-> {'ok (same uid means no privilege needed)' if not survived else 'UNEXPECTED'}"
    )

    for name, text in verdicts.items():
        print(f"{name}: {text}", flush=True)

    reproduced = silent
    print(
        f"C3-A5-VERDICT={'reproduced' if reproduced else 'not-reproduced'}",
        flush=True,
    )

    shutil.rmtree(root, ignore_errors=False)
    leftovers = sorted(str(p) for p in Path(args.root).parent.glob(Path(args.root).name + "*"))
    print(f"N-cleanup: root_gone={not Path(args.root).exists()} leftovers={leftovers}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
