#!/usr/bin/env python3
"""Cluster acceptance for S2/S3/S4: a pause that survives its worker.

仓库版（2026-09-26 从 ``tmp/k0s/checkpoint_acceptance.py`` 搬入）。它回答的是**生产形态**
（image-rootfs + ``E2B_REAL_ROOT=1``）下这条能力到底能不能用，所以它不只是回归测试，
也是"这一版能不能对外声明"的判据。用法见 ``docs/deploy-clusters.md`` §9。

The one shape today cannot do, and the reason the whole feature exists:

    start a sandbox running something -> pause it -> **replace the worker that
    hosts it** -> resume -> the same process is back, and the sandbox can still
    exec.

The second half of the last line is D9/(b): the restored process is a child of
the sandbox's *session*, so the session keeps serving commands, unlike OCI's
own restore path (which has no init and refuses exec by name).

Each step is asserted, not printed: the counter file must freeze at the pause
and continue from where it stopped after the resume (not from 1, and not by the
week-old process having secretly kept running), the checkpoint image must exist
on the shared volume while paused and be gone after the resume, and the exec
after the resume must return the expected stdout.

Uses the deployed gateway + `kubectl` against the self-hosted k0s cluster
(`docs/deploy-clusters.md` §2: the KUBECONFIG must point at *that* cluster).

**它会删掉宿主 worker 的 pod**，而那台 worker 上**别人的**沙箱会跟着一起死：
worker 的沙箱注册表是内存态，pod 没了，那些沙箱就再也 `resume` 不回来。所以删之前脚本先读
控制面的按节点名单（`GET /internal/nodes/<id>/sandboxes`），名单里只要还有不属于本次验收的
沙箱就**拒绝并退出（码 2）**，`--force` 是"我确认它们可以和这个 pod 一起死"的唯一说法。
⇒ **只能在没有别人沙箱的 worker 上跑**（见 `docs/deploy-clusters.md` §9）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import NoReturn

import httpx

API = os.environ.get("E2B_API_URL", "http://172.18.78.49:3000")
KEY = os.environ.get("E2B_API_KEY", "local-key")
INTERNAL = os.environ.get("E2B_INTERNAL_API_KEY", "internal-key")
KUBECONFIG = os.environ["KUBECONFIG"]
NAMESPACE = os.environ.get("E2B_NAMESPACE", "sandlock")

#: 导出根（共享卷的挂载点）。N27 之前平台状态与沙箱树都直接躺在它下面，今天不是了：
#: 树根下沉到 ``<export>/workspaces``、平台状态到 ``<export>/state``（两个名字都由 worker
#: 自己的清单给出，见 `state_base_of`）。这里的值只当**旧布局**的回退。
BASE = "/var/lib/e2b-sandboxes"
#: Relative on purpose: the file API resolves against the sandbox's cwd
#: (``/home/user``), and an absolute-looking path is treated as relative to it.
COUNTER = "tick"
#: Both fixtures write **atomically** (temp file + `os.replace`). `pause()`
#: freezes the process wherever it happens to be, and a plain `open(w)` leaves
#: the file *empty* for the whole freeze if the freeze lands between the
#: truncate and the write -- the reader then reports "" and this acceptance
#: calls it "the counter vanished at the pause" (measured 2026-09-25: the same
#: script passed twice in a row and then failed this way). `os.replace` within
#: one directory is atomic, so every observation is a complete value: either
#: the previous tick or the new one.
PROGRAM = (
    "import os, time\n"
    "n = 0\n"
    "while True:\n"
    "    n += 1\n"
    "    with open('/home/user/.tick.tmp', 'w') as fh:\n"
    "        fh.write(str(n))\n"
    "    os.replace('/home/user/.tick.tmp', '/home/user/tick')\n"
    "    time.sleep(1)\n"
)

#: The second workload, used for the worker-restart half: the first one is killed
#: before the exec check (the deployment runs ONE command per sandbox at a time,
#: `max_concurrent_commands_per_sandbox` = 1), so the process the restart has to
#: bring back starts afterwards, into its own files.
#:
#: It writes a **boot marker** as its very first statement and publishes its pid
#: there, so "the resume announced a child" can be told apart from "the restored
#: program actually executed": after the resume, `boot2.txt` naming a *new* pid
#: means the restored image ran Python code, and the marker being unchanged means
#: it died inside the restore itself. Its stdio is redirected to files by the
#: shell that execs it, so a traceback survives where a closed stderr would have
#: swallowed it (the stdio fds are skipped by the restore).
PROGRAM2 = (
    "import os, time\n"
    "with open('/home/user/boot2.txt', 'w') as fh:\n"
    "    fh.write('pid=%d\\n' % os.getpid())\n"
    "n = 0\n"
    "while True:\n"
    "    n += 1\n"
    "    with open('/home/user/.tick2.tmp', 'w') as fh:\n"
    "        fh.write(str(n))\n"
    "    os.replace('/home/user/.tick2.tmp', '/home/user/tick2')\n"
    "    time.sleep(1)\n"
)


def step(name: str, **fields) -> None:
    print(json.dumps({"step": name, **fields}, ensure_ascii=False), flush=True)


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": KUBECONFIG},
    )


def worker_env(name: str) -> str:
    """`sts/e2b-worker` 的 worker 容器里某个环境变量的值（没设 = 空串）。

    Read from the manifest rather than from this process's environment: the
    question is what the *deployed* worker was told, and the runner's own
    environment is not evidence about the cluster.
    """
    return kubectl(
        "get",
        "sts",
        "e2b-worker",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='" + name + "')].value}",
    ).stdout.strip()


def state_base_of() -> str:
    """平台状态（含 checkpoint 图）落在哪个 base 下面。

    N27 把树根下沉了一级，并把平台状态搬成了它的**兄弟**：图从
    ``<export>/_runtime/.checkpoints`` 搬到了 ``<state base>/_runtime/.checkpoints``。
    迁完之后旧路径**不存在**（同一个挂载上的 ``rename(2)``，不是留了一份副本），
    而"旧路径读不到"与"图根本没写"在字面上是同一条 `No such file or directory` ——
    所以这里按 worker 自己的清单回答，而不是沿用 2026-09-25 的那个常量。

    `E2B_STATE_BASE` 没设 = 平台状态跟着工作区 base（N27 之前的布局，也就是所有其它
    部署今天的形态）；两个都没设 = 导出根本身。
    """
    return (
        worker_env("E2B_STATE_BASE")
        or worker_env("E2B_WORKSPACE_BASE")
        or BASE
    )


def checkpoint_store_root(state_base: str) -> str:
    """`<state base>/_runtime/.checkpoints` —— 图真正落地的那层目录。"""
    return f"{state_base}/_runtime/.checkpoints"


def route_of(sandbox_id: str) -> dict:
    resp = httpx.get(
        f"{API}/internal/routes/{sandbox_id}",
        headers={"X-Internal-Key": INTERNAL},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def platform_account() -> dict:
    """The S2/D3 numbers as the control plane's node view sees them."""
    resp = httpx.get(
        f"{API}/internal/nodes",
        headers={"X-Internal-Key": INTERNAL},
        timeout=30,
    )
    resp.raise_for_status()
    return {
        node["nodeID"]: {
            "used": node.get("platformDiskUsedMB"),
            "budget": node.get("platformDiskBudgetMB"),
        }
        for node in resp.json()
    }


