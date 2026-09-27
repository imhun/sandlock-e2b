# 剩余任务计划（2026-09-20，除 G）

用户口径：**完成 A–F，除 G**。按此顺序执行，逐项验收；G（over-budget 端到端注入）
明确不做——为了测试在生产语义里塞钩子不划算，已有单测固定其语义。

| # | 任务 | 形态 | 状态 |
|---|---|---|---|
| F | rollout 后自动预热 base image（避免冷节点 `428 warm_required`） | 部署脚本 + 文档 | **已做**（2026-09-21，§22.5.13）：一份 helper 两处调用；热节点 `cached=true` 跳过、冷节点 18.8 s 转热，均集群实测 |
| E | 查清 supervise 那个预存在失败测试 | 诊断，必要时修 | **已做**（2026-09-21）：AF_UNIX `sun_path` 108 上限（长路径 153 字节 / `/src` 100 字节），fork 夹具改 `/tmp/sandlock-ctl-test-<pid>`，长路径转绿、`/src` 30+888 全绿 |
| A | 目录自身 `st_size` 计入账本（N31 修法②） | helper 输出 + 两处求和 + 契约测试 + 重建 | **已做，口径改为「实际分配」**（同日）：本机 NAS 上 `st_size`（4096→16384）不是空间、`du` 全程 512，故按 `st_blocks×512` 计费；集群平台数 = 沙箱测量 = `du` = 33792（逐字节相等，du diff 0） |
| B | N29：长任务（快照）不该是同步 HTTP | 幂等 + 网关超时 + 文档；异步化记为后续 | **已做（2026-09-22）**：幂等（2026-09-22 早）之后，异步形态落地为**加性**选项 —— `Prefer: respond-async`/`?async=1` ⇒ 202 + `status:"creating"`，`GET /snapshots/{id}` 轮询，重启可收尾；SDK 那条同步路径不变。入口超时仍属部署项（§22.5.14） |
| C | N26：共享卷单一信任域 | 记录已接受风险 + 具体缓解选项与触发条件 | **已做**（同日）：结论写进 backlog N26 行（接受；③④与卷切片各带触发条件） |
| D | N27：平台状态另起 BASE | 评估并记录（低优先级，触发条件） | **已做**（同日）：结论写进 backlog N27 行（不做；触发条件=切 pure 形态或平台状态暴露给非本租户） |

## 验收标准

## 已定位的落点（省下一次搜索）

- **A**：目录块数字来自 **`deploy/priv/maint.c`** 的 `walk` 输出（C 程序，随 worker 镜像构建），
  Python 侧两处求和是 `envd_service/runtime/dir_ledger.py::scan_subtree` 与
  `envd_service/priv_helpers.py::dir_size`（后者只取 `kind == "f"`）。契约测试在
  `tests/unit/test_dir_ledger.py`（逐字节等于 `dir_size`）。所以 A = 改 C + 两处求和 + 测试期望 + 重建镜像。
- **F**：`deploy/k8s-k0s/apply.sh` 与 `deploy/scripts/upgrade.sh` 目前都没有预热步骤；
  端点是 **POST** `/agent/images/<urlencoded-ref>/warm`（GET 只查询），带 `X-Internal-Key`，
  打在每个 worker 的 `127.0.0.1:49983` 上（可用 `kubectl exec ... -- python3 -c ...` 触发）。
- **E**：`crates/sandlock-supervise/tests/supervise.rs::test_supervise_path_serve_launches_instance_and_serves_verbs_until_shutdown`，
  失败信息是 `timed out waiting for registered socket to appear`；未改动的树上同样失败，
  需要在容器里单独跑该用例并看 supervise 侧 stderr。
- **B**：`control_plane/api/sandboxes.py` 的快照路由是同步实现（2000 文件 > 网关 60 s 超时）；
  最小修法是幂等 + 网关超时对齐，异步化是后续。

- F：`apply.sh` 之后，两个节点的 base image 都是热的（`peek` 返回 `cached: true`），
  不需要手动 POST；文档里写清 GET/POST 的区别。
- E：给出失败原因（环境 or 真 bug），能修则修，不能修则写进 backlog 并说明触发条件。
- A：`dir_ledger` 与 `priv_helpers.dir_size` 的"逐字节相等"契约仍然成立，且两者都把目录自身的
  `st_size` 计入；集群上平台数字与沙箱内 `du` 更接近（目录多的树差距变小）。
- B：重试一个已经成功但客户端超时的快照请求，得到"已经存在/已完成"而不是 409；
  超时前不再让客户端拿到 504 而服务端继续跑。
- C/D：写成可执行的结论（做/不做/触发条件），进 backlog。

## B 的剩余工作（2026-09-21 量清事实，未改代码）

