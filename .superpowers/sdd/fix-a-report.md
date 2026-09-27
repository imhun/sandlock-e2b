# C1 fix-a 报告：四条 C 侧 / 清单侧记账项（A2、A4、A5、A7）

日期：2026-09-27 ｜ 工作目录：`tmp/wt-c1-fix-a` ｜ 分支：`feat/c1-fix-a` ｜ 开工 HEAD：`ff04e76`
写集（只碰这四个）：`deploy/priv/maint.c`、`deploy/k8s/priv-broker.yaml`、
`tests/contract/test_broker_socket_c.py`、`tests/unit/test_worker_manifest_permissions.py`。
`deploy/priv/priv_common.{c,h}` **未动**（这一轮不需要）；`envd_service/**` **未动**。

---

## 0. 结论（先给结论）

1. **四条全收**：A2 拒绝路径改为"非阻塞首读、无数据才退到有界 poll"；A4 新增健康 socket
   并把探针直连它、broker cap 从五条收到三条；A5 把 `E2B_BROKER_REQUEST_READ_MS` 显式写进
   清单（值 == 代码默认 30000）；A7 给 `walk` 一个自己的 64 MiB 未转义上限，其它 verb 仍 256 MiB。
2. **协议面未动**：`v` / `args` / `timeout_s` / `ok` / `exit` / `stdout` / `stderr` / `hello` /
   `roots` / `uid_pool` / `peer_uid` / `peer_gid` 一个没加没减；`exec` 形态（argv 形状、白名单、
   uid/gid 门）逐字不变。唯一变化的字符串是 **walk 超限拒绝**的输出上限文案（`… -byte walk
   output cap …`），它是 A7 新增的分支；**非 walk 的文案逐字不变**
   （`stdout exceeded the 268435456-byte output cap and was killed`）。
3. **判据（本机实测输出见 §6）**：契约 lane `28 passed`（开工 23）、单测清单 `53 passed`、
   `kubectl kustomize deploy/k8s | kubectl apply --dry-run=client -f -` 8 个对象全部
   `created (dry run)`；顺带把 `tests/contract/test_broker_socket_identity.py` 一起跑也是绿的（34 passed）。
4. **每条都有"撤销即红"证据**（§2..§5 各列变异与失败断言原文）。
5. **一条与任务书前提不符的实测，必须先说**（§1）：旧代码并不是"无条件等满 50 ms"——它
   `poll` 在"对端已说话/已挂断"时立刻返回。真正付 50 ms 的只有"连着但一直不出声"的对端，而那
   正是新代码**仍然**要等的那一格（答案不能被 close 的 RST 吞掉）。所以这条改动的可观测面是
   **分支**而不是耗时；测试据此设计（分支报告 + 一条防"无条件等"的延迟断言），详见 §1。

---

## 1. A2：拒绝路径不再固定等 50 ms —— 代码按任务书改，证据设计按实测走

### 1.1 改法（`deploy/priv/maint.c`）

- `refuse_connection()` 拆成：**一次非阻塞读**（`drain_available()`，`recv(MSG_DONTWAIT)`）→
  拿到数据就非阻塞排空到底、看到 EOF 就直接收工，**全程不碰计时器**；只有在"EAGAIN 且一个字节
  都还没有"时才退回到**一次**有界 `poll(PRIV_REFUSAL_WAIT_MS)`，之后照旧非阻塞排空、永不重复等待。
- 新增诊断开关 `E2B_BROKER_REFUSAL_TRACE`（**默认关**）：开的时候拒绝路径报它走了哪个分支
  （`…the peer had already spoken: N bytes drained without waiting` /
  `…the peer had already hung up: nothing to drain` /
  `…the peer has not spoken yet: waiting up to 50 ms for its request`）。
  **不入清单**：它是排障开关，不是运维参数；默认关意味着既有"每连接一行 `refused:`"的日志钉子
  完全不受影响（下面 3 条老用例的精确日志断言都还是绿的）。

### 1.2 为什么必须用"分支报告"而不是纯计时断言（实测）

用同一份源码、同一台机器（容器内，旧代码与新代码各跑一遍；脚本 `tmp/a2-probe/probe.py`，
量的是 daemon **accept 循环**的推进——每个连接的 `refused:` 行是答案写出**之前**打的）：

| 对端行为 | 旧代码每次接受耗时 | 新代码 |
|---|---|---|
| connect 后立刻 close（EOF） | 0.1–0.4 ms | 0.0–0.1 ms |
| connect 后立刻发请求并等答案 | 0.0 ms | 0.0–0.1 ms |
| **connect 后既不发也不关（沉默且在连）** | **51.3 ms** | **51.1 ms** |

