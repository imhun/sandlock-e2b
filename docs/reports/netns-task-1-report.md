# Task 1 报告：给 prod-shaped lane 加 netns 形态通道

日期：2026-09-26 ｜ 工作目录：`/Users/polus/project/ai/sandlock-e2b`
简报：`.superpowers/sdd/netns-task-1-brief.md`（= `docs/superpowers/plans/2026-09-26-netns-shape-unification.md` 的 Task 1）

## 0. 结论

按简报做完：`deploy/scripts/test-prod-shaped.sh` 现在把
`E2B_ENABLE_NET_ISOLATION` / `E2B_FD_INJECT_CONNECT` / `E2B_TEST_NET_ISOLATION`
**绑成一个 `NETNS_ENV`** 透传进两个相位的 `docker run`，只设一半会 `exit 2`；
三条文本断言落成 `tests/unit/test_prod_shaped_lane_netns_passthrough.py`（3 passed）。

形态真跑跑完了：**netns 档 phase 1 = 1795 passed / 6 skipped / 3 xfailed / 0 failed，
phase 2（uid 65534）= 57 passed / 1 skipped / 0 failed**。简报 §Step 4 点名
"phase 2 不绿就不要往下做 Tasks 2–4" —— **phase 2 是绿的，Tasks 2–4 可以继续**。

⚠️ 但简报的**前提**（"contract 三条会被静默 skip"）**经实测不成立**，见 §5。
这条不改变交付物，但改变"为什么需要它"的叙事，需要你拍一下怎么改文档/后续简报表述。

## 1. 改了什么

| 文件 | 动作 |
|---|---|
| `deploy/scripts/test-prod-shaped.sh` | 在 `PIDNS_ENV` 块后新增 `NETNS_ENV` 块（+17 行）；两处 `docker run` 的 `-e` 列表各加一行 `$NETNS_ENV \`（+2 行） |
| `tests/unit/test_prod_shaped_lane_netns_passthrough.py` | 新建，逐字照抄简报 Step 1（三条断言） |

```diff
@@ PIDNS_ENV 块之后
+NETNS_ENV=""
+if [ -n "${E2B_ENABLE_NET_ISOLATION:-}" ] && [ -n "${E2B_FD_INJECT_CONNECT:-}" ]; then
+    NETNS_ENV="-e E2B_ENABLE_NET_ISOLATION=${E2B_ENABLE_NET_ISOLATION} -e E2B_FD_INJECT_CONNECT=${E2B_FD_INJECT_CONNECT} -e E2B_TEST_NET_ISOLATION=${E2B_TEST_NET_ISOLATION:-1}"
+elif [ -n "${E2B_ENABLE_NET_ISOLATION:-}${E2B_FD_INJECT_CONNECT:-}" ]; then
+    echo "!! E2B_ENABLE_NET_ISOLATION and E2B_FD_INJECT_CONNECT must be set together (create_app refuses the unpaired shape)" >&2
+    exit 2
+fi

@@ 两处 docker run 的 -e 列表（phase 1 `:202`、phase 2 `:234`）
     $PIDNS_ENV \
+    $NETNS_ENV \
     $CACHE_ENV \
```

未动 `# shellcheck disable=SC2086` 的写法（未加引号展开是刻意的，与 `$MIRRORS_ENV` 一致）。

## 2. Step 1–2：RED 原始输出

`tmp/testenv/bin/python -m pytest tests/unit/test_prod_shaped_lane_netns_passthrough.py -q -p no:cacheprovider`
（完整留证：`tmp/task1-red.log`）