def node_sandbox_ids(node_id: str) -> list[str]:
    """控制面视角下这台 worker 上的沙箱 id 列表。

    读的是**控制面**的按节点视图（`control_plane/api/internal.py::node_sandboxes`，
    也是 worker 做分区 reconcile 的权威名单），不是 worker 自己的内存注册表：要删的
    正是这个 pod，它没有资格给自己开一张"我很空"的证明。
    """
    resp = httpx.get(
        f"{API}/internal/nodes/{node_id}/sandboxes",
        headers={"X-Internal-Key": INTERNAL},
        timeout=30,
    )
    resp.raise_for_status()
    return list(resp.json()["sandboxIDs"])


#: 礼貌检查拒绝时的退出码。与断言失败（1）分开：这里连"开始验收"都没发生，
#: 集群上什么都没动。
REFUSED_WORKER_BUSY = 2


def _refuse_worker_pod(message: str) -> NoReturn:
    """拒绝删 pod：原文进 stderr（人读），退出码进 shell（脚本读）。"""
    print(message, file=sys.stderr, end="", flush=True)
    raise SystemExit(REFUSED_WORKER_BUSY)


def ensure_worker_is_exclusively_ours(
    node_id: str, sandbox_id: str, *, force: bool = False
) -> None:
    """删这台 worker 的 pod 之前，先确认它上面只有本次验收的沙箱。

    这条检查是给"共享集群 / CI / 两个人同时跑"用的：删 pod 会连带打死当时宿在它上面的
    任何别人的沙箱，而没有任何东西能把它们救回来。判据**可证伪**——名单必须真的被读到
    （读不到 = 拿不到证据 = 拒绝），并且必须真的包含本次验收自己的沙箱（名单连自己都
    不认 ⇒ 名单不可信 ⇒ 拒绝）。两者都有 `--force` 这个显式出口。
    """
    if force:
        step("politeness_overridden", node=node_id, sandbox=sandbox_id)
        return
    try:
        ids = node_sandbox_ids(node_id)
    except Exception as exc:  # noqa: BLE001 - any answer other than the list is a refusal
        _refuse_worker_pod(
            f"拒删 worker pod {node_id}：读不到控制面的按节点沙箱名单"
            f"（GET /internal/nodes/{node_id}/sandboxes）：{type(exc).__name__}: {exc}\n"
            "那张名单是「这台 worker 上没有别人的沙箱」的唯一证据；拿不到就不动 pod。\n"
            "出路：先把通道/控制面修好（deploy/scripts/open-cluster-tunnel.sh）再重跑；\n"
            "      确实要不看这张名单就删，用 `--force` 显式承担。\n"
        )
    others = sorted(set(ids) - {sandbox_id})
    if others:
        listed = "\n".join(f"  - {other}" for other in others)
        _refuse_worker_pod(
            f"拒删 worker pod {node_id}：它上面有 {len(others)} 个不属于本次验收的沙箱：\n"
            f"{listed}\n"
            "删 pod 会把别人的沙箱一起打死（worker 的注册表是内存态，它们无法再 resume）。\n"
            "出路：先把它们迁走或杀掉（带 API key 的 `POST /sandboxes/<id>/migrate`，"
            "或 `Sandbox.kill(id)`）再重跑；\n"
            "      如果你确认它们可以和这个 pod 一起死，重跑时加 `--force` 显式承担。\n"
        )
    if sandbox_id not in ids:
        shown = " ".join(sorted(ids)) or "空"
        _refuse_worker_pod(
            f"拒删 worker pod {node_id}：控制面的按节点名单里没有本次验收的沙箱 "
            f"{sandbox_id}（名单：{shown}）。\n"
            "名单连自己都不认的时候，「这上面没有别人」这句话不算数——所以不动 pod。\n"
            "出路：确认这条沙箱的记录还在（Sandbox.create 之后 `/internal/routes/<id>` 能查到）再重跑；\n"
            "      确实要不看这张名单就删，用 `--force` 显式承担。\n"
        )


