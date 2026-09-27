# N37 报告：单条命令写 4000 个文件为什么会被截断

**Status**：根因已定位（**API 前面的边缘对「静默 60.0 s 的响应体」做空闲切断**，
而 envd 的 process 流从不发协议里的 in-band `KeepAlive`）；本仓侧最小修复 + 先红后绿用例已完成，
本机端到端验收（含"单命令写 4000 个文件 ×3 连续通过"）已跑通。
**唯一未做**：镜像重建与上集群（本报告 §6 给出确切命令与判定）。

**一行摘要**：变量不是"单条命令里的文件数"而是**那条命令保持静默的时长**——
集群上 2000 文件 ≈ 47 s（< 60 s）通过、4000 文件 ≈ 94 s（> 60 s）在 61.4 s 被切断；
把 `kubectl port-forward` 直连 control-plane（绕过边缘）后静默 90 s 也能通过，
而 SDK 早就用 `Keepalive-Ping-Interval` 要求过 in-band ping，只是我们的 process 流没实现。

---

## 1. 结论（先看这一节）

1. **现象的真因在命令流的"静默时长"上**：客户端 → `172.18.78.49:3000`（边缘）→
   `.140:31907` 这条路上，**响应体静默 60.0 s 就被切断**，SDK 把它报成
   `TimeoutException: … unexpected EOF during chunk size line`。任何静默超过 60 s 的命令都会死，
   与它做什么毫无关系（`sleep 90` 一样死）。
2. **N37 原来记的"变量是单条命令内文件数"是相关而不是因果**：NFS 上写一个文件 ≈ 23 ms，
   2000 文件 ≈ 47 s（过），4000 文件 ≈ 94 s（死），三条 2000 分开跑各自 ≈ 47 s（都过）。
   同一份 4000 文件的工作，只要**每 100 个打印一行**（保持流有字节）就在 94.0 s 通过。
3. **平台侧缺失的是协议里现成的东西**：官方 SDK 在 `process.Process/Start` 上发
   `Keepalive-Ping-Interval: 50`（`e2b/connection_config.py`），协议里也有空的
   `ProcessEvent.KeepAlive`；本仓的 **filesystem watch 一直每 15 s 发**，而
   **process 流（`envd_service/rpc.py::_consume_stream`）只转发 data/end，一次都不发**；
   网关 `_FORWARD_HEADERS` 还把这个头丢掉了（所以客户端连"要求更短间隔"这个杠杆也没有）。
4. **原来那些 worker 侧日志全是"果"**：`output loop failed`、`route-B instance … is closed
   (slot released)`、`events pump ended after N events`、`cannot read fdinfo` 四行，
   在**任何**一次正常 `sandbox.kill()` 拆箱时都会按同样顺序出现（本机 docker 已复现，§3.1），
   因为客户端超时后脚本 `finally: sandbox.kill()` 把沙箱拆了。失败发生前 worker 侧**没有任何错误**。

---

## 2. 假设 → 判别实验 → 结果

所有集群实验都通过 `tmp/n37/cluster_keepalive_probe.py` / `cluster_run.py` 走**和事故同一条路**
（`E2B_API_URL=E2B_SANDBOX_URL=http://172.18.78.49:3000` + SDK + `X-API-Key` from `e2b-secrets`），
版本 `0.1.0-597-g3701a53-20260926-163057`。每组都是**新沙箱 + 一条命令**。