原计划写的是「幂等 + 网关超时 + 文档」，本轮把**客户端实际看到的形状**量了一遍
（`deploy/scripts/acceptance/probe_n29_sync.py`，日志 `tmp/k0s/n29-sync.log`；2000 个文件 / 512 B 的树，
入口 `http://172.18.78.49:3000`）：

| 观察 | 事实 |
|---|---|
| 一次快照 POST 的耗时 | 超过入口的 60 s ⇒ 客户端拿不到回答；控制面自己的下游调用超时是 **120 s**（`control_plane/api/snapshots.py` 的 `httpx.post(..., timeout=120)`），所以"控制面还在跑"这件事有 60 s 的窗口是**设计如此**，不是竞态 |
| 服务端 | **拷贝跑完并落了记录**：事后 `GET /snapshots` 里确实有 `snap_a6663c2fac9862b0`（`names: ['n29-probe']`），而客户端那一次是超时 |
| 紧接着重试 | **到不了控制面**：一次 504 之后入口把 upstream 摘掉，随后 `GET /sandboxes` 连续 502 约 30 s（本轮实测：15 s 时仍 502，30 s 恢复 200），`Sandbox.create`/`kill` 同样 502。也就是说 backlog 里写的"重试得 409 already exists"**不是**这条路径上的第一个现象——先撞上的是入口自身的 502 |
| 重试的后果（读代码的结论） | 控制面每次请求都 `new_sandbox_id()` 生成**新** `snap_…`，所以客户端重试（入口恢复后）会**再拷一份**，而不是拿回既有快照；worker 侧只有"同一个 `snapshotID` 且目标已存在"才回 409（`envd_service/agent.py` 的 `if dst.exists()`），而那条路径今天只能由控制面自己重发同 id 触发——它并不重发 |

**因此 B 的修法顺序（下次直接照着做）**：

1. **先定"客户端可重试"的标识**：控制面的快照 id 现在每次随机，重试无法被识别。可选
   (a) 让请求体里的 `name` 参与幂等（同沙箱 + 同名 ⇒ 同一个 `snapshot_id`，worker 的
   `dst.exists()` 那就变成"已完成，返回既有记录"），或 (b) 支持 `Idempotency-Key` 头并由控制面
   落一张短 TTL 的映射。e2b SDK 不发额外头，所以 (a) 才是"SDK 用户重试也有效"的那条。
2. **worker 侧把 409 改成幂等回答**：目标目录存在且 `fs/` 已完整（拷完才 rename 或写 marker）⇒
   200/201 + "already exists, completed"，而不是 409；半份 payload 仍要能重拷（现有单测
   `test_snapshot_copy_does_not_leave_a_half_payload` 是这条的地基）。
3. **入口超时与摘除参数**（`docs/k8s-deployment.md` §14 的入口那节）：把 `proxy_read_timeout`
   提到覆盖一次合法拷贝（或按 ① 的异步语义彻底绕开），并复核
   `max_fails`/`fail_timeout`/`proxy_next_upstream`（`non_idempotent` 尤其要看——它决定一次
   超时后重试打不打到**第二个上游**，这正是重复拷一份的另一个来源）。改完要用
   `deploy/scripts/acceptance/probe_n29_sync.py` 复跑：期望 1st POST 不再 504，或重试拿回**同一个** snapshot。
4. **文档**：把"客户端超时但服务端成功"的语义写进 API 文档（快照是幂等的、以 `name` 或
   `Idempotency-Key` 为准；超时后先 `GET /snapshots` 再决定是否重试）。

验收判据（原计划）：重试得到"已存在/已完成"而不是 409；超时前不再让客户端拿 504 而服务端继续跑。


## 收口记录（2026-09-22，`0.1.0-429-g2abaf33-20260922-094142`）
`deploy/scripts/build-and-push.sh` 重建并推送，`deploy/k8s-k0s/apply.sh` 滚动两个 worker +
控制面，全部换成该版本（pod 的 `.status.containerStatuses[0].image` 逐条核对）。这一次的镜像
**带上了「修复那 10 个失败」那一轮的代码改动**，其中唯一影响运行时的是一条真 bug 修复：
`PrivHelpers.slot_spawner()` 现在接受 N25 的 `events_fd`（非 root 生产形态原来会在起 route-B
槽位时 `TypeError`）。