```
FFF                                                                      [100%]
=================================== FAILURES ===================================
______________ test_lane_forwards_the_net_isolation_pair_when_set ______________

    def test_lane_forwards_the_net_isolation_pair_when_set() -> None:
        # Unset stays unset: the code default is the shared-netns shape.
>       assert 'NETNS_ENV=""\n' in LANE
E       assert 'NETNS_ENV=""\n' in '#!/bin/sh\n# Run the E2B suite ... fi\n'

tests/unit/test_prod_shaped_lane_netns_passthrough.py:25: AssertionError
______________ test_half_a_pair_is_refused_instead_of_silently_shared ____________

>       assert (
            '    echo "!! E2B_ENABLE_NET_ISOLATION and E2B_FD_INJECT_CONNECT must '
            'be set together (create_app refuses the unpaired shape)" >&2\n' in LANE
        )
E       assert '...' in '#!/bin/sh\n...'

tests/unit/test_prod_shaped_lane_netns_passthrough.py:36: AssertionError
________________ test_both_phases_carry_the_net_isolation_pair _________________

>       assert LANE.count("\n    $NETNS_ENV \\\n") == 2
E       AssertionError: assert 0 == 2
E        +  where 0 = <built-in method count of str object at 0x7f78a9bb2600>('\n    $NETNS_ENV \\\n')

tests/unit/test_prod_shaped_lane_netns_passthrough.py:46: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_prod_shaped_lane_netns_passthrough.py::test_lane_forwards_the_net_isolation_pair_when_set
FAILED tests/unit/test_prod_shaped_lane_netns_passthrough.py::test_half_a_pair_is_refused_instead_of_silently_shared
FAILED tests/unit/test_prod_shaped_lane_netns_passthrough.py::test_both_phases_carry_the_net_isolation_pair
3 failed in 0.08s
```

三条都红，与简报的"逐条期望输出"一致（第一条缺符号、第三条 `0 == 2`）。

## 3. Step 3–4：GREEN 原始输出

### 3.1 单测

`tmp/testenv/bin/python -m pytest tests/unit/test_prod_shaped_lane_netns_passthrough.py -q -p no:cacheprovider`

```
...                                                                      [100%]
3 passed in 0.03s
```

### 3.2 通道本身（`docker` 打桩，不起套件）

用 `tmp/shim/docker` 顶掉 `docker`，看脚本真正会敲的命令（只读、秒级）：

```
=== half pair: only E2B_ENABLE_NET_ISOLATION ===
!! E2B_ENABLE_NET_ISOLATION and E2B_FD_INJECT_CONNECT must be set together (create_app refuses the unpaired shape)
exit=2
=== half pair: only E2B_FD_INJECT_CONNECT ===
!! E2B_ENABLE_NET_ISOLATION and E2B_FD_INJECT_CONNECT must be set together (create_app refuses the unpaired shape)
exit=2
=== full pair (shimmed docker, both phases) ===
[E2B_ENABLE_NET_ISOLATION=true]
[E2B_FD_INJECT_CONNECT=true]
[E2B_TEST_NET_ISOLATION=1]
[E2B_ENABLE_NET_ISOLATION=true]
[E2B_FD_INJECT_CONNECT=true]
[E2B_TEST_NET_ISOLATION=1]
=== unset (shared shape) ===
0 netns flags (expected)
```

⇒ 两个相位各带三个 `-e`；只设一半 `exit 2`；不设则一个 netns 变量都不进容器。

### 3.3 netns 档全量（简报 Step 4 的命令，6–12 分钟那一步）

```bash
E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
E2B_ENABLE_NET_ISOLATION=true E2B_FD_INJECT_CONNECT=true \
  ./deploy/scripts/test-prod-shaped.sh 2>&1 | tee tmp/netns-unify-lane.log
```

```
SKIPPED [1] tests/contract/test_pure_shape_workspace_ownership.py:123: pure-sandlock contract requires an empty E2B_BASE_IMAGE (gate B shape)
SKIPPED [1] tests/security/test_template_isolation.py:169: the unprivileged worker shape both production manifests ship
SKIPPED [1] tests/unit/test_dir_ledger.py:281: root can read a 0000 directory
SKIPPED [1] tests/unit/test_uid_pool.py:469: the non-root worker shape cannot be asserted as root
SKIPPED [1] ../Users/.../tests/unit/test_worker_manifest_permissions.py:608: kubectl needed to render the kustomize overlay
SKIPPED [1] ../Users/.../tests/unit/test_worker_manifest_permissions.py:681: kubectl needed to render the kustomize overlay
1795 passed, 6 skipped, 3 xfailed, 11961 warnings in 454.22s (0:07:34)
==> phase 2: unprivileged worker (uid 65534 + the file-capability brokers)
SKIPPED [1] tests/security/test_template_isolation.py:101: the refusal is about an euid-0 in-process mediator
57 passed, 1 skipped in 13.71s
```

