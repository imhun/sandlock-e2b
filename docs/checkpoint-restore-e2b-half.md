# E2B 侧的 checkpoint/restore：设计

守卫用例（`tests/unit/test_checkpoint_restore_unused.py`）挡着的就是这一份：
**引擎能用了，但 E2B 这一半没设计。** 本文把它设计出来，并把方案里
"看起来能做、其实做不了"的三处先钉住（§1）。

---

## 0. 这个能力在产品上是什么

今天 `pause` 的实现是**冻结进程组**（`ProcessManager.pause_all` → SIGSTOP，
worker 侧 `_agent_set_paused`）。所以：

* 进程活在 **worker 的内存里**，只在它的 worker 还活着时存在；
* worker 滚动重启 / 节点掉线 ⇒ 沙箱的进程没了（文件还在，`_runtime` 与树都在共享 NFS 上）；
* `/sandboxes` 里的沙箱能"跨 worker 迁移"（`deployment_smoke` 验的就是这条），
  但那迁移的是**文件**，不是**正在跑的进程**。

checkpoint/restore 补的正是这一段：**把一个正在跑的沙箱写进磁盘、之后再恢复**——
包括它的 worker 已经不在了的时候。因为 workspace base 是共享 NFS，
"之后"可以是同一个 worker、也可以是另一个节点。

**所以这个能力的产品形状是：`pause` 变得能在 worker 重启后存活。**
（不是新造一个用户可见的动词；E2B SDK 的 `pause()`/`connect()` 已经有位置放它。）

**但"存活"有个上限，是引擎给的（§1(d)）**：恢复出来的沙箱里**原有那个进程**回来了，
而**新的 exec 不被服务**（OCI 的恢复路径按名拒绝，因为 exec 靠 `sandlock-init`，
恢复出的沙箱没有 init）。所以对"长驻服务要继续服务"够用，对"继续在这个箱子里干活"不够 ——
这是 §2 的 D9，需要先拍。

> ⚠ 需求本身仍未确认：仓库里没有任何"用户要这个"的记录，`envd` 至今没碰过这套 API。
> 本设计按"`pause` 存活"这个最有说服力的形状写；如果最后没人要，停在这里的代价也只是这份文档。

---

## 1. 三个把方案钉死的事实

### (a) `Sandbox` 活在 slot 进程里 ⇒ **必须先加一个 slot verb**

生产是 route B：真正的 `Sandbox` 句柄在 `sandlock-supervise` 那个独立进程里，
worker 的 python 只是通过 socket 上的 verb 跟它说话
（`envd_service/route_b.py`，verb 有 `run`/`exec`/`wait_child`/`kill_child`/
`update_network`/`shutdown`，分发在 fork 的 `crates/sandlock-supervise/src/serve.rs:922`）。

所以"E2B 自己那一半"这个说法**不完整**：`checkpoint()` 是 `Sandbox` 上的方法，
worker 调不到它，得先有一条 verb。好消息是加 verb 是安全的——未知 verb 会被干净拒绝，
worker 早就把"这个 slot 不认识这个 verb"（旧二进制）当成一种正常情况处理
（`route_b.py` 里那两处 "an older binary" 注释）。

**这不是纯 E2B 改动，和 `update_network` 当年一样是 fork+E2B 一起动。**

### (b) blob 的天然位置**不在**磁盘账本里，而"计进账"这句话得说清记到谁头上

进程内存的落点不能是 workspace——那是 guest 可读的。天然的、也是唯一干净的位置是
`gateway_common/paths.py` 的 `sandbox_runtime_dir()`：`<base>/_runtime/<id>`，
注释写着 *"the sandbox has no access at all"*。

**但它不在账本里。** 磁盘账本量的是 `record.workspace_dir`（`<base>/<id>`），
`_runtime/<id>` 是它的**兄弟目录**（`registry.py::disk_usage_snapshot` 逐 `workspace_dir` 走）。
今天那里有多大，是量过的（2026-09-25，两台 worker）：**每个沙箱 4096 字节**——正好一个块，
内容只有 `command-logs.jsonl`（样例 59 字节）。对照 1 GiB 的 `diskMB`，那是 **0.0004%**。
所以"把现有的 `_runtime` 纳入账本"这件事本身几乎不花钱（见 §2 D3 的实测）。

**真正不一样的只有 blob**：它是**整个进程的内存**，量级差三四个数量级。
难点不是"要不要算"，而是**记到谁的账上** —— 因为按用户的 `diskMB` 去算会撞上一个很坏的形状：