也就是说：任务书说的"无条件等 50 ms"在旧代码里并不成立（`poll` 只要 socket 可读就立刻返回），
而唯一真付 50 ms 的沉默对端，新代码**按设计仍然要等**（这就是"答案可靠送达"那一格）。
纯 `time.monotonic()` 断言在新旧代码上都是绿的 ⇒ **不能**当 RED 证据。改用：

- **主证据**：daemon 自己的分支报告（撤销/变异即红，见 §1.3）；
- **辅助证据**：两条"请求已经在 socket 里"的拒绝连着做（用 `SIGSTOP` 冻结 daemon 后写入，所以
  "已经在"是事实不是竞态），第二个的答案必须在 `< PRIV_REFUSAL_WAIT_MS/2` 内到——它专门防
  "无条件等 50 ms"这类回归（那种实现会让第二个答案晚 50 ms）。

### 1.3 RED 证据（两次变异，各自红一次）

1. **把 `poll` 放回 recv 前面**（保留分支报告）：
   `assert spoken.stop() == …` 红，日志里出现
   `+ he peer has not spoken yet: waiting up to 50 ms for its request`（本该是
   `the peer had already spoken: 24 bytes drained without waiting`）。
2. **关掉分支报告**（模拟"这条改动被抹掉"）：
   `assert spoken.stop() == …` 红，缺两行
   `- e2b-maint: refusal trace: the peer had already spoken: 24 bytes drained without waiting`。

新增用例：`tests/contract/test_broker_socket_c.py::test_a_refusal_only_waits_for_a_peer_that_has_not_spoken_yet`
（含"沉默对端确实走有界等待"的正向外对照——删掉等待那一格它也会红）。

---

## 2. A4：健康 socket —— 探针不再借 worker 身份，broker 不再要 SETUID/SETGID

### 2.1 C 侧

- `e2b-maint serve [--socket P] [--health-socket P]`：**不传**（或传空值）＝与今天一模一样，
  只服务一个 socket。`--health-socket` 与 `--socket` 同名 → 启动即拒（`refused: the health socket
  and the broker socket are the same path: …`，且在建任何文件之前判）。
- 健康 listener：`0660 root:root`（`bind_listener()` 的 group 传 0），只答
  `{"v":1,"hello":true}`（带 `args` 的同形请求也拒：`the health socket answers only {"v":1,"hello":true}`），
  **不查 `E2B_BROKER_PEER_UID`**；门是"uid 必须 0"，非 root 对端点名拒
  （`peer uid N is not root: the health socket is for this container's own probe`）。
  两个 listener 用一个 `poll(…, -1)` 等（谁也饿不死谁），门仍在**父进程、fork 之前**；
  健康连接也 fork 一个 handler（root-only，但"连上就不说话"的 root 对端不该占住 accept 循环）。
- **业务 socket 一字未改**：`0660 root:<peer gid>`、`SO_PEERCRED` 父进程门、不过门不 fork。
  既有用例 `test_hello_rejects_a_peer_uid_mismatch` / `…gid_mismatch`（`assert _handlers(handle) == []`）
  继续压住"门在父进程"。
- 真机形态（写进 yaml 注释）：`/run/e2b-broker` 目录仍是 `0710 root:65534`（`socket-dir-init`），
  `broker.sock` 由 daemon 建成 `0660 root:65534`，`health.sock` 建成 `0660 root:root` ——
  worker（65534）**连不上健康 socket**，这是**有意**的。

### 2.2 清单侧（`deploy/k8s/priv-broker.yaml`）

- command 加 `--health-socket /run/e2b-broker/health.sock`；
- 两条探针改成**直接** `/var/lib/e2b-priv/e2b-maint ping --socket /run/e2b-broker/health.sock`
  （`setpriv` 整段删掉）；
- cap 集 `drop: [ALL]` + `add: [CHOWN, DAC_OVERRIDE, FOWNER]`（三条）；
- 注释写清"健康 socket 只有 root 能连，所以探针不需要降权；业务 socket 的门没动"，
  以及文件头加了 A4 一段。

### 2.3 测试与 RED 证据

新增/改写：

- 契约（C 侧，容器内 root）：`test_the_health_socket_is_root_only_and_answers_the_probe_hello`
  （`0660 root:root`、业务 socket 仍 `0660 root:<peer gid>` 且仍拒 root 且不为它 fork、健康 socket
  直答完整 hello、`ping --socket <health>` rc=0、verb 与带 args 的 hello 都被点名拒、
  daemon 仍活着）、`test_the_health_socket_gate_refuses_a_non_root_peer_by_name`
  （先 `0660` 挡住池 uid 的 `connect()`，再把模式放宽到 `0666` 证明**门本身**点名拒）、
  `test_serve_refuses_a_health_socket_that_is_the_broker_socket`。
