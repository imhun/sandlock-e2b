#!/usr/bin/env python3
"""N83 Phase 1 · Task 1：每个沙箱的 cgroup 到底该挂在哪一层？

这个探针只回答一个问题 —— 在被测节点上，"在 worker pod 的 cgroup 之下建一个子
cgroup、并让写进去的 `cpu.max` **真的生效**"走哪条路。它**不是**读一眼就完事的
取证：cgroup v2 的 no-internal-process 规则让"worker 容器自己的 cgroup"（它有进
程）可能根本不能给子节点启用 cpu 控制器，那种情况下写在子 cgroup 里的 `cpu.max`
会**静默失效**。所以唯一的判据是：把限额写进去、跑一段自旋、读 `cpu.stat`。

它做的事（只碰自己建的目录，退出前删干净，可重复跑）：

  1. 读自己看到的 cgroupfs 与 ``/proc/self/cgroup`` —— 记下这套形状到底有没有 host
     cgroup namespace、rw 视图能看到什么（2026-10-06 的读数：**非特权容器拿不到 host
     cgroupns**，`/proc/self/cgroup` 是 ``0::/``；但 rw 的 hostPath 视图照样看得到宿主
     整棵树。结论见 docs/superpowers/plans/2026-10-06-n83-per-sandbox-cgroup.md §3）；
  2. 找到本节点上 worker pod 的 cgroup 目录，列出它的容器子目录与各项只读读数
     （`cgroup.type` / `cgroup.subtree_control` / `cgroup.controllers` / `cpu.max`）；
  3. 候选 **A**：在 worker *容器目录* 下建 `sbx_probe`（先尝试给容器目录写 `+cpu`）；
  4. 候选 **C**：在 worker *pod 目录* 下建 `sbx_probe`（与容器目录同级）；
  5. 每个候选都做"写 `cpu.max` → 回读 → fork 一个自旋子进程 → 把它的 pid 写进这个
     cgroup 的 `cgroup.procs` → 读回 `/proc/<pid>/cgroup` → 3 s 后读 `cpu.stat`"；
     限额 10 ms/100 ms（0.1 核）跑 3 s ⇒ 生效时 `usage_usec ≈ 300000`、
     `nr_throttled > 0`；不生效时 `usage_usec ≈ 3000000`、`nr_throttled == 0`；
  6. 用完删掉目录；候选 A 若真的改动过容器目录的 `cgroup.subtree_control`，按**原值**
     写回（写不下去会如实报出来，不会假装干净）。

用法（探针容器内，需要 root + hostPID + hostCgroupNamespace + rw 的 /sys/fs/cgroup）：

    python3 -u /probe/probe_cgroup_placement.py --json

环境：
    E2B_PROBE_CGROUP_ROOT       默认 ``/host-cgroup``
    E2B_PROBE_WORKER_POD_UIDS   逗号分隔的 worker pod uid 列表；本节点命中的那个会被
                                选中（两台 worker 各挂在一个节点上，一个节点只会命中
                                一个）。不填则退化成"目录名里出现的第一批 pod<uid>"。

读数给人看，判据在 docs/superpowers/plans/2026-10-06-n83-per-sandbox-cgroup.md §3。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

CGROUP_ROOT = Path(os.environ.get("E2B_PROBE_CGROUP_ROOT", "/host-cgroup"))

#: 探针建的目录名。故意不带 sandbox_id：它不属于任何沙箱，用完就删。
PROBE_DIR = "sbx_probe"

#: 0.1 核 = 10 ms / 100 ms。选这么小是为了"打满额度"这件事在 3 s 里就看得出来，
#: 同时它对节点的影响可以忽略（0.1 核 × 3 s = 0.3 core·s）。
QUOTA_US = 10_000
PERIOD_US = 100_000
SPIN_SECONDS = 3.0

#: 判"生效"的门槛：额度 0.3 s 的 CPU 时间，留一倍余量；不生效时是 3 s 量级，差 5 倍。
ENFORCED_MAX_USAGE_USEC = 600_000


# --------------------------------------------------------------------------- #
# 最小 I/O：每一步都留证据（errno 原文），不猜
# --------------------------------------------------------------------------- #
def read_text(path: Path) -> str:
    """读一个 cgroupfs 文件；失败时返回 ``<errno N: ...>`` 而不是抛。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return f"<errno {exc.errno}: {exc.strerror}>"


def write_text(path: Path, text: str) -> str:
    """写一个 cgroupfs 文件；成功返回 ``"ok"``，失败返回 errno 原文。"""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return "ok"
    except OSError as exc:
        return f"errno {exc.errno} ({exc.strerror})"


def cpu_stat(cgroup_dir: Path) -> dict[str, int] | str:
    """``cpu.stat`` 的键值；读不到就返回错误字符串。"""
    raw = read_text(cgroup_dir / "cpu.stat")
    values: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        try:
            values[key] = int(value)
        except ValueError:
            return raw
    return values or raw