`tests/contract/test_mcp_netns.py` 的三条**没有 skip**：

```
$ rg -n 'SKIPPED.*test_mcp_netns\.py' tmp/netns-unify-lane.log
NONE (contract not skipped)
$ rg -n 'test_mcp_netns\.py' tmp/netns-unify-lane.log
41:tests/contract/test_mcp_netns.py: 68 warnings      # 模块被收集并执行（skip 则不会产生这些告警）
```

phase 2 = 57 passed 与 `docs/open-issues.md` OBS-5 行记的数字一致；**0 failed**。
> 注意 phase 2 的选择集（5 个 sandlock/route-B 文件）不含 netns 契约，所以 phase 2 证明的是
> "把 netns 开关作为 worker 默认值塞进去，无特权相位仍全绿"，不是"契约在无特权相位过了"。

### 3.4 同日共享 netns 档对照

简报要求"条数与**同日的共享 netns 档**逐条一致"。仓库里没有今天的共享档，
所以补跑了一遍同命令、**不带** netns 变量（`tmp/netns-unify-lane-shared.log`）：

```
FAILED tests/contract/test_snapshots.py::test_async_snapshot_answers_immediately_then_completes
1 failed, 1794 passed, 6 skipped, 3 xfailed, 11788 warnings in 453.96s (0:07:33)
```

逐条对照（收集总数两档都是 1804）：

| | netns 档 | 共享档 |
|---|---|---|
| passed | 1795 | 1794 |
| failed | **0** | **1**（`test_async_snapshot_answers_immediately_then_completes`） |
| skipped | 6（同上列 6 条，逐条相同） | 6（同上列 6 条，逐条相同） |
| xfailed | 3（N35 三条，逐条相同） | 3（N35 三条，逐条相同） |
| phase 2 | 57 passed / 1 skipped / 0 failed | 未跑（phase 1 非零 ⇒ `set -e` 在 phase 2 前中止） |

⇒ 差异只有那一条竞态失败，**净零**（1794+1 = 1795）。该失败与本次改动无关：
共享档的命令行里一个 netns 变量都没有（`rg -c` = 0，§3.2 的 unset 档同证），
而同一 shape **紧接着重跑一次全量 = 1795 passed, 0 failed**（`tmp/task1-snapshot-isolate-1.log`）
⇒ 时序敏感，不是形态相关。机制见 §6.2。

## 4. 命令清单（可复现）

```bash
# 1) RED
tmp/testenv/bin/python -m pytest tests/unit/test_prod_shaped_lane_netns_passthrough.py -q -p no:cacheprovider
# 2) GREEN
tmp/testenv/bin/python -m pytest tests/unit/test_prod_shaped_lane_netns_passthrough.py -q -p no:cacheprovider
# 3) 通道打桩（起套件前先确认命令行）
PATH="$PWD/tmp/shim:$PATH" E2B_ENABLE_NET_ISOLATION=true ./deploy/scripts/test-prod-shaped.sh   # => exit 2
# 4) netns 档全量
E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
E2B_ENABLE_NET_ISOLATION=true E2B_FD_INJECT_CONNECT=true \
  ./deploy/scripts/test-prod-shaped.sh 2>&1 | tee tmp/netns-unify-lane.log
# 5) 同日共享档对照
E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
  ./deploy/scripts/test-prod-shaped.sh 2>&1 | tee tmp/netns-unify-lane-shared.log
```

留证文件（都在项目内 `tmp/`）：`task1-red.log`、`task1-green-unit.log`、
`netns-unify-lane.log`、`netns-unify-lane-shared.log`、`task1-snapshot-isolate-1.log`、
`task1-snapshot-rerun-aborted.log`（被我中断的第二遍，仅部分输出）、`shim/docker`。

## 5. 与简报的差异

### 5.1 简报的前提不成立（重要）