`pause` 是**释放**资源的动作，而不是消耗资源的动作。如果 pause 写的 checkpoint 记在被暂停的那个
沙箱自己的配额里，那么"暂停"会把它推过预算，于是它**从此不能写**（worker 侧实测口径：
超预算 ⇒ `RLIMIT_FSIZE=0` 让每个写失败在 `EFBIG`，`O_CREAT`/`mkdir`/`symlink`/`link` 回 `ENOSPC`，
见 `control_plane/registry/manager.py::enforce_disk_budget` 的说明），而它的文件**一个字节都没变**。
用户想腾空间只能删自己的文件，而 blob 不会因此变小。

（一条我先前的猜测在这里被代码否掉了，记下来免得别人重走：磁盘超限**不会**触发 pause ——
`enforce_disk_budget` 的注释写着 *"Over budget is not a pause"*，这是刻意的产品语义
（冻结会连"删东西把自己弄回预算内"一起拿走）。所以不存在"pause → 更大 → 更多 pause"的正反馈；
上面那个陷阱与它无关，它只来自"记到谁头上"。）

### (c) `restore_skipped` 是一条**语义选择**，不是缺陷

socket / pipe / memfd 恢复不了（引擎固有边界，与架构无关）。恢复出来的进程
**不会有它原来的连接**。这必须被明确表达给调用方，而不是让沙箱静默地"看起来恢复正常、
然后第一次 read/write 才炸"。引擎已经把它列出来了（`restore_skipped` 的 fd 表，
`test_restore.rs` 断言"只有 stdio"），所以 E2B 侧要做的是**把它作为恢复结果的一部分**
返回/记录，并在文档里说清"恢复的沙箱没有原有的网络连接"。

### (d) 恢复出来的沙箱**不能 exec** —— 这是引擎的语义，不是缺口

S1 的 `checkpoint` verb 落地后去查 restore 那一半，撞到引擎自己写死的一句话。
OCI 的恢复路径（`crates/sandlock-oci/src/supervisor.rs` 的 `serve_one_running`）
对 `Exec` 的回答是：

    exec is not supported on a restored container

旁边的理由是：**exec 靠 `sandlock-init` 转发**，而恢复出来的沙箱里**没有 init** ——
`restore_interactive` 起的是一个"被还原的进程"，不是 `sandlock-init`
（对照 create 路径：它 `spawn` 出 init，再由 init 服务 exec）。

**这条改变的是产品含义，不是实现细节**：

| | 今天（SIGSTOP 冻结） | 恢复之后 |
|---|---|---|
| 原来那个进程 | 活着 | **活着**（内存状态回来了） |
| 能不能 exec 新命令 | 能 | **不能**（引擎按名拒绝） |

所以"pause 活过 worker 重启"换来的不是一个**完好如初**的沙箱，而是一个
**进程还在、但不能再往里敲命令**的沙箱。对"跑着长驻服务、要它继续服务"的形状这够了
（服务照旧）；对"我要继续在这个箱子里干活"的形状，**不够**。

于是 E2B 侧必须先回答一个产品问题（§2 D9），而不是先写代码。

---

## 2. 决策点与建议

| # | 决策 | 建议 | 理由 |
|---|---|---|---|
| D1 | blob 放哪 | `<base>/_runtime/<id>/checkpoint/<gen>.blob` | 沙箱完全无权限；与 record/命令日志同处一个平台目录；跨节点天然可见（共享 NFS） |
| D2 | 谁拥有 | worker uid（0700 目录），**不是**沙箱的池 uid | blob 是进程内存，可能含凭据；沙箱自己永远不该读到它 |
| D3 | 配额怎么算 | **记到平台账上，不记进用户的 `diskMB`**：给 `_runtime` 加一条独立的平台账（节点级 + 舰队级），checkpoint 写入前先看它够不够；不够就**拒绝这次 checkpoint 并退回今天的 SIGSTOP**，而不是悄悄吃掉用户的空间 | §1(b)。两边的理由都硬：不计 = checkpoint 变成绕过配额的口子；按用户配额计 = "暂停"把沙箱推过预算，它从此只能读不能写（`EFBIG`/`ENOSPC`），而用户自己的文件一个字节没变 |
| D4 | 何时 checkpoint | **`pause` 时**，且可配置（`E2B_PAUSE_CHECKPOINT=1`，默认关） | 复用已有的、用户可见的生命周期动词；默认关 = 不改变今天的行为 |
| D5 | 何时 restore | `resume` 时，**若进程已不在**（worker 重启过）才走恢复；还在就直接解冻 | 恢复是慢路径、且丢连接，不该在正常路径上付这个代价 |
| D6 | `restore_skipped` 对外 | 恢复结果里带 fd 表；日志 + 文档明说"连接不回来"；**不**假装成功 | §1(c) |
| D7 | 跨节点 | 允许（blob 在共享 NFS 上），但**同内核**是硬前提 | 引擎前提，与架构无关 |
| D8 | 清理 | 随沙箱 teardown 一起删（`_delete_sandbox_runtime` 已经按 verified target set 删 `_runtime`） | 不新增一条回收路径 |
| D9 | **恢复后 exec 不可用，产品上怎么算** | **二选一，需要拍板**：**(a)** 接受"恢复 = 进程回来、不能再 exec"，把它写进对外语义（长驻服务形状够用）；**(b)** 让恢复出来的会话 exec-capable（引擎侧要新做"把 checkpoint 还原进一个带 init 的会话"），代价明显更大 | §1(d)：引擎自己按名拒绝 `exec is not supported on a restored container`。这不是我们能顺手补的缺口，是"恢复一个进程"与"恢复一个可交互的箱子"的区别 |