# --------------------------------------------------------------------------- #
# 找到 worker pod 的 cgroup
# --------------------------------------------------------------------------- #
def _pod_dir_tokens(pod_uid: str) -> tuple[str, ...]:
    """两种命名都收：cgroupfs driver 的 ``pod<uid>``，systemd driver 的 ``pod<uid_>``。"""
    return (f"pod{pod_uid}", f"pod{pod_uid.replace('-', '_')}")


def find_pod_dirs(uids: list[str]) -> list[Path]:
    """在 cgroup 树里找名字等于 ``pod<uid>`` 的目录（深度有界，只为了不漏目录）。"""
    wanted = {token for uid in uids if uid for token in _pod_dir_tokens(uid)}
    found: list[Path] = []
    base_depth = len(CGROUP_ROOT.parts)
    for dirpath, dirnames, _ in os.walk(CGROUP_ROOT, followlinks=False):
        depth = len(Path(dirpath).parts) - base_depth
        if depth >= 4:  # kubepods/<qos>/pod<uid> 已经是 3 层
            dirnames[:] = []
            continue
        for name in list(dirnames):
            if name in wanted:
                found.append(Path(dirpath) / name)
    return sorted(found)


def dir_facts(cgroup_dir: Path) -> dict[str, object]:
    """一个 cgroup 目录的只读读数。"""
    facts: dict[str, object] = {
        "path": str(cgroup_dir),
        "cgroup.type": read_text(cgroup_dir / "cgroup.type"),
        "cgroup.subtree_control": read_text(cgroup_dir / "cgroup.subtree_control"),
        "cgroup.controllers": read_text(cgroup_dir / "cgroup.controllers"),
        "cpu.max": read_text(cgroup_dir / "cpu.max"),
        "cpu.stat": cpu_stat(cgroup_dir),
        "cgroup.procs": read_text(cgroup_dir / "cgroup.procs").split(),
    }
    return facts


def container_dirs(pod_dir: Path) -> list[Path]:
    """pod 目录下的容器目录（排除探针自己的目录）。"""
    out: list[Path] = []
    try:
        for entry in sorted(pod_dir.iterdir()):
            if entry.is_dir() and entry.name != PROBE_DIR:
                out.append(entry)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# 自旋 + 限额是否真的生效
