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

### 1.1 口径：存量边界 vs 增量边界（为什么不用 COW 的 `max_disk`）

被否决的候选里，sandlock COW 是唯一**自带配额参数**的（`CowState.max_disk_bytes`，
超限在 seccomp 通知里返回 `ENOSPC`），所以值得把"为什么不选它"写成口径差异，而不是一句"已否决"：

| | COW `max_disk` | XFS project quota（采用） |
|---|---|---|
| 记账边界 | **时间**：分支创建**之后**写入 upper 的字节 | **位置**：`<base>/<id>` 这棵树里**当前**的所有字节 |
| 口径 | 增量（`disk_used` 随删除递减、并有 `recalc_disk_used()` 按目录重算） | 存量（就是这棵树现在的占用） |
| 沙箱创建时已有的内容 | 不计（那是 lower） | 计 |
| 分支重建 / 快照 merge 后 | **归零**（COW 文档 §8.4 自己列的开放问题） | 不变（真实占用） |
| 强制点 | 用户态 mediator 拦到的写路径 | 内核分配路径（无旁路概念） |
| 对用户可解释为 | "最多还能再写 X" | "最多占 X"（可对着 `du` 解释） |

两者的差别**不是"包不包括镜像 rootfs"**——镜像 rootfs 两种口径下都不计：

- XFS project quota 打在**沙箱树**上（`<base>/<id>`），而镜像 rootfs 是共享缓存里的 chroot 目标
  （`resolve_image_rootfs()` → `image_cache_dir`；线上实测 workspace base 顶层根本没有 `_images`），
  所以它天然不在配额内；
- COW 的 lower 是 `workdir` 在分支创建时的内容，镜像 rootfs 同样不在其中。

> ⚠️ **不要**把这两件事混起来解释（本文档早期一版对比表就把它写成了"img 全算"，是错的）。
> 真正要防"沙箱写爆共享盘"时选**存量口径**（现在的实现）；只有做"事务预算/回滚"时才需要**增量口径**。

#### 1.1.1 探针实测（2026-09-13，`.superpowers/sdd/task-cowprobe-report.md`）：**今天根本用不了**

route-B 给了"每沙箱一个常驻 supervise 实例"之后，"`max_disk` 是不是终于可用了"值得实测一次。结论是**不能用**，
而且不是口径问题，是**能不能激活 + 强制点在哪**：

1. **在 E2B 形态下压根不激活。** `max_disk` 确实落到了 builder，serve 路径也确实走建分支的代码，
   但门槛是 `!no_supervisor && self.workdir.is_some()`（fork `sandbox.rs:2024`），而 COW 开关本身是
   `cow: sandbox.workdir.is_some()`；E2B 的 ceiling 从不设 `workdir`/`fs_storage`
   ⇒ 生产 worker 形态（uid 65534、CapEff=0、BND `0xc3`）下 `max_disk=8M`，**单次 open 写 256 MiB 成功**，
   `/tmp/sandlock-cow-<uid>` 从未被创建。只补 `workdir` 后同一条路径立刻建分支并把 64 MiB 打成 ENOSPC。
2. **即使激活，强制点在"写 open"，不在写入字节。** 实测 `write`(64 MiB) / `O_APPEND` / `ftruncate` /
   `pwrite` / `mmap` 写回 / `fallocate` / `O_DIRECT` / 稀疏文件 / `sh -c 'cat > f'` / 静态 C / 静态 Go
   **全部先写成功**，只有**下一个 open** 才 ENOSPC；单次 open 256 MiB 照过。真正被拦的只有写 open、
   路径 `truncate`、`mkdir`（第 2049 个目录）。⇒ 它是"每文件/每 open"的粗粒度闸，不是字节级配额。
   > 本文档早前把 COW 描述成"每写一次即 ENOSPC"是**不准确**的，以本节实测为准。
3. **共享卷完全不进账**：挂进来的卷写穿 64 MiB，闸门仍开（与 COW 文档 §7"卷独立配额"一致，
   但意味着卷的配额只能靠本方案）。
4. **storage 落点不适合生产**：默认 `/tmp/sandlock-cow-<uid>`（槽位 `XDG_RUNTIME_DIR`/`TMPDIR` 未设）、
   0700、**节点本地**、与共享卷不同设备；worker 自身 EACCES、broker 白名单拒绝 ⇒ 控制面看不见；
   A 节点写完后节点猝死，B 节点看不到那份数据（跨节点迁移不成立）。
5. **成本**：写本身不慢，但 `close()` 的 merge 要 2 ms/文件（3000 个小文件 = 6.0 s）；copy-up 改 1 页要整份复制
   （64 MiB → 53.6 ms，期间占用 2.00×）；每次写 open 随 upper 条目线性变慢（5200 条 → 8.0 ms/open）。
6. **重启/崩溃**：计数只在内存、按代次从 0 起 ⇒ `max_disk=8M` 而 workdir 已有 10 MiB 时，新一代再写 10 MiB
   仍成功（合计 20 MiB）；崩溃后旧分支留在盘上却不带 `PRESERVED` marker，既不合并也不清理。