| 项 | 证据 |
|---|---|
| 部署 | `apply.sh` EXIT=0；两个 worker 都滚动到新版本；预热步骤照跑（两节点 `peek cached=true`，`warmed=skipped`） |
| A（目录计费） | `deploy/scripts/acceptance/probe_dir_stsize.py`：平台数 = 沙箱内独立测量 = `du -s -B1` = **33792**，逐字节相等、du diff 0 |
| F（预热） | `apply.sh` 内置步骤输出两节点 `cached=true`；冷节点路径在 2026-09-21 已单独验证过（`cached=false` → POST → `cached=true`，18.8 s） |
| 端到端 | `deployment_smoke.py` **DEPLOYMENT SMOKE OK**（命令/文件、迁移保留文件、网络配置、远端卷隔离、模板构建→拉取→rootfs、箱内 MCP 经代理、kill 后预留归零）；`multinode_smoke.py` **MULTI-NODE SMOKE OK**（4 箱 2+2 跨节点） |
| 非 root 形态 | 由 lane 的 phase 2 覆盖（`UNPRIVILEGED_PHASE` 默认跑）：51 passed；集群这份清单是 **root worker**（`worker-root.patch.yaml` 的 `runAsUser: 0`），走的是 `route_b._spawn_slot`，不受那条修复影响。<br>⚠ **这一格是 2026-09-21 当天的形态，已被 C1 wave 2（2026-09-27）取代**：`worker-root.patch.yaml` 已删除，worker 不再有任何 root 容器、`chown`/`rm`/`walk` 交给基线 `e2b-priv-broker` DaemonSet（见 `docs/k8s-deployment.md` §24） |

即：A/F 已在集群复验，B 仍未做（见上），C/D 是书面结论。

## B 收口（2026-09-22，`0.1.0-431-gf821435-20260922-101447`）

**做了什么**（对应 backlog 的 ②③④，① 异步仍留后续）：

* **幂等键**：`POST /sandboxes/{id}/snapshots` 认 `Idempotency-Key` 头或 body 里的 `snapshotID`。
  同键 = 同一份快照：记录在 ⇒ **200 + `{"status":"completed","alreadyExists":true}`**（不再拷）；
  拷贝还在跑 ⇒ 这次请求**等它跑完**再回同一份记录（控制面按 id 加锁，单副本即可，多副本要换共享锁）。
* **worker 侧 `.complete` 标记**：`POST /agent/snapshots` 对已完成载荷回 **200 `alreadyExists`**（不再是 409），
  只有"目录在、标记不在"（崩在半路）才是 409 —— 并发保护不变、重试不再付第二次整树拷贝；控制面把
  worker 的 409 如实翻成 409（不再伪装成 502）。
* **本地形态**同理：载荷已在盘上就 `copy_fs=False` 只写记录。
* **文档**：明确"客户端超时 ≠ 失败"；没带键的重试（e2b SDK 只发 `name`）会**再建一个新快照**——
  要可重试语义就得带键，或先 `GET /snapshots`。入口侧要配的值与理由见 §22.5.14（`proxy_read_timeout`
  ≥ 合法拷贝时间；复核 `non_idempotent`/`max_fails`/`fail_timeout`）。

**验收（集群，直连控制面 `kubectl port-forward`，2000 文件的树）**：

| 现象 | 数字 |
|---|---|
| 第一次 POST（带键，客户端超时 600 s） | **201**，**76 s**，id == 键 |
| **同键重试** | **200 + `alreadyExists:true`**，**0.12 s**（同一 id，列表只有一条） |
| 同键并发两条（"重试赶在拷贝进行中"这一形态） | **200 + 201，同一个 id**，列表只有一条 —— 第二次等了第一次的拷贝，没有再拷 |
| 换一个键 | **201**，另一份快照（键才是判别式，不是"每沙箱一份"） |
| 清理 | `DELETE /templates/{id}` → 204 ✓ |

**为什么走直连**：经入口时每次 2000 文件快照都在 **60.1 s 拿到 504**，随后入口**摘 upstream 约 30 s**
（`/sandboxes` 连续 502，实测两次），重试根本到不了控制面 —— 那半是**部署项**（入口超时与摘除参数），
本仓库改不动，已按 §22.5.14 给出要配的值。

**顺带抓到一个新问题（与本改动无关，已记 N32）**：这轮反复做大快照期间，worker 心跳出现
**76–83 s 断档**（`deploy/scripts/heartbeat_gaps.py --since 40m`，四个窗口分别 82.72 / 82.15 / 78.43 /
77.07 s），而 k8s overlay 的节点健康窗口是 **30 s** ⇒ 控制面把节点判为 unhealthy、`node health sweep`
把沙箱标成 **orphaned**，于是**紧接着的下一次快照回 409「Sandbox … is not running」**
（直连探针 2026-09-22 实测，`tmp/k0s/n29-idem-direct2.log`；当时共享 base 上只有 8 棵树、2.6 GB，
所以不是"树太多把轮次拖长"）。断档的量级与拷贝耗时几乎相同，但是拷贝的 NAS 负载、reconcile 的磁盘
轮次还是别的阻塞路径，需要一次定向测量才能定论。