def delete_worker_pod(node_id: str, sandbox_id: str, *, force: bool = False) -> str:
    """删掉宿主 worker 的 pod 并返回它的 uid —— 本脚本唯一会打死沙箱的动作。

    礼貌检查与这一行 `kubectl delete` 写在同一个函数里，是为了它们不能分家：
    脚本里其它的 pod 操作只有读（`get`/`logs`/`exec`），删只有这里一处。
    """
    ensure_worker_is_exclusively_ours(node_id, sandbox_id, force=force)
    uid_before = kubectl(
        "get", "pod", node_id, "-o", "jsonpath={.metadata.uid}"
    ).stdout.strip()
    kubectl("delete", "pod", node_id)
    return uid_before


def read_counter(sandbox, name: str = COUNTER) -> int | None:
    """The counter a background process keeps in memory; ``None`` if absent.

    Absent is a normal answer before the first tick -- the read API raises
    rather than returning empty, so it is spelled out here instead of leaking
    into every predicate.
    """
    from e2b.exceptions import FileNotFoundException

    try:
        raw = sandbox.files.read(name)
    except FileNotFoundException as exc:
        step("counter_missing", detail=str(exc))
        return None
    # A reader can land inside the writer's truncate-then-write window (`open(w)`
    # empties the file, the write follows), so an empty read is "not yet", not an
    # error.
    text = raw.strip()
    return int(text) if text else None


def diagnose(sandbox, pod: str, sandbox_id: str) -> None:
    """Say what the tree and the worker look like when the resume did not take."""
    for path in (".", "/home/user", f"/home/user/{COUNTER}"):
        try:
            step("diagnose_list", path=path, entries=[e.name for e in sandbox.files.list(path)])
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            step("diagnose_list_failed", path=path, detail=f"{type(exc).__name__}: {exc}")
    logs = kubectl("logs", pod, "--tail", "2000").stdout
    for line in logs.splitlines():
        if sandbox_id in line and "resume" in line.lower():
            step("diagnose_log", line=line.strip())


