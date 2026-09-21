# 剩余任务计划（2026-09-20，除 G）

用户口径：**完成 A–F，除 G**。按此顺序执行，逐项验收；G（over-budget 端到端注入）
明确不做——为了测试在生产语义里塞钩子不划算，已有单测固定其语义。

| # | 任务 | 形态 | 状态 |
|---|---|---|---|
| F | rollout 后自动预热 base image（避免冷节点 `428 warm_required`） | 部署脚本 + 文档 | **已做**（2026-09-21，§22.5.13）：一份 helper 两处调用；热节点 `cached=true` 跳过、冷节点 18.8 s 转热，均集群实测 |
| E | 查清 supervise 那个预存在失败测试 | 诊断，必要时修 | **已做**（2026-09-21）：AF_UNIX `sun_path` 108 上限（长路径 153 字节 / `/src` 100 字节），fork 夹具改 `/tmp/sandlock-ctl-test-<pid>`，长路径转绿、`/src` 30+888 全绿 |
| A | 目录自身 `st_size` 计入账本（N31 修法②） | helper 输出 + 两处求和 + 契约测试 + 重建 | **已做，口径改为「实际分配」**（同日）：本机 NAS 上 `st_size`（4096→16384）不是空间、`du` 全程 512，故按 `st_blocks×512` 计费；集群平台数 = 沙箱测量 = `du` = 33792（逐字节相等，du diff 0） |
| B | N29：长任务（快照）不该是同步 HTTP | 幂等 + 网关超时 + 文档；异步化记为后续 | 待做（见文末） |
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