# --------------------------------------------------------------------------- #
def _spin(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    counter = 0
    while time.monotonic() < deadline:
        counter += 1
    os._exit(0)


def measure_enforcement(cgroup_dir: Path, seconds: float) -> dict[str, object]:
    """把一个自旋子进程放进 ``cgroup_dir``，量它的 CPU 与节流。

    顺序有意写死：先 fork（子进程还没进 cgroup）、再由**父进程**写
    ``cgroup.procs``、再读回 ``/proc/<pid>/cgroup`` 确认、最后才让子进程开始烧。
    这样量到的 usage 全部属于这个 cgroup。
    """
    result: dict[str, object] = {}
    before = cpu_stat(cgroup_dir)
    result["cpu_stat_before"] = before

    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # 子进程：等父进程把它放好再烧
        try:
            os.close(write_end)
            os.read(read_end, 1)
            _spin(seconds)
        except BaseException:
            pass
        os._exit(0)

    os.close(read_end)
    try:
        result["place_pid"] = write_text(cgroup_dir / "cgroup.procs", f"{pid}\n")
        result["proc_cgroup_after_place"] = read_text(Path(f"/proc/{pid}/cgroup"))
        try:
            os.write(write_end, b"1")
        finally:
            os.close(write_end)
        time.sleep(seconds + 0.5)
        result["cpu_stat_after"] = cpu_stat(cgroup_dir)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    after = result.get("cpu_stat_after")
    if isinstance(after, dict) and isinstance(before, dict):
        used = after.get("usage_usec", 0) - before.get("usage_usec", 0)
        throttled = after.get("nr_throttled", 0) - before.get("nr_throttled", 0)
        result["usage_usec_delta"] = used
        result["nr_throttled_delta"] = throttled
        result["enforced"] = bool(used <= ENFORCED_MAX_USAGE_USEC and throttled > 0)
    else:
        result["usage_usec_delta"] = None
        result["nr_throttled_delta"] = None
        result["enforced"] = None
    return result


def probe_candidate(parent: Path, label: str, *, enable_cpu_first: bool) -> dict[str, object]:
    """在一个候选父目录下试建探针 cgroup，并量限额是否生效。"""
    outcome: dict[str, object] = {
        "candidate": label,
        "parent": str(parent),
        "parent_cpu.max": read_text(parent / "cpu.max"),
        "parent_cgroup.type": read_text(parent / "cgroup.type"),
        "parent_cgroup.subtree_control_before": read_text(parent / "cgroup.subtree_control"),
    }
    target = parent / PROBE_DIR
    subtree_restore: str | None = None

    try:
        outcome["mkdir"] = "ok"
        try:
            target.mkdir()
        except OSError as exc:
            outcome["mkdir"] = f"errno {exc.errno} ({exc.strerror})"
            return outcome

        if enable_cpu_first:
            original = outcome["parent_cgroup.subtree_control_before"]
            outcome["enable_cpu"] = write_text(parent / "cgroup.subtree_control", "+cpu")
            if outcome["enable_cpu"] == "ok" and isinstance(original, str):
                subtree_restore = original
            outcome["parent_cgroup.subtree_control_after"] = read_text(
                parent / "cgroup.subtree_control"
            )

        # 限额必须先写进去；写不进去就没什么可量的了。
        outcome["cpu.max_file_exists"] = (target / "cpu.max").exists()
        outcome["cpu.max_written"] = write_text(target / "cpu.max", f"{QUOTA_US} {PERIOD_US}")
        outcome["cpu.max_readback"] = read_text(target / "cpu.max")
        outcome["limit_applied"] = outcome["cpu.max_readback"] == f"{QUOTA_US} {PERIOD_US}"
        if outcome["limit_applied"]:
            outcome["enforcement"] = measure_enforcement(target, SPIN_SECONDS)
        return outcome
    finally:
        # 清理：先 cgroup.kill（若有），再 rmdir；失败要点名，不假装干净。
        cleanup: dict[str, object] = {}
        if (target / "cgroup.kill").exists():
            cleanup["cgroup.kill"] = write_text(target / "cgroup.kill", "1")
        try:
            target.rmdir()
            cleanup["rmdir"] = "ok"
        except OSError as exc:
            cleanup["rmdir"] = f"errno {exc.errno} ({exc.strerror})"
        if subtree_restore is not None:
            cleanup["subtree_control_restore"] = write_text(
                parent / "cgroup.subtree_control", subtree_restore
            )
            cleanup["subtree_control_after_restore"] = read_text(
                parent / "cgroup.subtree_control"
            )
        outcome["cleanup"] = cleanup


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def probe() -> dict[str, object]:
    reading: dict[str, object] = {
        "cgroup_root": str(CGROUP_ROOT),
        "self_cgroup": read_text(Path("/proc/self/cgroup")),
        "cgroup_root_listing": sorted(
            entry.name for entry in CGROUP_ROOT.iterdir() if entry.is_dir()
        )
        if CGROUP_ROOT.is_dir()
        else f"<not a directory: {CGROUP_ROOT}>",
        "uid": os.getuid(),
        "self_cpu_max": read_text(Path("/sys/fs/cgroup/cpu.max")),
    }

    uids = [
        uid.strip()
        for uid in os.environ.get("E2B_PROBE_WORKER_POD_UIDS", "").split(",")
        if uid.strip()
    ]
    pod_dirs = find_pod_dirs(uids)
    reading["worker_pod_dirs"] = [str(path) for path in pod_dirs]
    if not pod_dirs:
        reading["error"] = "no pod<uid> cgroup directory matched on this node"
        return reading

    pod_dir = pod_dirs[0]
    reading["pod"] = dir_facts(pod_dir)
    containers = container_dirs(pod_dir)
    reading["container_dirs"] = [dir_facts(path) for path in containers]

    # 候选 A：挂在 worker 容器目录下（需要给容器目录 +cpu —— 它有进程，预期 EBUSY）。
    if containers:
        reading["candidate_A"] = probe_candidate(
            containers[0], "A (inside the worker container cgroup)", enable_cpu_first=True
        )
    # 候选 C：挂在 pod 目录下（与容器目录同级；cpu 已被委派给 pod 的子节点）。
    reading["candidate_C"] = probe_candidate(
        pod_dir, "C (sibling of the worker container cgroup)", enable_cpu_first=False
    )

    a_enforced = bool(
        isinstance(reading.get("candidate_A"), dict)
        and reading["candidate_A"].get("enforcement", {}).get("enforced")
    )
    c_enforced = bool(
        reading["candidate_C"].get("enforcement", {}).get("enforced")
        if isinstance(reading.get("candidate_C"), dict)
        else False
    )
    if a_enforced:
        reading["verdict"] = "A"
    elif c_enforced:
        reading["verdict"] = "C"
    else:
        reading["verdict"] = "none (stop: neither placement enforces a cpu.max here)"
    return reading


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="只打 JSON")
    args = parser.parse_args()

    try:
        reading = probe()
    except BaseException:
        reading = {"error": traceback.format_exc()}

    text = json.dumps(reading, indent=2, ensure_ascii=False, default=str)
    if args.json:
        print(text)
    else:
        print("=== N83 cgroup placement probe ===")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