简报（以及计划里"为什么排第一"整段）说：脚本没透传 ⇒ `tests/contract/test_mcp_netns.py`
**靠 `E2B_TEST_NET_ISOLATION=1` 的门控会被静默 skip** ⇒ 绿是"少跑三条的绿"。实测**不成立**：

1. `deploy/docker/Dockerfile.test-runner:96` 就有 `E2B_TEST_NET_ISOLATION=1`（镜像内置），
   加入时间 `407a59c 2026-09-03`；`docker inspect e2b-sandlock-test:latest` 的 `Config.Env`
   确认它在运行时容器里生效：

   ```
   E2B_BASE_IMAGE=python:3.11-slim
   E2B_TEST_NET_ISOLATION=1
   ```
2. 当年那份**被引用的日志**其实也没 skip 它：`tmp/prod-shaped-netns-on.log`（2026-09-16）
   的三条 skip 是 `test_pure_shape_workspace_ownership` / `test_template_isolation` /
   `test_uid_pool`，而该模块产生了 `tests/contract/test_mcp_netns.py: 45 warnings`
   ⇒ 3 skipped 从来不是这三条契约。
3. 契约的 worker 开关是**harness 自己设的**，与宿主 env 无关：
   `tests/contract/test_mcp_netns.py:68-70` 直接传
   `envd_settings_extra={"enable_net_isolation": True, "fd_inject_connect": True}`。
   所以 `E2B_ENABLE_NET_ISOLATION` / `E2B_FD_INJECT_CONNECT` 透传**不决定**契约跑不跑。
4. 直接反证：我这次同日共享档（**不带**任何 netns 变量）里该模块一样被执行、一样 0 failed
   （`tmp/netns-unify-lane-shared.log`）。

那么这条通道还剩什么价值（仍成立）：它让**整条套件**跑在"worker 默认形态 = netns"
（`E2B_ENABLE_NET_ISOLATION` 是 envd/CP 读的部署默认值）而不是共享 netns 默认值上；
Task 2–4 要证的是部署形态，这条通道是必要的。但**不能**再用"少跑三条的绿"这个理由。

### 5.2 phase 2 的缩进（风格差异，被迫的）

简报第三条断言硬编码 4 空格：`LANE.count("\n    $NETNS_ENV \\\n") == 2`，而 phase 2
的参数列表是 8 空格续行。两者不可能同时满足，必须选一个：

- **选了**：照简报 Step 3 的代码块字面（4 空格）插入，phase 2 因此是
  ```
          $PIDNS_ENV \
      $NETNS_ENV \        <-- 4 空格
          $CACHE_ENV \
  ```
  shell 语义完全一致（续行缩进对 shell 无意义），但看起来和邻居不齐。
- 没选：8 空格 + 改断言 —— 仓库测试规范禁止"为了通过而改期望"（"修复生产代码而非测试期望"），
  且简报的期望输出明确是 `3 passed`。

如果更在意脚本可读性，正确做法是**同时**改断言的 pattern（比如按 `$NETNS_ENV \\\n` 计数）
和缩进 —— 那属于改简报定的契约，我没自作主张，留给你拍。

## 6. 自审发现

### 6.1 三条断言是"文本钉钉子"，不是行为测试

它们断言脚本里的字符串，不执行脚本（本机 Docker 跑不了秒级）。真正的行为证据是 §3.2 的
打桩输出 + §3.3 的两档全量。Task 6 若引用这三条当"形态通道存在"的证据，应同时引 §3.3 的汇总行。

### 6.2 顺带撞出来的真实缺陷（不在本任务范围，建议转 N 项）

共享档里 `tests/contract/test_snapshots.py::test_async_snapshot_answers_immediately_then_completes` 红了一次：

```
control_plane/api/snapshots.py:565: in get_snapshot
    record = _snapshots(request).get(snapshot_id)
control_plane/registry/snapshots.py:427: in get
    payload = json.loads(path.read_text(encoding="utf-8"))
E   json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
   ... self = <JSONDecoder ...>, s = '', idx = 0
```