| # | 假设 | 判别实验 | 实测结果 | 裁决 |
|---|---|---|---|---|
| H1 | 单条命令的**文件数/事件条数**撞上某个上界（事件泵缓冲、`MAX_WRITE_FDS=4096`、条目门、dirty 账本） | ① 本机 docker（x86_64、overlay、route-B slot 全开）单命令写 4000 文件；② 集群单命令写 4000 文件但每 100 个 `print` 一行 | ① 1.6 s 通过（无任何 worker 侧异常）；② 94.0 s 通过 | **否证**（文件数不是变量） |
| H2 | 变量是**单条命令保持静默的时长**（路径上某个空闲超时） | ① 静默 90 s；② 静默 50 s；③ 每 5 s 输出一行、共 95 s；④ `kubectl port-forward svc/control-plane 13000:3000` 直连（**绕过边缘**）后重跑静默 90 s | ① **60.0 s 失败**（复跑 60.5 s），错误与 N37 逐字一致；② 50.7 s 通过；③ 95.5 s 通过；④ **90.5 s 通过**；⑤ 同样 4000 文件：静默 94 s 失败 61.4 s / 每 100 个打印一次 94.0 s 通过 | **成立**：切口是**边缘**对静默响应体的 60.0 s 空闲切断；k8s 栈内没有这个切口 |
| H3 | `slot released` / `output loop failed` 是**因**（中介或账本把槽位搞死） | 对照"正常拆箱"是否出现同样四行：本机 docker 跑一条**必过**的命令，然后 `kill()`；再逐行读集群失败前后的 worker 日志 | 正常拆箱同样按序输出 `sandlock instance closed` → `output loop failed`（wait_child 打到已 closed 的 instance）→ `is closed (slot released)` → `events pump ended` + `cannot read fdinfo`；集群失败前 worker 只有 heartbeat，DELETE 是**客户端超时后 kill 触发的** | **否证**（是果，不是因） |
| H4 | `cannot read fdinfo` 与槽位释放同源（watch 丢 fd / mediator 崩） | 看这条消息出现的时机与 slot 的退出方式：是否只在一拍内出现、slot 是否异常退出 | 只在拆箱那一拍出现（`fd` 已被沙箱关闭，读 `/proc/<pid>/fdinfo/<fd>` 自然 ENOENT；打印本身有 6 次上限），slot 是被 `shutdown` 正常收走的，stderr 里没有 mediator panic | **否证**（同属"果"；watch 的丢弃路径按设计工作） |

补充（不是假设，是读数）：本机 docker 上 4000 文件命令即使被 `--sleep-ms 20` 拉长到 **86.7 s**
也通过——因为本机没有那个 60 s 切口；这条同时证明"时长本身无害，静默 + 切口才致命"。

---

## 3. 最小复现

### 3.1 本机（推荐，不需要集群）

- **引擎/worker 形态**：`e2b-sandlock-test:latest` 容器，`--cap-drop ALL` + 声明 caps +
  `deploy/seccomp/sandlock-worker.json`（与 `deploy/scripts/test-prod-shaped.sh` phase 1 同款），
  栈用 `tests.conftest._start_multinode`（control plane + 1 worker(`executor=auto` → chroot → route-B slot) + gateway），
  SDK 走官方 `e2b`。脚本：`tmp/n37/run-repro.sh`（直接栈）与 `tmp/n37/run-relay.sh`（带边缘）。
- **把边缘搬进本机**：`tmp/n37/relay_probe.py` 在 SDK 与控制面之间放两个"双向静默 N 秒就切断"的
  TCP 中继（API 与 sandbox/gateway 各一个），复刻边缘的 60 s 空闲切口。
  先自检中继本身：`--idle-s 10 --only silent-90s` ⇒ 10.6 s 失败，
  错误与集群**逐字相同**（`unexpected EOF during chunk size line`），说明中继复刻的是同一条切口。

| 场景（`--idle-s 60`） | 去掉 keepalive（RED） | 修好后（GREEN） |
|---|---|---|
| 静默 90 s | **60.6 s 失败** | **90.7 s / 90.9 s 通过**（两次） |
| 单命令写 4000 文件（`--sleep-ms 22`，约 96 s，全程静默） | **61.6 s 失败** | **×3 连续通过**（见 §4 表） |

日志：`tmp/n37/relay-red-idle60.log`、`tmp/n37/relay-green-idle60b.log`、`tmp/n37/relay-smoke-idle10.log`。

### 3.2 集群（事故形状的 RED，本单已跑）

`tmp/n37/cluster_run.py --files 4000 --runs 1`：61.4 s 失败，
错误与 2026-09-26 的 `accept-snapshot-restart{2,3}.log` 完全一致
（`tmp/n37/cluster-4000-run1.log`；worker 侧 `kubectl logs e2b-worker-0` 里同一沙箱的
`is closed (slot released)` + `output loop failed` + `events pump ended after 53 events`）。

---

## 4. 修复（本仓侧）