def wait_for(predicate, *, budget_s: float, what: str):
    deadline = time.monotonic() + budget_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.5)
    raise AssertionError(f"timed out after {budget_s}s waiting for {what} (last={last!r})")


def image_on_node(pod: str, sandbox_id: str, state_base: str) -> str:
    # The images live in the platform's own store, under the **state base** since
    # N27 (before that they were under the export root, which is also what a
    # deployment without `E2B_STATE_BASE` still looks like):
    # `<state base>/_runtime/.checkpoints/<id>/latest`.
    store = f"{checkpoint_store_root(state_base)}/{sandbox_id}"
    result = kubectl(
        "exec",
        pod,
        "--",
        "sh",
        "-c",
        f"ls -l {store} 2>&1 | head -20 "
        f"; du -sk {store}/latest 2>/dev/null "
        f"; ls {store}/latest 2>&1 | head -20",
        check=False,
    )
    shown = result.stdout.strip()
    if result.returncode != 0 and result.stderr.strip():
        # A dead tunnel answers with nothing on stdout and the reason on stderr;
        # without this the caller reports "no checkpoint image" and sends the
        # reader hunting for a write bug that is not there (measured
        # 2026-09-25 -- the image had in fact been written, the tunnel was down).
        shown = f"kubectl failed (rc={result.returncode}): {result.stderr.strip()}\n{shown}"
    return shown


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "生产形态的 pause → 换 worker → resume 验收。它在中途会删掉宿主 worker 的 pod，"
            "所以先确认那台 worker 上没有别人的沙箱（否则拒绝并退出码 2）。"
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "跳过「这台 worker 上没有别人的沙箱」这条礼貌检查。"
            "只在确认上面那些沙箱可以和这个 pod 一起死（= 再也 resume 不回来）时用。"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from e2b import Sandbox

    args = parse_args(argv)

    # The steps below that read a node or restart a worker go through `kubectl`,
    # which needs the tunnel (`deploy/scripts/open-cluster-tunnel.sh`). A dead
    # one fails *later* and in a way that looks like a checkpoint bug, so say it
    # here instead of three assertions in.
    probe = kubectl("get", "nodes", "-o", "name", check=False)
    assert probe.returncode == 0, (
        "kubectl cannot reach the cluster -- run "
        "deploy/scripts/open-cluster-tunnel.sh and retry; stderr: "
        f"{probe.stderr.strip()}"
    )

    # 开关必须真的在这版清单里（`deploy/k8s/worker.yaml`），且三件一起才构成"生产形态"：
    # 真根、pause 抓图、平台账。哪一个没开，下面那条验收的失败原因都不是引擎。
    assert worker_env("E2B_PAUSE_CHECKPOINT") == "1", (
        "worker 清单里 E2B_PAUSE_CHECKPOINT 不是 1；这一版 pause 不会写图，"
        "下面的验收测的不是这个功能"
    )
    assert worker_env("E2B_REAL_ROOT") == "1", "worker 清单里 E2B_REAL_ROOT 不是 1"
    assert worker_env("E2B_PLATFORM_DISK_MB") == "8192", (
        "worker 清单里的平台账预算不是 8192 MiB；图会以 0=不限 的形态落盘"
    )

    state_base = state_base_of()
    store_root = checkpoint_store_root(state_base)

    sandbox = Sandbox.create(api_url=API, sandbox_url=API, api_key=KEY)
    sandbox_id = sandbox.sandbox_id
    handle = None
    try:
        node = route_of(sandbox_id)
        step("created", sandboxID=sandbox_id, node=node["nodeID"], address=node["address"])

        # 先证明下面两次 `ls` 看的是**图真正落地的那层目录**（N27 之后它是
        # `<state base>/_runtime/.checkpoints`，不再是 `<export>/_runtime/.checkpoints`）。
        # "路径搬了"和"图没写"在输出上都是 `No such file or directory`，所以这一条要在
        # 任何一次捕获之前就断掉。
        layout = kubectl(
            "exec",
            node["nodeID"],
            "--",
            "sh",
            "-c",
            f"test -d {store_root} && echo present",
            check=False,
        )
        assert "present" in layout.stdout, (
            f"宿主 worker 的 checkpoint store {store_root}（state base {state_base}）不存在："
            f"布局搬家了（N27）或通道断了。rc={layout.returncode} "
            f"stderr={layout.stderr.strip()!r}"
        )
        step("layout", state_base=state_base, store=store_root)

        # Written to a file rather than passed inline: the SDK's `run` takes a
        # shell string, and a here-doc over Connect-RPC is an unnecessary place
        # to lose quoting.
        sandbox.files.write("tick.py", PROGRAM)
        # `exec` for the same reason as the second workload below: the worker's
        # shell has to *become* python, or the capture takes the shell.
        handle = sandbox.commands.run("exec python3 -u /home/user/tick.py", background=True)
        first = wait_for(
            lambda: (lambda v: v if v is not None and v >= 3 else None)(
                read_counter(sandbox)
            ),
            budget_s=60,
            what="the background process to tick",
        )
        step("running", counter=first)

        assert sandbox.pause() is True, "pause() did not report the new state"
        paused_at = read_counter(sandbox)
        assert paused_at is not None, "the counter vanished at the pause"
        time.sleep(4)
        frozen_at = read_counter(sandbox)
        assert frozen_at == paused_at, (
            f"the sandbox is not frozen: counter moved {paused_at} -> {frozen_at}"
        )
        step("paused", counter=paused_at, frozen_after_4s=frozen_at)

        shown = image_on_node(node["nodeID"], sandbox_id, state_base)
        assert "meta.json" in shown or "policy.dat" in shown, (
            f"no checkpoint image on the hosting node:\n{shown}"
        )
        step("image", node=node["nodeID"], listing=shown)
        step("platform_account", nodes=platform_account())

        # FUP-29's product shape: thaw the session that is still here and run a
        # command in it -- "capture, then exec in the same session". That used to
        # wedge about one run in three in the fork's *harness* (a single-threaded
        # runtime starved the sandbox's supervisor); the slot here is
        # multi-threaded, so this must not wedge, and the process must keep
        # ticking too (a thaw, not a restore).
        Sandbox.connect(sandbox_id, api_url=API, sandbox_url=API, api_key=KEY)
        ticked = wait_for(
            lambda: (lambda v: v if v is not None and v > paused_at else None)(
                read_counter(sandbox)
            ),
            budget_s=60,
            what="the thawed process to keep ticking",
        )
        step("thawed_kept_ticking", counter=ticked)

        # The deployment runs one command per sandbox at a time
        # (`max_concurrent_commands_per_sandbox` = 1), so the background counter
        # has to go before the next command -- otherwise it queues and the SDK
        # reports "command queue timed out after 30s", which is the deployment's
        # concurrency shape rather than anything about pause.
        assert handle is not None
        handle.kill()
        handle = None
        time.sleep(2)
        thawed = sandbox.commands.run("echo THAWED_OK")
        assert thawed.exit_code == 0, f"exec after a thaw failed: {thawed.stderr!r}"
        assert thawed.stdout == "THAWED_OK\n", f"unexpected stdout {thawed.stdout!r}"
        assert thawed.stderr == "", f"unexpected stderr {thawed.stderr!r}"
        step("exec_after_thaw", stdout=thawed.stdout)

        # A fresh long-lived process for the restart half: this is the one the
        # resume has to bring back from the image.
        sandbox.files.write("tick2.py", PROGRAM2)
        # `exec` **as the first word of the command string** so the worker's own
        # `/bin/sh -c` *replaces itself* with python: the session's live child is
        # then python, which is what the capture has to take. Measured 2026-09-25:
        # writing `sh -c 'exec python3 …'` instead makes the worker's shell fork a
        # second shell (the session child), so the capture took a *dash* -- the
        # image came back with 19 mappings and 388 KiB of anonymous memory (python
        # is ~40 mappings and several MiB), and the restore brought back a shell
        # whose only child was gone, which exits immediately. That looked exactly
        # like "the restored process dies".
        #
        # The redirects put stdio in files the worker can read afterwards (the
        # restore skips pipe fds, so a traceback on stderr would be lost).
        handle = sandbox.commands.run(
            "exec python3 -u /home/user/tick2.py "
            "> /home/user/out2.txt 2> /home/user/err2.txt",
            background=True,
        )
        second = wait_for(
            lambda: (lambda v: v if v is not None and v >= 3 else None)(
                read_counter(sandbox, "tick2")
            ),
            budget_s=60,
            what="the second background process to tick",
        )
        step("running_again", counter=second)
        step("boot_before_pause", content=sandbox.files.read("boot2.txt"))
        # Pause again, so the worker-restart half below exercises the image.
        assert sandbox.pause() is True
        paused_at = read_counter(sandbox, "tick2")
        assert paused_at is not None, "the second counter vanished at the pause"
        step("paused_again", counter=paused_at)

        # Replace the worker: the slot, and every process it held, dies with it.
        # 这是本脚本唯一会打死沙箱的动作，所以它先过礼貌检查：这台 worker 上只有
        # 本次验收自己的沙箱，才允许删；否则打印出路、退出码 2、pod 一个都不碰。
        uid_before = delete_worker_pod(node["nodeID"], sandbox_id, force=args.force)
        step("worker_deleted", pod=node["nodeID"], uid=uid_before)

        def replacement_ready():
            """A *different* pod object, running, with its container ready."""
            fields = kubectl(
                "get",
                "pod",
                node["nodeID"],
                "-o",
                "jsonpath={.metadata.uid} {.status.phase} "
                "{.status.containerStatuses[0].ready}",
                check=False,
            ).stdout.split()
            if len(fields) != 3:
                return None
            uid, phase, ready = fields
            return (uid, phase, ready) if uid != uid_before and ready == "true" else None

        step("worker_back", replacement=wait_for(
            replacement_ready, budget_s=240, what="the replacement worker to be ready"
        ))

        def cp_can_reach_it():
            try:
                route = route_of(sandbox_id)
            except Exception:
                return None
            return route if route.get("nodeID") == node["nodeID"] else None

        # `/internal/routes` answers 502 while the node is not healthy, and a
        # node is healthy only after its replacement has re-registered and
        # heartbeated -- which is also what makes the resume push reachable.
        step("worker_registered", route=wait_for(
            cp_can_reach_it, budget_s=240, what="the control plane to see the new worker"
        ))

        before_resume = read_counter(sandbox, "tick2")
        assert before_resume == paused_at, (
            f"the counter moved while the worker was gone: {paused_at} -> {before_resume}"
        )

        Sandbox.connect(sandbox_id, api_url=API, sandbox_url=API, api_key=KEY)
        try:
            resumed = wait_for(
                lambda: (lambda v: v if v is not None and v > before_resume else None)(
                    read_counter(sandbox, "tick2")
                ),
                budget_s=120,
                what="the resumed process to tick again",
            )
        except AssertionError:
            try:
                step("counter_now", raw=repr(sandbox.files.read("tick2")))
            except Exception as exc:  # noqa: BLE001 - diagnostics only
                step(
                    "counter_now_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            # Did the restored image run *any* Python? `boot2.txt` names the pid
            # that wrote it: the source process's pid before the capture, and a
            # new one if the restored process got that far.
            for probe_file in ("boot2.txt", "err2.txt", "out2.txt"):
                try:
                    step("restore_probe", file=probe_file, content=sandbox.files.read(probe_file))
                except Exception as exc:  # noqa: BLE001 - diagnostics only
                    step(
                        "restore_probe_failed",
                        file=probe_file,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
            diagnose(sandbox, node["nodeID"], sandbox_id)
            raise
        assert resumed - before_resume < 30, (
            f"the counter jumped {before_resume} -> {resumed}: that is not the same "
            "process continuing"
        )
        step("resumed", before=before_resume, after=resumed)

        echoed = sandbox.commands.run("echo EXEC_OK")
        assert echoed.exit_code == 0, f"exec after resume failed: {echoed}"
        assert echoed.stdout == "EXEC_OK\n", f"unexpected exec stdout: {echoed.stdout!r}"
        assert echoed.stderr == "", f"unexpected exec stderr: {echoed.stderr!r}"
        step("exec_after_resume", stdout=echoed.stdout)

        consumed = image_on_node(node["nodeID"], sandbox_id, state_base)
        assert "No such file or directory" in consumed, (
            f"the image was not consumed by the resume:\n{consumed}"
        )
        step("image_consumed", listing=consumed)

        logs = kubectl("logs", node["nodeID"], "--tail", "4000").stdout
        marker = "into the session (child "
        assert marker in logs, "the worker never logged a restore into a session"
        line = [ln for ln in logs.splitlines() if marker in ln][-1]
        step("worker_log", line=line.strip())

        step("OK")
        return 0
    finally:
        if handle is not None:
            try:
                handle.kill()
            except Exception:
                pass
        try:
            sandbox.kill()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