根因是**写记录不是原子的**：`_write_record`（`control_plane/registry/snapshots.py:404-409`）
先 `path.parent.mkdir(...)` 再 `path.write_text(...)`（截断后写），而 `get()` 是先
`path.is_file()` 再 `read_text()` —— 中间存在"文件已存在但内容为空"的窗口；异步快照
（`Prefer: respond-async`）恰好让"另一路在写、这一路在轮询"同时发生，于是读到空串。
证据：同一 shape 紧接着重跑全量 0 failed（时序敏感），且 netns 档同一条通过。
修法方向是写临时文件 + `os.replace`（或读侧容忍空文件重试），**我没动**（超出 Task 1 范围）。
文档里搜不到这条失败（`rg` docs/ 与 progress.md 均无），像是新暴露的。

## 7. 担忧

1. **§5.1 是这次最该被复核的一条**：简报的前提错了，Task 2–6 里凡引用"三条契约会被 skip"
   的表述（含 Task 6 的收口钉子、`docs/production-deployment-requirements.md` §2.4.5 的措辞）
   都需要改口径。请拍板：是把通道的理由改写成"整条套件的 worker 默认形态对齐部署形态"，
   还是顺带把 `Dockerfile.test-runner` 的 `E2B_TEST_NET_ISOLATION=1` 收回去（那会让契约
   重新依赖调用方 env —— 影响面更大，不建议单方面做）。
2. 共享档那次红是**偶发**：它证明两档"净零差"，但也意味着这条 lane 的 0 failed 不是
   稳的（今天两档合起来 3 次全量里红 1 次）。Task 2–4 拿它当验收证据时，红一次要按
   §6.2 的机制先判是竞态还是形态回退。
3. phase 2 的绿**不覆盖** netns 契约（选择集里没有它）。若后续要"无特权 worker + netns
   形态"的证据，得扩大 phase 2 的选择集 —— 那是形态/时长取舍，简报没写，我不自定。
4. 工作区是共享的：跑这条 lane 期间另一个 agent 提交了 3 个 commit（HEAD 从 `45acc9e`
   走到 `8ec5437`）。我的证据只依赖 `deploy/scripts/test-prod-shaped.sh` 与 tests 的当前内容，
   两者未被他人改动；但两档全量之间若有第三方改动落地，逐条对照的效力会打折。

## 8. 提交

```bash
git add deploy/scripts/test-prod-shaped.sh tests/unit/test_prod_shaped_lane_netns_passthrough.py
git commit -m "test(lane): let the prod-shaped lane reproduce the netns shape"
```

（精确两个文件，不用 `git add -A`。）

---

# 第二轮：复核后的返工（2026-09-26，同一份报告的续篇）

用户复核确认了 §5.1 的方向，并把结论钉得更死（三条事实见 §9.4）。本轮做了三件事：
改话术、修缩进/断言、同步计划与文档。**没有**重跑 6–12 分钟的形态全量（第一轮的
`1795/0` 与 `57/0` 仍是这次的证据，保留在 §3）。

## 9.1 话术改前 → 改后（三处）

### (a) 脚本注释（`deploy/scripts/test-prod-shaped.sh:163`）

**改前**
```
# ... and this lane exists to reproduce the deployed
# shape. The two are a pair -- `create_app` refuses the single-switch shape by
# name (it would leave every sandbox loopback-only, i.e. "the network is
# down" with no error anywhere) -- so forward them together, and only when
# both are set. Unset stays unset: the code default is the shared-netns shape.
# `E2B_TEST_NET_ISOLATION` is what un-skips `tests/contract/test_mcp_netns.py`
# (three cases); without it a "netns shape" run is green for the wrong reason.
```

**改后**
```
# ... and this lane
# exists to reproduce the deployed shape -- of the *whole* suite, because the
# in-process control plane and worker read these two variables as their
# deployment default (envd_service/config.py, pinned by
# tests/unit/test_net_isolation_config.py). Forward them when both are set;
# without that the lane could only ever run the code default (shared netns)
# shape, which is why the documented netns-shaped full run (§2.4.5) was not
# reproducible from the script.
# `tests/contract/test_mcp_netns.py` is *not* the reason: it never needed this
# passthrough (the runner image bakes `E2B_TEST_NET_ISOLATION=1`, and the
# contract's own harness sets the worker's pair), so it runs the same way
# whether the pair arrives here or not. `E2B_TEST_NET_ISOLATION` is forwarded
# only so the shape stays explicit end to end.
# The two are a pair -- `create_app` refuses the single-switch shape by name (it
# would leave every sandbox loopback-only, i.e. "the network is down" with no
# error anywhere) -- so forward them together, and only when both are set.
# Unset stays unset: the code default is the shared-netns shape.
```