| 文件 | 改动 |
|---|---|
| `envd_service/process/events.py` | 新增 `keepalive_event()` → `{"event": {"keepalive": {}}}`（协议里的空 `ProcessEvent.KeepAlive`） |
| `envd_service/rpc.py` | `_consume_stream(proc, queue, keepalive_s)`：`await asyncio.wait_for(queue.get(), timeout=keepalive_s)`，超时即推一次 KeepAlive（取消的 `Queue.get` 不吞事件）；`rpc_start` / `rpc_connect` 从 `Keepalive-Ping-Interval` 解析 interval 传入 |
| `gateway_common/keepalive.py` | 新增 `SDK_KEEPALIVE_PING_INTERVAL_S=50`（SDK 要的值）、`STREAM_KEEPALIVE_S=15`（缺省）、`STREAM_KEEPALIVE_MAX_S=30`（上限）、`EDGE_IDLE_CUT_S=60`（实测的边缘切口，作为给定值）与 `stream_keepalive_interval_s()`（缺省/不可解析/非正 → 15；大于 30 → 钳到 30，因为"发得更勤"永远安全、"发得更晚"正是这个 bug） |
| `envd_service/gateway.py` | `_FORWARD_HEADERS` 补 `keepalive-ping-interval`（原先被白名单丢掉） |
| `tests/unit/test_process_stream_keepalive.py` | 7 条：三个数的序关系、interval 解析/钳位、静默流"每 interval 恰好一次 ping 且不早于 interval"、忙流原样转发不插 ping、end 之后不再发 |
| `tests/contract/test_process_keepalive.py` | 4 条：走真实 app 的流式 RPC——静默 3 s 的 `Start` 必须带 ≥2 个形状精确的 keepalive 且都在 data 之前；忙命令只有 start/data/end；end 之后无任何消息；外加"用**已安装的 SDK** 读 `KEEPALIVE_PING_HEADER`/`KEEPALIVE_PING_INTERVAL_SEC` 并断言等于我们假设的值"（漂移护栏） |
| `tests/unit/test_gateway.py` | +1 条：真实请求穿网关，断言 `Keepalive-Ping-Interval` 到达 node（端到端，而不是去看那个集合） |

**先红后绿**（本机 `.venv` 与容器内一致）：

- RED（把 keepalive 去掉 + 网关不转发）：`tmp/n37/red-mutation.log` ⇒
  `test_a_silent_stream_pings_once_per_interval`、`test_a_silent_command_carries_keepalive_events`、
  `test_the_sdk_keepalive_interval_reaches_the_node` 三条红，其余 10 条绿。
- GREEN（恢复）：13 passed。
- 未碰 fork 引擎、未重建 `wheels/fork`（根因不在引擎里）。

---

## 5. 验收（判据：单命令写 4000 个文件，连续 ≥3 次）

本机端到端（relay `--idle-s 60`，两条命令都跨过 60 s 切口）：

| 次数 | 场景 | 结果 | 耗时 |
|---|---|---|---|
| 1 | 静默 90 s | OK（exit 0） | 90.9 s |
| 2 | 单命令 4000 文件（静默） | OK（`created 4000`，exit 0） | 96.4 s |
| 3 | 单命令 4000 文件（静默） | OK（`created 4000`，exit 0） | 96.5 s |
| 4 | 单命令 4000 文件（静默） | OK（`created 4000`，exit 0） | 94.8 s |

同一脚本在去掉 keepalive 时：静默 90 s 在 60.6 s 死、4000 文件在 61.6 s 死（RED）。
修好后 `RELAY PROBE DONE: failures=0`，且每个场景都记到一次中继切断
（那 60 s 只落在 create 之后空闲的池化连接上，命令流本身靠 15 s 一次的 ping 活着）。
集群上同一形状（4000 文件、静默 94 s）在未部署修复的 0.1.0-597 上仍在 61.4 s 死。

---

## 6. 残留与下一步