- 单测（清单侧）：`test_the_baseline_renders_the_root_broker_daemonset_with_the_workers_identity`
  里命令/`capabilities`/两条探针的精确断言（探针路径从**同一容器的 argv** 里取，探针与 daemon
  不能漂移；业务 socket 门仍在父进程这条由上面契约用例复用），以及改写后的
  `test_the_broker_probes_dial_the_root_only_health_socket`（整段无 `setpriv`、cap 里没有
  `SETUID`/`SETGID`）；`test_socket_transport_and_its_broker_are_inseparable`（两种 render）
  也同步到新 argv，并断言健康 socket 与业务 socket 同目录。

RED（三种撤销，各自红）：

1. **C 侧忽略 `--health-socket`**（`*health_path = NULL` 一句模拟"这个 flag 没接上"）：
   3 条健康 socket 契约用例红（daemon 只报 `serving on …`、没有 `health on …`；同名用例直接超时）。
2. **探针改回业务 socket**：
   `AssertionError: livenessProbe must dial this container's own root-only health socket
   (/run/e2b-broker/health.sock) …` + 基线 DaemonSet 那条红（2 failed）。
3. **cap 加回 `SETUID`/`SETGID`**：
   `Extra items in the left set: 'SETGID', 'SETUID'` + `assert 'SETUID' not in [...]`（2 failed）。

---

## 3. A5：请求读截止上清单

- `priv-broker.yaml` 的 broker 容器 env 里显式写 `E2B_BROKER_REQUEST_READ_MS: "30000"`（= 代码默认），
  注释说明它是**保护本节点 broker 的运维旋钮**：过门后迟迟不发请求的对端，读截止到点只拒当前连接
  （`ok:false`，点名 "the request was not sent within N ms"），daemon 不退出；**代码默认值没动**。
- pin：`tests/unit/…::test_the_broker_names_the_request_read_deadline` 渲染清单并把该值与
  `maint.c` 的 `PRIV_DEFAULT_REQUEST_READ_MS`（从源码里读）比等；基线 DaemonSet 那条也断言同一值。
- RED：把 env 删掉 → 上述两条红（`KeyError: 'E2B_BROKER_REQUEST_READ_MS'`）。

---

## 4. A7：walk 自己的 64 MiB 上限

### 4.1 C 侧

- 常量区新增 `PRIV_MAX_WALK_OUTPUT = 64 MiB`（注释写推导：单树 ≤ `E2B_DISK_MAX_ENTRIES`=500000 条目
  × ~80 B ≈ 40 MB ⇒ 64 MiB 留 1.6×；64 MiB × 6（最宽转义）= 384 MiB < worker 侧 512 MiB 线路上限
  ⇒ 合法答案永远先被 daemon 自己以 `ok:false` 拒掉，worker 侧只用于防冒充者）。
  该 define 包在 `#ifndef` 里：契约 lane 会**用同一份源码**加 `-DPRIV_MAX_WALK_OUTPUT=…` 重编，
  用来在不产生 64 MiB 流量的前提下驱动一次真实的"超限"（裸 `#define` 会变成重定义告警，破坏
  这条 lane 的 `-Wall -Wextra` 干净断言）。
- `struct output` 增加 `limit` / `limit_name`，`output_append()` 用 `sink->limit`；
  `set_output_limits()` 按 `args[0] == "walk"` 选 64 MiB（名字 `walk output`），其余 verb 256 MiB（名字 `output`）。
  超限文案改成 `%s exceeded the %llu-byte %s cap and was killed`：非 walk 逐字不变，walk 为
  `stdout exceeded the 4096-byte walk output cap and was killed`（**点名**）。
- `envd_service/priv_helpers.py` **未动**（另一个 agent 的文件，由它把注释对齐 64 MiB）。

### 4.2 测试与 RED 证据

- 契约：`test_walk_has_its_own_smaller_output_ceiling` —— 先用**同一份源码 + 小上限**（4096 B）重编并
  临时安装（用例结束时还原，模块夹具不受影响），200 个条目的树先被直接 `walk` 一遍证明输出
  **确实超过**该上限，再断言 daemon 回 `ok:false` + 点名文案，且 daemon 仍活着；
  最后从源码断言 `PRIV_MAX_WALK_OUTPUT == 64 MiB`、`PRIV_MAX_OUTPUT == 256 MiB`、
  `64 MiB < 256 MiB`、`6 × 64 MiB < 512 MiB`。