删掉的错句：`E2B_TEST_NET_ISOLATION is what un-skips ...` / `green for the wrong reason`。
新增明确的一句：**契约不受本开关影响**（"is *not* the reason ... runs the same way whether
the pair arrives here or not"）。

### (b) 测试 docstring（`tests/unit/test_prod_shaped_lane_netns_passthrough.py:1`）

**改前**（4 行，核心错句如下）
```
`tests/contract/test_mcp_netns.py` gates itself on `E2B_TEST_NET_ISOLATION=1`
and the worker reads `E2B_ENABLE_NET_ISOLATION` / `E2B_FD_INJECT_CONNECT`, but
`deploy/scripts/test-prod-shaped.sh` forwarded only MIRRORS/MEMORY/PIDNS/CACHE
-- so the documented netns-shaped full run ... could not be reproduced
from the script: the netns contract would have been silently skipped instead.
```

**改后**：标题改成 *"must be able to run the **whole** suite in the deployed shape"*，
并把"这不是什么/这是什么"分成两条 bullet：契约**自给自足**（引 `Dockerfile.test-runner:93-96`
与 `_netns_servers()` 的 `envd_settings_extra`，明说 "Neither of the lane's switches gates or
shapes that module"）；本改动解决的是**整档的部署默认形态**（引 `envd_service/config.py:142/:150`
与 `tests/unit/test_net_isolation_config.py`）。删掉 "would have been silently skipped"。

### (c) 计划里的"为什么排第一"（`docs/superpowers/plans/2026-09-26-netns-shape-unification.md:121-123`）

**改前**
> ……而 `tests/contract/test_mcp_netns.py:31-37` 用 `E2B_TEST_NET_ISOLATION=1` 显式门控 ——
> 所以 §2.4.5 记的"netns 形态本机全量 1439 passed"……**用今天的脚本复现不出来**：
> contract 三条会被 skip，跑出来的绿是"少跑三条的绿"。

**改后**
> ……于是 §2.4.5 记的那次"netns 形态本机全量"……**用脚本复现不出来**。
>
> **2026-09-26 更正（Task 1 实测）**：原因**不是**"contract 三条会被 skip"——那是错的。
> ① 镜像自 `407a59c` 起 `ENV E2B_TEST_NET_ISOLATION=1`；② `_netns_servers()` 自己设 worker 的
> 两个开关 ⇒ 该契约**自给自足**……真正的原因是**另一件事**：lane 跑的是**整档**套件，而
> in-process 控制面/worker 的默认形态确实读那两个变量 ⇒ "用脚本跑出**部署形态的整档**"
> 在此之前做不到。

## 9.2 缩进与断言

### 改法

1. **脚本**：phase 2（`deploy/scripts/test-prod-shaped.sh:244`）的 `$NETNS_ENV \` 由 4 空格
   改成与邻居一致的 8 空格（phase 1 的 `:212` 仍是 4 空格，与它自己的邻居一致）。
2. **断言**：删掉硬编码列数的 `LANE.count("\n    $NETNS_ENV \\\n") == 2`，换成**结构断言**
   （新增 `_indent()` 辅助）：两条 `docker run`；`$NETNS_ENV \` 恰好两行；每行紧跟在
   `$PIDNS_ENV \` 之后；缩进与该 `$PIDNS_ENV \` 行相同；两行分属**两个不同**的 `docker run`。
   强度不降反升：从"4 空格至少有 2 处"变成"两个相位各一处、位置贴在既有透传形状之后、缩进正确"。
   半对报错的断言原样保留（`echo "!! ..." >&2` + `exit 2`）。

```python
def test_both_phases_carry_the_net_isolation_pair() -> None:
    lines = LANE.splitlines()
    runs = [i for i, line in enumerate(lines) if line.strip().startswith("docker run ")]
    assert len(runs) == 2

    forwarded = [i for i, line in enumerate(lines) if line.strip() == "$NETNS_ENV \\"]
    assert len(forwarded) == 2

    for index in forwarded:
        anchor = lines[index - 1]
        assert anchor.strip() == "$PIDNS_ENV \\"
        assert _indent(lines[index]) == _indent(anchor)

    owners = {max(run for run in runs if run < index) for index in forwarded}
    assert owners == set(runs)
