# 沙箱磁盘配额最终方案（XFS project quota）

## 1. 决策记录

| 候选路线 | 结论 | 原因 |
|---|---|---|
| 沙箱级 COW（SandboxSession / 常驻 supervisor） | ❌ 否决 | sandlock 契约大改 + 单点风险 + 进程隔离重设计；事务性非沙箱刚需 |
| 每命令 COW + merge | ❌ 否决 | merge 开销 + 丢失更新 + background 并发冲突；数据层冲突 COW 也解决不了 |
| 无 COW + du 轮询检查 | ❌ 否决 | du 慢（大目录秒级）+ 滞后（kill 窗口）；不是记账是统计存量 |
| LD_PRELOAD 写记账 | ❌ 否决 | 字节级快但可被静态链接/Go 程序绕过 |
| 每沙箱独立文件系统（loop） | ❌ 否决 | 挂载点不跨 worker 共享，破坏零拷贝迁移 |
| **XFS project quota** | ✅ **采用** | 内核硬限、目录级、不改 uid、不破坏共享存储/迁移、零滞后；目标机已是 XFS |

最终路线：**XFS project quota（生产硬限）+ 非 XFS 环境降级跳过 + E2B 命令串行锁**。

## 2. 架构

```
生产目标机（XFS + prjquota）
└─ /var/lib/e2b-sandboxes           # 共享存储（XFS，project quota 启用）
   ├─ <sandbox_id>/                 # 每个沙箱目录 = 一个 project id
   │   └─ workspace/
   └─ _cow/  _snapshots/  ...       # 其他目录（默认 project 0，无限额）
```

- 每个沙箱目录分配独立 project id（projid），限额 = `RuntimeSandbox.disk_mb`
  （控制面已下发的沙箱配额）；
- 目录设置 `PROJINHERIT`，沙箱内新建文件自动继承 project id；
- 写超限 → 内核返回 `ENOSPC`（XFS project quota 语义：`xfs_trans_dqresv`
  对 project quota 硬限返回 `-ENOSPC`，`EDQUOT` 仅用于 user/group quota；
  硬限强制不变：零滞后、内核强制、跨命令累计）；
- 沙箱删除 → 删除目录 + 清理 project 记录；
- 非 XFS 环境（macOS 本地 / Docker Desktop / OrbStack 默认 / ext4 容器）：
  `_xfs_project_supported` 检测失败 → 跳过 quota + 警告，沙箱照常运行。

## 3. E2B 侧改动清单

### 3.1 检测与降级

```python
def _xfs_project_supported(workspace_base: Path) -> tuple[bool, str]:
    """检测 workspace_base 是否支持 XFS project quota。"""
    # 1) findmnt/mountinfo：文件系统 == xfs
    # 2) xfs_info：projid32bit == 1
    # 3) 挂载选项含 prjquota（或可在线启用）
    # 4) xfs_quota 命令存在
```

任一不满足 → 返回 `(False, reason)`，agent 创建沙箱时跳过 quota 并记警告日志。

### 3.2 沙箱创建（`agent_create_sandbox`）

```bash
xfs_quota -x -c "project -s -p /var/lib/e2b-sandboxes/<id> <projid>" <mount>
xfs_quota -x -c "limit -p bhard=<disk_mb>M <projid>" <mount>
```

**projid 分配（已实现，E2.2）**：`sandbox_id` 的 SHA-256 哈希映射到
`1..2^31`（含 2^31，实测 `xfs_quota` 接受），分配前用
`xfs_quota -x -c "report -p"` 读取当前 project 表做冲突探测，命中已占用
projid 时线性递增（到 2^31 回绕到 1）。选择哈希而非 worker 计数器的原因：
映射确定、worker 重启/多 worker 共享存储时无需跨节点协调；探测避免复用
仍活跃的 projid（共享 projid 会共享限额）。

创建时若 `project -s` 成功但 `limit` 失败（半创建状态），先尽力
`project -C` 清理再抛出，调用方记警告降级、沙箱照常创建；projid 与沙箱
id 的映射持久化到 `sandbox.json`（`RuntimeSandbox.project_id: int | None`）。
已存在 `project_id` 的沙箱（迁移回滚重 prov 等）复用原 projid，不重新分配。

### 3.3 沙箱删除（`agent_delete_sandbox`）

```bash
xfs_quota -x -c "project -C -p /var/lib/e2b-sandboxes/<id> <projid>" <mount>
rm -rf <sandbox_dir>          # 删除目录后 quota 计数自动释放
```

`project -C` 清除目录的 project 状态并递归归还计账；`rm -rf` 后使用量归零。
quota 表项（0 使用量 + 原硬限）仍保留为孤儿记录，由 M4 定期清理；实测
`project -d` 只操作 `/etc/projects`（本方案不维护），不能删除 quota 表项。
`keepFiles=true`（迁移停源节点）不执行 `project -C`：文件保留、project
状态保留，迁移失败回滚时配额仍生效。