1. **上集群（唯一未做）**：本仓侧修复要生效必须重建镜像并发布，且**两个镜像都要重建**——
   `envd_service/rpc.py` 在 worker 镜像里，`envd_service/gateway.py` 在
   control-plane-gateway 镜像里：
   ```bash
   deploy/scripts/build-and-push.sh                # VERSION 形如 0.1.0-NNN-g<sha>-<ts>
   # 用新 VERSION 写 deploy/stack/.version，然后
   KUBECONFIG="$PWD/tmp/k0s/kubeconfig" deploy/k8s-k0s/apply.sh
   ```
   判定：`kubectl -n sandlock get deploy,sts` 三个工作负载同版本 + `kubectl diff` 为空；
   然后在集群上跑 `tmp/n37/cluster_keepalive_probe.py`
   （静默 90 s 必须 90 s 级通过、`tree-4000` 必须通过）与
   `tmp/n37/cluster_run.py --files 4000 --runs 3`。
   ⚠ 本单**没有**做这一步，而且这次**不该**由本单顺手做：
   - 线上是 `0.1.0-597-g3701a53`，而当前 `main` 已经领先它 **54 个提交**
     （`git log --oneline 3701a53..HEAD | wc -l`，含 N41 的 quota 单事务、N45 的
     `E2B_PID_NS`、N27/N39 的收口等）。从工作树重建镜像并 `apply.sh` 等于**发一版**
     ——那是版本发布决定，不是"修 N37"这件事能顺手做的。
   - 发布前还要注意：镜像从**工作树**构建，别的 agent 随时可能往里加未完成的改动；
     建议从本提交（`8253ad6`）的干净 worktree 构建，别直接从脏工作树 build。
2. **边缘那 60 s 是"给定值"，不是本仓的旋钮**：运维也可以把边缘的空闲超时抬上去，
   但那不解决这一类问题（任何更紧的代理都会重现），而且 SDK 已经明确要求了 ping ⇒
   协议侧实现才是正解。`EDGE_IDLE_CUT_S` 就是把这个给定值显式写下来，
   让 `STREAM_KEEPALIVE_MAX_S < EDGE_IDLE_CUT_S` 能被用例钉住。
3. **同类流**：`filesystem.WatchDir` 一直有 15 s ping（既有实现）；`process.Process/Connect`
   与 `Start` 现在共用同一条 relay，PTY 也走 `Start`，所以一起被覆盖。
   `StreamInput`（上行）仍是 `unimplemented`，与本次无关。
4. **F11 的影响**：那条验收要的是"比重启副本 ~130 s 启动更长的快照拷贝"，即 4000+ 文件。
   在本机（无边缘）它本来就不会被截断；在集群上要等 §6.1 的发布落地才解除。
   注意 F11 的拷贝命令同样**全程无输出**，所以它属于同一类形状。
5. **未复现的部分**：本机 `overlay` 存储没有 XFS prjquota（`/dev/loop-control` 不可用），
   所以本机实验里磁盘 tightening 走的是"无配额"路径；但静态读数与集群一致
   （静默 90 s 与文件系统无关，一个文件都不写也照样 60 s 死），不影响结论。

---

## 7. 原始日志与脚本清单

| 材料 | 路径 |
|---|---|
| 集群 RED（4000 文件） | `tmp/n37/cluster-4000-run1.log` |
| 集群对照（2000 文件 / 静默 90 s / 静默 50 s / 忙 95 s / 忙 4000 文件） | `tmp/n37/cluster-2000.log`、`tmp/n37/cluster-probe-all.log`、`tmp/n37/cluster-probe-silent.log` |
| 集群绕过边缘（port-forward）静默 90 s 通过 | `tmp/n37/cluster-probe-portforward.log`（`cluster-probe-pf-silent90.log` 是第一次尝试，port-forward 先掉了 ⇒ 502，只作废件保留） |
| 本机 relay 自检（10 s 切口，复刻同一条错误） | `tmp/n37/relay-smoke-idle10.log` |
| 本机 relay RED / GREEN（60 s 切口） | `tmp/n37/relay-red-idle60.log`、`tmp/n37/relay-green-idle60b.log` |
| 本机栈内 4000 文件（无切口）+ 拉长到 86.7 s | `tmp/n37/files4000-run1.log`、`tmp/n37/files4000-slow20-run1.log` |
| 用例先红 | `tmp/n37/red-mutation.log` |
| 复现脚本 | `tmp/n37/{cluster_run,cluster_keepalive_probe,relay_probe,repro_n37,relay_debug}.py`、`tmp/n37/{run-repro,run-relay}.sh` |
| 事故原始材料（2026-09-26） | `tmp/plan-2026-09-26/{tree-bisect.log,accept-snapshot-restart2.log,accept-snapshot-restart3.log}` |