```

### 新 RED（针对第一轮的 4 空格版脚本；`tmp/task1-round2-red.log`）

```
..F                                                                      [100%]
    for index in forwarded:
        anchor = lines[index - 1]
        assert anchor.strip() == "$PIDNS_ENV \\"
>       assert _indent(lines[index]) == _indent(anchor)
E       AssertionError: assert '    ' == '        '
E         Strings contain only whitespace, escaping them using repr()
E         - '        '
E         ?      ----
E         + '    '

tests/unit/test_prod_shaped_lane_netns_passthrough.py:78: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_prod_shaped_lane_netns_passthrough.py::test_both_phases_carry_the_net_isolation_pair
1 failed, 2 passed in 0.09s
```

（RED 是**断言真的抓到了缩进错**，不是"改测试凑绿"——正是第一轮我自己点出的那条。）

### 新 GREEN（`tmp/task1-round2-green.log`）

```
...                                                                      [100%]
3 passed in 0.03s
```

外加两条与第一轮同期的机械复查：`sh -n deploy/scripts/test-prod-shaped.sh` = syntax ok；
`docker` 打桩下"半对" `exit=2`、全对时两个相位各带一次 `E2B_ENABLE_NET_ISOLATION=true`（计数 2）。

## 9.3 计划改了哪几行

`docs/superpowers/plans/2026-09-26-netns-shape-unification.md`：

| 行 | 改动 |
|---|---|
| `:114` | Files 的行号/缩进说明（`:155-172` → `:155-180`，并写明"与所在列表的续行同缩进"） |
| `:121-123` | "为什么排第一"整段重写 + 新增 **2026-09-26 更正** 段（§9.1c） |
| `:127-148` | Step 1 的 python 代码块：docstring 换成新版本（与文件逐字一致，已用脚本核对） |
| `:158-207` | Step 1 的第三条测试：换成结构断言 + 新增 `_indent()` |
| `:185` | Step 2 的 Expected 补一句"若已存在缩进不对的版本，第三条改为报缩进不符（实测 `assert '    ' == '        '`）" |
| `:212-238` | Step 3 的插入代码块：脚本注释换成新版本（逐字核对通过） |
| `:250` | Step 3 的插入位置说明：`:184`/`:215` → `:212`/`:244`，并写明两处缩进不同 |
| `:274` | Step 4 的 Expected 1：补"**这一条只是回归检查**：该契约本来就自给自足，通道的作用是让整档跑在部署形态的默认值上" |
| `:950` | Task 6 的 Files：生产文档行号提示（§2.4.5 加了更正，`:534` 之后要重新核对） |

`docs/production-deployment-requirements.md`：

| 行 | 改动 |
|---|---|
| `:501-513`（新增 14 行） | §2.4.5 末尾追加 **2026-09-26 更正**：那次"netns 形态全量"是共享形态整档 + 自给自足的契约（两个宿主变量没进容器），并记下 2026-09-26 起可复现的数字（`1795/6 skipped/3 xfailed/0 failed` + phase 2 `57/1 skipped/0 failed`）与两档净零差 |

**Task 6 没有同样的错**：它是"消费 Task 1–5 的提交与 `tmp/` 证据"，不重复"会被 skip"的因果，故未改内容（只补了行号提示）。

**逐字核对**：用脚本把计划 Task 1 里的 python 代码块与
`tests/unit/test_prod_shaped_lane_netns_passthrough.py` 全文比对 = `True`；Step 3 的 sh 代码块与
`deploy/scripts/test-prod-shaped.sh:163-188` 比对 = `MATCH`。计划里的可照抄文本与仓库现状一致。

## 9.4 重写理由时**新核实到的事实**（各一行）

1. `envd_service/config.py:142` `E2B_ENABLE_NET_ISOLATION`（默认 `False`）、`:150` `E2B_FD_INJECT_CONNECT`（默认 `False`）—— 这就是"in-process 控制面/worker 的部署默认形态"的读取点；配对守卫在同文件 `:472`（`NET_ISOLATION_PAIRING_ERROR`）。
2. `tests/unit/test_net_isolation_config.py` 用 `monkeypatch` 分别 `delenv/setenv` 这两个变量，并钉住"配对才允许启动"⇒ `E2B_ENABLE_NET_ISOLATION`/`E2B_FD_INJECT_CONNECT` 是由**测试自身**读的部署形态开关（lane 透传确实改变整档的形态）。
3. `deploy/stack/docker-compose.prod.yml:209-210`（worker）/`:351-352`（worker2）以 `${E2B_ENABLE_NET_ISOLATION:-true}` / `${E2B_FD_INJECT_CONNECT:-true}` 下发 ⇒ 出厂形态默认开，lane 透传是"与出厂一致"而不是"打开一个新特性"。
4. `deploy/docker/Dockerfile.test-runner:93-96` 一行 `ENV` 里同时有 `E2B_BASE_IMAGE=python:3.11-slim` 与 `E2B_TEST_NET_ISOLATION=1`，`git log -S` 指向 `407a59c`（2026-09-03）⇒ 门控在镜像里已存在 **23 天**，不是最近才有的行为。
5. 第一轮 §5.1 说的"09-16 日志里契约执行过"可复核：该日志 `tests/contract/test_mcp_netns.py: 45 warnings`，且 3 条 skip 是 pure-shape / template_isolation / uid_pool。

## 9.5 本轮的提交与留证

| commit | 内容 |
|---|---|
| `d93123d` test(lane): pin the netns passthrough structurally and say what it is for | `deploy/scripts/test-prod-shaped.sh` + `tests/unit/test_prod_shaped_lane_netns_passthrough.py`（话术 + 缩进 + 结构断言） |
| `168f193` docs(plan): Task 1 — the netns gate was never skipped, the lane's value is the whole-suite shape | `docs/superpowers/plans/2026-09-26-netns-shape-unification.md` + `docs/production-deployment-requirements.md` |
| `4bc338e` docs(plan): correct the Task 6 line-number note after the §2.4.5 correction | 上面 Task 6 那行的行号提示（`+9` → 实测 `:501-513`，14 行）—— 单独一个小 commit 避免 amend 已落地的 `168f193` |

新增留证：`tmp/task1-round2-red.log`、`tmp/task1-round2-green.log`（第一轮的 `task1-red.log` /
`task1-green-unit.log` / `netns-unify-lane.log` / `netns-unify-lane-shared.log` 原样保留）。

本轮**未跑** 6–12 分钟形态全量（按用户指示），故 §3.3/§3.4 的 `1795/0`、`57/0` 仍是唯一形态证据；
本轮改动只涉及注释文本、一处缩进与测试断言，`sh -n` 与打桩复查已覆盖其正确性。

## 9.6 本轮的自审

- 断言改用 `line.strip().startswith("docker run ")` 定位两个相位：脚本若将来重构 `docker run`
  的写法（例如换成 `docker compose run`），这条会红 —— 这正是想要的（形态通道不该悄悄换载体）。
- 结构断言依赖"`$NETNS_ENV \` 紧跟在 `$PIDNS_ENV \` 之后"：与简报要求的插入位置一致；若将来有人
  把它挪到别处（哪怕仍然两处齐全），测试会红并要求同步更新意图，符合"精确钉钉子"。
- §2.4.5 的更正使该文件多 9 行，Task 6 里 `:534` 之后的引用行号会漂 —— 已在 Task 6 的 Files 行写明。
- 仍未解决、留给用户的仍是第一轮 §7 的第 1、3 条（是否把镜像里的 `E2B_TEST_NET_ISOLATION=1`
  收回；phase 2 的选择集是否扩到含 netns 契约）。