- RED：把 walk 也放回 256 MiB 那个上限 → 断言红
  （`{'ok': True}` ≠ `{'ok': False, …}` —— 请求被正常执行了）。

---

## 5. 没碰的东西 / 纪律

- 未碰集群（没有 `kubectl get/apply` 真操作，只有 `kustomize` + `--dry-run=client`）。
- 未新增 skip/xfail；未过滤输出；断言都是精确相等（`_response(...) == {...}`、整段日志相等）。
- 临时物都在项目内 `tmp/`（`tmp/a2-probe/`），未提交。

---

## 6. 复现（判据 + 实际输出）

```console
$ docker run --rm --security-opt seccomp=$PWD/deploy/seccomp/sandlock-worker.json \
    -v $PWD:/w -w /w e2b-sandlock-test:latest sh -c \
    'cc -O2 -Wall -Wextra -o /tmp/m deploy/priv/maint.c deploy/priv/priv_common.c && \
     pytest tests/contract/test_broker_socket_c.py -q -p no:cacheprovider'
............................                                             [100%]
28 passed in 17.26s

# 同 lane 一起跑（确认身份那条契约也要绿）
$ … pytest tests/contract/test_broker_socket_identity.py tests/contract/test_broker_socket_c.py -q -p no:cacheprovider
..................................                                       [100%]
34 passed in 19.69s

$ tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py -q -p no:cacheprovider
.....................................................                    [100%]
53 passed in 3.41s

$ kubectl kustomize deploy/k8s | kubectl apply --dry-run=client -f -
deployment.apps/autoscaler created (dry run)
deployment.apps/control-plane created (dry run)
deployment.apps/redis created (dry run)
statefulset.apps/e2b-worker created (dry run)
poddisruptionbudget.policy/control-plane created (dry run)
poddisruptionbudget.policy/e2b-worker created (dry run)
daemonset.apps/e2b-priv-broker created (dry run)
daemonset.apps/seccomp-installer created (dry run)
```

A2 耗时对照（§1.2 的表，重跑命令见 `tmp/a2-probe/probe.py`）：

```console
$ … python3 tmp/a2-probe/probe.py 1     # 新代码
close  [0.1, 0.1, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0]
data   [0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
silent [0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
stall  51.1
```

单测全量对照（证明没有回归，43 个失败全是**开工 HEAD 就有**的环境问题）：

```console
$ tmp/testenv/bin/python -m pytest tests/unit -q -p no:cacheprovider     # 本树
43 failed, 1660 passed, 12 skipped, 2 warnings in 132.17s
$ … 同上，在 ff04e76 的干净 worktree 里
43 failed, 1660 passed, 12 skipped, 2 warnings in …
$ diff base.txt mine.txt && echo IDENTICAL-FAILURE-SETS
IDENTICAL-FAILURE-SETS
```

---

## 7. 疑虑 / 需要对方决定的事

1. **A2 的分支报告是新增诊断开关**（`E2B_BROKER_REFUSAL_TRACE`，默认关，不入清单）。如果评审
   不接受 daemon 里出现"主要服务于测试可观测性"的开关，替代方案是把报告并进 `refused:` 日志行
   —— 代价是"对端已挂断"那种竞态下**每个连接的日志行数不再固定**，会打断现有
   `test_a_peer_that_hangs_up_cannot_take_the_broker_down` 的精确日志断言（故未选）。
2. **docs 里仍有旧探针形态**（`setpriv` 降权连业务 socket）：`docs/deploy-clusters.md` §7、
   `docs/production-deployment-requirements.md`（F4 那段与 §2.7.x 附近）、
   `docs/superpowers/plans/2026-09-27-priv-broker-externalization.md`（item 4 / item 8 / Task 8 表格）。
   这些文件不在本单写集（只许四个文件），请 planner 决定是否另开一张小单同步。
3. `envd_service/priv_helpers.py` 的 `BROKER_MAX_OUTPUT_BYTES` 注释把 `walk` 也算在 256 MiB 里 ——
   那是另一个 agent 的文件（本单按纪律未动），它承诺把注释对齐 64 MiB。
4. **未在 k0s 真机验证**（本单按纪律不碰集群）：健康 socket 的 `0660 root:root`、探针以容器 root
   直连、以及 DaemonSet 起 pod 后的 Ready 状态，需要在下一次 `apply.sh` 时看一次；容器 lane 已把
   mode/owner、root 直答、非 root 拒、业务 socket 仍拒 root 这几条覆盖住。
5. A2 的语义边界：健康 socket 的非 root 拒绝也走同一条 `refuse_connection()`（同一 drain + 同一
   分支报告），这是有意的——两个 listener 的拒绝路径不该有两套行为。