---

## 3. 阶段（每阶段独立验收）

| 阶段 | 做什么 | 验收 |
|---|---|---|
| **S0** | ✅ 修掉守卫用例里过时的架构说法（它仍写着"引擎只支持 x86_64/riscv64、aarch64 要先移植"，而 aarch64 的 S0–S5 2026-09-24 已落地） | 用例文本与代码一致 |
| **S1a** | ✅ fork：slot 加 `checkpoint` verb（写 blob 到调用方指定的路径）—— fork `e76cb2f`，主仓 pin `82a26df` | fork 的 supervise 相位 **31 passed / 0 failed**，新用例钉住"镜像是引擎格式"与"捕获不是 kill" |
| **S1b** | fork：`restore` —— 但它不是一条 verb，而是**从镜像起一个 slot**（`Checkpoint::load` → 用镜像里的 policy 起沙箱 → `restore_interactive`），服务 `stats`/`shutdown`、**按名拒绝 exec**（照 OCI 的既有语义） | fork 相位新用例：从镜像起的 slot `stats` 说进程活着、`exec` 得到那句按名拒绝、`shutdown` 干净退出。**先答 D9 再动手**：若选 (b)，S1b 的形状完全不同 |
| **S2** | worker：agent 端点 + D1/D2/D8 的落地 + **D3 的平台账与拒绝路径** | 单测：blob 落在 `_runtime`、沙箱读不到、**平台账计入且用户的 `diskMB` 不变**、平台账不够时拒绝而不是写入、teardown 删净 |
| **S3** | 生命周期：`pause` 写、`resume`（进程不在时）恢复，旗标默认关 | 集群验收：**启一个跑着的沙箱 → 重启 worker → resume → 进程状态还在**（正是今天做不到的那一条）。**验收要连 §1(d) 一起写**：恢复后 `exec` 的行为按 D9 的结论定（(a) 则断言那句按名拒绝，(b) 则断言能继续 exec） |
| **S4** | `restore_skipped` 的对外语义（D6） | 契约用例 + 文档 |

**S1 之前的任何 E2B 侧改动都没有意义**：没有 verb，worker 拿不到 `Sandbox`。

---

## 4. 明确不做

* **不做跨内核恢复**：同内核是引擎前提（捕获的是这台内核的地址空间布局）。跨内核要重做引擎，不在这个能力的范围里。
* **不做"自动迁移正在跑的沙箱"**：blob 在共享 NFS 上让这条路技术上可行，但那是调度器的活，
  且要先把 §2 全部落地。留给以后按需评估。
* **不改 `pause` 今天的行为**（旗标默认关）：S3 之前，pause 仍只是 SIGSTOP。

---

## 5. 出处

* 引擎与两种根形态：`docs/chroot-workspace-exec.md` §9.7.9、§11（A 方案 `a6f6b04`）
* 当前守卫：`tests/unit/test_checkpoint_restore_unused.py`
* slot 协议：`envd_service/route_b.py`、fork `crates/sandlock-supervise/src/serve.rs`
* 平台状态目录：`gateway_common/paths.py`（`sandbox_runtime_dir`）
* 账本口径：`envd_service/runtime/registry.py::disk_usage_snapshot`、`envd_service/runtime/dir_ledger.py`