**因此**：要用 COW 当配额，等于**重做一遍配额机制**（接线 `workdir`/`fs_storage` + 强制点下沉到字节级 +
卷纳入 + storage 落共享卷 + 记账持久化），而不是打开一个开关。它今天真正可用的价值面只剩"事务/回滚"，
且受 merge 成本与节点本地 storage 限制。

**平台预置内容的约束（新增）**：沙箱需要预置基线内容（数据集、预装目录等）时，
**必须放进共享目录 + 只读绑定**，**不得复制进沙箱树**——复制会变成"每沙箱一份 = 计入沙箱配额 + 存储放大"，
破坏"配额 = 用户增量"这个语义。今天的镜像 rootfs 正是按这个约束做的。

**已登记缺口（Z-F7，2026-09-13 闭环）**：镜像缓存走 `E2B_IMAGE_CACHE_DIR`，默认值是**相对路径**
`tmp/sandboxes/_images`，线上 `.env` 未设置 ⇒ 缓存落在 **worker 容器内**、`up -d` 即丢，
且**不受任何配额约束**（不在沙箱 projid 内，也不在共享卷上）。

处置结果（口径见 `docs/production-deployment-requirements.md` §2.7）：生产形态把
`E2B_IMAGE_CACHE_DIR` 显式指到共享卷 `/var/lib/e2b-sandboxes/_images`（compose 的
worker-1/worker-2/control-plane 与 k8s 的 worker/control-plane 同源，且
`E2B_IMAGE_CACHE_OWNER_UID=65534` 把缓存交给 worker uid，使 root 控制面与 65534 worker
都能写、沙箱 uid 只能读）；因为 `_images` 属于 **project 0（无限额）**，容量上界由解析器
自己的按量 GC 承担（`E2B_IMAGE_CACHE_MAX_BYTES`，**默认不限、生产清单显式设 4 GiB**；
`_oci/` 布局 tar 只计不删；`total_bytes` 就是 `du` 口径的真实占用，含暂存残留与未完成
条目）——**没有**给 `_images` 单独建 project。逐出**跳过**任何被 `sandbox.json`
（`base_image`，或记录里的 digest）引用的条目与本次刚发布的条目，`MIN_AGE` 有 60s 硬下限。
跨进程安全：extraction 全程持 `<cache>/<image-slug>.lock` 的 `flock`（有超时，
`E2B_IMAGE_CACHE_LOCK_TIMEOUT_S`），并以「同文件系统暂存树 + 原子 rename」发布；
"清垃圾"先把残留原子抢到私有隔离名并复核，所以不可能删掉别人刚发布的条目；失败只清自己
创建的暂存物（原实现只有进程内锁，两个 worker 共享目录时会互相踩）。默认（不设 env 的
本地开发形态）落点不变：仍是相对路径。

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
- worker 侧：**`E2B_QUOTA_AGENT_URL` 存在即 agent 形态**（A6：它是唯一开关，
  优先于默认 false 的 `E2B_QUOTA_VIA_AGENT`；`E2B_QUOTA_AGENT_TOKEN` 同值），
  `envd_service.app.create_app` 自动 wire `xfs_quota.agent_query/agent_ops`
  HTTP 客户端；不设 URL 才是本地直连（dev/legacy 形态）。agent 不可达/401/
  协议错误一律抛 `ProjectQuotaError`，由既有调用方降级跳过 + 警告，不阻塞
  沙箱；agent 形态下 worker 不执行任何本地配额命令；
- 部署：`deploy/docker/Dockerfile.quota-agent` +
  `deploy/compose/docker-compose.quota-agent.yml`（NFS 服务器形态说明、
  `E2B_QUOTA_AGENT_PATH_MAP` 客户端→服务器路径映射）；生产栈里是
  `deploy/stack/docker-compose.prod.yml` 的 `quota-agent` 服务
  （`profiles: ["quota"]`，`cap_add: SYS_ADMIN` —— **特权只留在 agent 上**）。
- 非 root worker（E5.1，uid 65534）：`xfs_quota -x` 按 effective
  CAP_SYS_ADMIN 门控而非 euid。Docker/compose 形态对非 root 清零 CapEff，
  本地直连（未配 `E2B_QUOTA_AGENT_URL`）必然 EPERM，每沙箱磁盘硬限会
  静默失效——worker 启动时检测有效能力集并明确告警「磁盘配额不可用：非
  root 需启用 agent 形态（`E2B_QUOTA_AGENT_URL` 指向 quota-agent）」；
  生产非 root 部署必须部署 quota-agent，或 worker 以 root 运行（不推荐）。
  保留 effective CAP_SYS_ADMIN 的 worker（root，或 `capabilities.add
  [SYS_ADMIN]`）仍走本地直连、不触发告警 —— 但 **A6 之后部署清单不再声明
  `SYS_ADMIN`**：`deploy/stack` 与 `deploy/k8s/worker.yaml` 都改用 agent 形态，
  本地直连只剩显式自建的 dev/root 形态。
  非 XFS 宿主（如 macOS 本地开发）按真实原因披露（filesystem is
  apfs/..., not xfs），不误报非 root 指引。

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
- COW 评估记录（已否决）：[third_party/sandlock/docs/sandbox-level-cow.md](../third_party/sandlock/docs/sandbox-level-cow.md)
