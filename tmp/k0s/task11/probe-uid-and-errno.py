#!/usr/bin/env python3
"""Task 11 底稿：越界 errno 在两态下到底答什么（逐字节）。

要量的两组：

1. `tests/security/test_uid_isolation.py::test_distinct_host_uids_isolate_same_path_files`
   里那些**绝对宿主路径**探针 —— 它们在 identity 形态下是"存在但未授权"（EACCES），
   在合成根形态下"根本不在树里"（ENOENT）。这是 Task 10 那条红的全部内容。
2. `tests/security/test_pure_root_errno_contract.py` 要写死的三条（简报 Step 5）：
   `/etc/passwd`（骨架里有、未授权）、`/src/host-only/SECRET` 与 `/src`（哪棵树里都没有）。

走 `tests/security/conftest.py::route_b_sandbox`（把 E2B_PURE_ROOTFS / E2B_REAL_ROOT
镜像进 executor 的唯一入口），并且**自证形态**：`has_sandbox_root` 与开关不符 ⇒ 退 2
（VACUOUS，量到的曲线无意义）。

退出码：0 = 两态都量到、自证成功；1 = 某形态没量完；2 = VACUOUS。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

# `tmp/k0s/task11/` 比其它探针深一层：仓根是 parents[3]。
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.conftest import TMP_ROOT  # noqa: E402
from tests.security.conftest import route_b_sandbox, run_sh  # noqa: E402

UID_A = 10000
UID_B = 10001

SHAPES: dict[str, tuple[str, str, bool]] = {
    "identity": ("off", "0", False),
    "synth-realroot": ("synth", "1", True),
}

SCRATCH = TMP_ROOT / "task11-uid-probe"
EVIDENCE = REPO_ROOT / "tmp/k0s/task11/probe-uid-and-errno.json"


def layout() -> tuple[Path, Path, Path]:
    """`sbx_a`/`sbx_b`, each `0770 <uid>:<worker gid>`, exactly the uid test's."""
    from envd_service.uid_pool import apply_sandbox_ownership

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    root = SCRATCH
    root.mkdir(parents=True)
    os.chmod(root, 0o755)
    ws_a = root / "sbx_a"
    ws_b = root / "sbx_b"
    for ws, uid in ((ws_a, UID_A), (ws_b, UID_B)):
        (ws / "workspace").mkdir(parents=True)
        apply_sandbox_ownership(ws, uid)
    (ws_a / "workspace" / "secret.txt").write_text("A-secret", encoding="utf-8")
    (ws_b / "workspace" / "secret.txt").write_text("B-secret", encoding="utf-8")
    for ws, uid in ((ws_a, UID_A), (ws_b, UID_B)):
        apply_sandbox_ownership(ws, uid)
    return ws_a, ws_b, ws_a / "workspace" / "secret.txt"


async def run_as(uid: int, workspace: Path, commands: list[str]) -> tuple[dict, dict]:
    executor, ws = route_b_sandbox(None, None, host_uid=uid, workspace=workspace)
    shape = {
        "E2B_PURE_ROOTFS": os.environ.get("E2B_PURE_ROOTFS"),
        "E2B_REAL_ROOT": os.environ.get("E2B_REAL_ROOT"),
        "has_sandbox_root": executor._has_sandbox_root,
        "chroot_root": executor._chroot_root,
        "synthetic_rootfs": (
            str(executor._synthetic_rootfs)
            if executor._synthetic_rootfs is not None
            else None
        ),
    }
    out: dict[str, list] = {}
    try:
        for command in commands:
            code, stdout, stderr = await run_sh(executor, ws, command)
            out[command] = [
                code,
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"),
            ]
    finally:
        executor.close()
    return out, shape


async def main() -> int:
    results: dict[str, dict] = {}
    shapes_seen: dict[str, dict] = {}
    failures: list[str] = []

    for label, (pure_rootfs, real_root, expects_root) in SHAPES.items():
        os.environ["E2B_PURE_ROOTFS"] = pure_rootfs
        os.environ["E2B_REAL_ROOT"] = real_root
        ws_a, ws_b, secret_a = layout()
        try:
            for role, uid, ws, commands in (
                (
                    "B",
                    UID_B,
                    ws_b,
                    [
                        f"cd {ws_a / 'workspace'}",
                        f"/bin/stat {ws_a / 'workspace'}",
                        f"/bin/cat {secret_a}",
                        f"printf x > {ws_a / 'workspace' / 'b-wrote.txt'}",
                        f"/bin/rm {secret_a}",
                        "cat workspace/secret.txt",
                        "cat ../sbx_a/workspace/secret.txt",
                        "cat /etc/passwd",
                        "cat /src/host-only/SECRET",
                        "stat /src",
                    ],
                ),
                (
                    "A",
                    UID_A,
                    ws_a,
                    [
                        f"/bin/cat {secret_a}",
                        "cat workspace/secret.txt",
                        "pwd",
                    ],
                ),
            ):
                measured, shape = await run_as(uid, ws, commands)
                results.setdefault(label, {}).update(
                    {f"[{role}] {c}": v for c, v in measured.items()}
                )
                shapes_seen.setdefault(label, {})[role] = shape
                if bool(shape["has_sandbox_root"]) is not expects_root:
                    failures.append(
                        f"VACUOUS [{label}/{role}] has_sandbox_root="
                        f"{shape['has_sandbox_root']} expected={expects_root} "
                        f"(E2B_PURE_ROOTFS={shape['E2B_PURE_ROOTFS']!r})"
                    )
        except Exception as exc:  # a state that cannot be built is a failure
            failures.append(f"MEASURE-FAIL [{label}] {type(exc).__name__}: {exc}")

    for line in failures:
        print(line)
    for label, roles in shapes_seen.items():
        for role, seen in roles.items():
            print(
                f"shape [{label}/{role}] pure_rootfs={seen['E2B_PURE_ROOTFS']!r} "
                f"real_root={seen['E2B_REAL_ROOT']!r} "
                f"has_sandbox_root={seen['has_sandbox_root']} "
                f"chroot_root={seen['chroot_root']} "
                f"synthetic_rootfs={seen['synthetic_rootfs']}"
            )

    print()
    for label in SHAPES:
        for command, triple in results.get(label, {}).items():
            print(f"{label:<15} {command:<64} = {triple}")
        print()

    diffs = 0
    if len(results) == len(SHAPES):
        reference = results["identity"]
        for command, triple in results["synth-realroot"].items():
            if reference.get(command) != triple:
                diffs += 1
    print(
        f"measured={','.join(sorted(results))} "
        f"failed={','.join(sorted(set(SHAPES) - set(results))) or '-'} "
        f"diffs={diffs}"
    )

    EVIDENCE.write_text(
        json.dumps(
            {"shapes": shapes_seen, "results": results, "diffs": diffs},
            indent=2,
        ),
        encoding="utf-8",
    )
    if any(line.startswith("VACUOUS") for line in failures):
        return 2
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