孤儿 project 记录：worker 启动时扫描 `sandbox.json` 与 quota 表不一致的
project id，定期清理。

### 3.4 快照与迁移

- **同一文件系统内**：目录 inode 不动（迁移零拷贝）或 `copytree` 到带
  `PROJINHERIT` 的目标目录 → project id 自动继承，无需额外处理；
- 跨文件系统（NFS 等）：project id 不迁移，目标端需重建 project（降级路径）。

### 3.5 命令串行锁（独立正确性修复）

`ProcessManager` 增加 per-sandbox `asyncio.Lock`：同一沙箱的命令写访问互斥，
并发命令排队（或返回 429）。这是与 quota 无关的独立修复（当前无任何并发
防护，[manager.py](/Users/polus/project/ai/sandlock-e2b/envd_service/process/manager.py:73)）。

### 3.6 NFS 形态：quota-agent（E2.6）

NFS 部署下 worker 只能看到 NFS 客户端挂载，真正的 XFS 文件系统在 NFS
服务器上。worker 不直接跑 `xfs_quota`，而是把配额操作转发给部署在服务器
侧的 quota-agent（小 HTTP 服务，`deploy/quota_agent`），由 agent 在服务器
本地执行 `xfs_quota`：

- 请求面：`GET /detect?mount=`（服务端检测 facts）/ `POST /project_create`
  （`{projid, path, limit_mb, mount}`）/ `POST /project_delete`
  （`{projid, path, mount}`）/ `GET /report?mount=`（项目配额表）/
  `POST /reconcile`（`{workspace_base, mount}`）；
- 鉴权：`X-Internal-Key` 携带 `E2B_QUOTA_AGENT_TOKEN`（与 worker 的
  internal key 风格一致），token 未配置时服务拒绝启动/应答；
- worker 侧：`E2B_QUOTA_VIA_AGENT=true` + `E2B_QUOTA_AGENT_URL/TOKEN` 时
  `envd_service.app.create_app` 自动 wire `xfs_quota.agent_query/agent_ops`
  HTTP 客户端；默认不设置即本地直连（零回归）。agent 不可达/401/协议错误
  一律抛 `ProjectQuotaError`，由既有调用方降级跳过 + 警告，不阻塞沙箱；
- 部署：`deploy/docker/Dockerfile.quota-agent` +
  `deploy/compose/docker-compose.quota-agent.yml`（NFS 服务器形态说明、
  `E2B_QUOTA_AGENT_PATH_MAP` 客户端→服务器路径映射）。

## 4. 本地验证（OrbStack）

OrbStack 容器内可挂载 XFS + prjquota（实测通过）：

```bash
# worker 测试容器（--privileged）
dd if=/dev/zero of=/var/lib/xfs.img bs=1M count=20480
mkfs.xfs -f /var/lib/xfs.img
losetup /dev/loop0 /var/lib/xfs.img          # 注意：mount -o loop 自动路径在该环境失败
mkdir -p /var/lib/e2b-sandboxes
mount -t xfs -o prjquota /dev/loop0 /var/lib/e2b-sandboxes
```

之后 `_xfs_project_supported` 检测通过，project quota 真实生效。
注意：alpine 的 `xfsprogs` 包缺 `xfs_quota`，本地验证用 rocky/alma 容器镜像。

## 5. 测试矩阵

| 环境 | quota 行为 | 验证内容 |
|---|---|---|
| 生产目标机（XFS + prjquota） | 生效 | 配额超限 ENOSPC（project quota 语义）、跨命令累计、迁移保留 |
| OrbStack 容器（XFS loop） | 生效 | 本地完整验证 |
| macOS 本地（LocalExecutor / 无 XFS） | 降级跳过 | 沙箱照常、日志警告 |
| CI（mock） | mock xfs_quota | 创建/删除/清理逻辑单测 |

## 6. 实施里程碑

| 里程碑 | 内容 | 验证 |
|---|---|---|
| M1 | `_xfs_project_supported` + 降级路径 + 单测 | mock 单测全绿 |
| M2 | 沙箱创建/删除的 project 管理 | 目标机/OrbStack 实测 |
| M3 | 命令串行锁 | 并发命令排队/429 测试 |
| M4 | 孤儿 project 清理 + 监控告警 | worker 启动扫描 |
| M5 | 生产部署（见部署要求文档） | 远程复测 |

## 7. 相关文档

- 生产部署要求：[production-deployment-requirements.md](production-deployment-requirements.md)
- COW 评估记录（已否决）：[sandbox-level-cow.md](sandbox-level-cow.md)
