# 生产部署要求（XFS project quota 磁盘配额）

<!-- 本文档同时收录 E6.4 NFS 共享存储形态部署要求与实测结论（§5）。 -->

## 1. 前置条件（已确认目标机满足）

| 要求 | 目标机现状 | 达标 |
|---|---|---|
| 文件系统为 **XFS** | `/dev/nvme0n1p2` = xfs | ✅ |
| XFS 支持 project quota（`projid32bit=1`） | `projid32bit=1` | ✅ |
| 内核 ≥ 4.5 | 6.12（EL 10.2） | ✅ |
| `xfs_quota` 工具 | quota 4.09 | ✅ |
| `lsattr`（e2fsprogs）| 未装则孤儿 project 只报不清 | ⚠️ **需安装**（`_scan_project_dirs` 靠它把 projid 映射回沙箱目录）|
| 挂载启用 `prjquota` | 当前 `noquota` | ⚠️ **需启用** |

## 2. 启用步骤（需维护窗口）

### 2.1 修改 /etc/fstab

根分区条目加 `prjquota` 挂载选项：

```fstab
/dev/nvme0n1p2 / xfs defaults,prjquota 0 0
```

### 2.2 在线启用（无需重启，首选）

```bash
mount -o remount,prjquota /
```

### 2.3 验证

```bash
grep " / " /proc/mounts          # 应看到 prjquota，不再是 noquota
xfs_quota -x -c "state" /        # 应显示 Project quota enabled
```

> 回退：`mount -o remount,noquota /` + 还原 fstab。XFS 的 quota 挂载选项
> 支持在线切换，无持久副作用。

## 2.4 每沙箱 host uid（E3.2）与 route-B 槽位（2026-09-09 起为默认开）

`E2B_PER_SANDBOX_UID` 现在默认 **true**：每个沙箱从池里拿一个独立 host uid，
workspace 按该 uid chown 0700。这不是可选项式的「加强安全」，而是另外两件事的地基：

- 共享卷的跨租户保护靠真实 DAC（1777+sticky），需要写者身份互不相同；
- chroot（镜像 rootfs）形态的 route-B `sandlock-supervise` 槽位就以该 uid 运行
  （`E2B_ROUTE_B` 默认 `auto`），路径中介的代打开由此落在沙箱自己身上（T5）。

要求与影响（逐条对照）：

| 项 | 说明 |
|---|---|
| 权限 | 只有 root（或 `CAP_SETUID`+`CAP_SETGID`+`CAP_CHOWN`+`CAP_SYS_ADMIN`）worker 能用。非 root worker 自动关闭 uid 池并保持「固定身份 + Landlock」（E5.1），启动时打一条 WARNING —— 也就是说现网 `user: "65534:65534"` 的 compose/k8s worker 行为与翻默认前**完全一致**。 |
| 容量 | 并发沙箱数受 `E2B_UID_POOL_SIZE` 约束（默认 1000，起始 `E2B_UID_POOL_START=10000`）；池满即建箱失败。多 worker 共用同一 workspace 时必须配**互不重叠**的段。 |
| 进程/内存 | chroot 形态每沙箱多一棵 supervise 进程树（supervise + sandlock-init + 停车 M0）。它在沙箱 cgroup **之外**，不计入 `max_memory`/`max_disk`，并在 `max_processes` 里占 1；容量表按「N 沙箱 = N 额外进程」重算。 |
| 回收 | route-B 代次的结束由 envd 生命周期（TTL/idle eviction/删除 → `executor.close()`）决定，不再依赖 core 的 15 min idle；槽位进程退出前该 uid 不会被再次租出（W1）。 |
| 文件系统 | uid 只对**支持属主的存储**有意义：repo 的 virtiofs bind 挂载上 chown 是 no-op，生产请用容器原生 / XFS（本项目门禁把 workspace 放 `/var/lib/e2b-sandboxes` 的 XFS+prjquota 上）。 |
| 关掉它 | 显式 `E2B_PER_SANDBOX_UID=false` 回到旧的共享 uid（1000）形态：**pure（无 base image）形态照常跑**；**chroot 形态在 root worker 上会被 fork 直接拒绝建箱**（沙箱 host uid 1000 ≠ 中介 euid 0 ⇒ `mediation_run_as=caller refused`，见下条）。非 root worker 不受影响（沙箱就用 worker 自己的 euid，中介身份与沙箱身份同一个）。E2B 曾下发的 `mediation_run_as='supervisor'` 降级档（代打开文件属主变 worker = T5）已于 2026-09-10 删除，不再有静默逃生门。 |
| 删档的后果（2026-09-10） | 「特权进程内中介 + 路径中介 + 非 0 host uid」这一组合现在只有 route B 一条路：root worker + chroot 形态若拿不到槽位（`E2B_ROUTE_B=off`、wheel 不带 supervise、或 `E2B_PER_SANDBOX_UID=false`）就**建箱失败**，并打一条 ERROR 说明为什么没有槽位、怎么修（容器实测钉在 `tests/security/test_template_isolation.py::test_in_process_chroot_is_refused_without_a_slot`）。非 root worker（E5.1，中介就是它自己的 euid）**不受影响** —— 那条组合不构成拒绝，所以也不会打这条 ERROR。 |
| **CAP_SYS_PTRACE**（进程内 RunAs 才有） | 内核要求「给子进程写 `uid_map`」除 `CAP_SETUID` 外还要对该子进程的 **ptrace 访问权**。实测 `--cap-drop ALL`：只补 `SYS_ADMIN` ⇒ 每个建箱都挂在 `sandlock_create failed`；只再补 `SYS_PTRACE` 即通。**route B 不需要它**（槽位自己就是那个 uid，自映射 `0 -> euid` 无需特权）。root worker + 非 chroot 形态开 E3.2 时，worker 启动会打 WARNING（`PER_UID_NO_PTRACE_WARNING`）说明缺哪条 cap、怎么修。 |

## 2.5 门禁容器的两种形态（别把测试特权当成生产需要）

- **`deploy/scripts/test-prod-shaped.sh`（生产形，默认推荐）**：容器不带 `--privileged`，
  `--cap-drop ALL` 后只给部署清单声明的那组 cap（Docker 默认集 + `SYS_ADMIN`
  `SYS_PTRACE` `NET_ADMIN`，`seccomp=unconfined`）。实测（2026-09-10）Landlock（ABI 8）
  与非特权 userns 都不需要任何特权；**唯一造不出来的是 XFS prjquota 暂存盘**
  （容器内 loop 设备不可用，即使 `--cap-add SYS_ADMIN` + `--device /dev/loop-control`
  也 `failed to setup loop device`）⇒ 7 个配额文件显式 `--ignore`，
  而 `E2B_TEST_STRICT_SKIPS=1` 仍开，漏列就变 error 而不是静默少跑。
  `NET_ADMIN` 是给**夹具**用的（往 lo 上放 198.18.0.99 作为可 allow/deny 的真实源地址），
  worker 自身不需要。
- **phase 2：`--user 65534:65534` 的无特权 worker 跑法**（`UNPRIVILEGED_PHASE=0` 可跳）：
  上面那条「生产形」lane 仍是 **root**（只是把 cap 削到部署等价集），而
  `docker-compose.prod.yml` 真正写的是 `user: "65534:65534"` —— 那是**第三种形态**：
  没有 `CAP_SETUID` ⇒ uid 池自动关、租不到 route-B 槽位、沙箱就用 worker 自己的 euid
  （E5.1），路径中介留在进程内。删掉 `mediation_run_as` 降级档之后，这一形态
  必须自己站出来跑一遍：`tests/security/test_template_isolation.py` +
  `tests/security/test_sandlock_isolation.py` + 两份 route-B/policy 单测，
  实测 `47 passed / 1 skipped / 0 failed`（唯一的 skip 是那条「euid 0 才谈得上被拒」
  的钉桩）。phase 2 用 `python:3.11-slim` 作基镜像（`PHASE2_BASE_IMAGE` 可改）：
  本地构建的 `python-mcp:3.14` 镜像在 registry 镜像站的白名单外，且它的 rootfs
  缓存落在 phase 1 那套 harness 目录里，65534 写不进去。
- **原有特权跑法**：继续承担 XFS 配额全量与 loop 相关用例。
  两条 lane 的差集只应当是「配额/loop」这一类，任何别处的差集都是生产可用性缺陷。

## 3. 运维要求

### 3.1 quota 管理（E2B worker 自动执行）

```bash
# 创建沙箱
xfs_quota -x -c "project -s -p /var/lib/e2b-sandboxes/<id> <projid>" /
xfs_quota -x -c "limit -p bhard=<quota>M <projid>" /

# 删除沙箱
xfs_quota -x -c "project -C -p /var/lib/e2b-sandboxes/<id> <projid>" /
rm -rf /var/lib/e2b-sandboxes/<id>
```

- projid 范围：1..2^31-1（32 位 project id），沙箱 id 哈希分配；
- 孤儿清理：worker 启动时对账 `sandbox.json` 与 quota 表，删除无主 project；
- 监控：`xfs_quota -x -c "report -p" /` 定期巡检，超限沙箱告警。

### 3.2 兼容性注意

- **EDQUOT 而非 ENOSPC**：超限写返回 `EDQUOT`，需确认沙箱内工具能正确处理
  （大多数处理一致，个别只处理 ENOSPC 的程序要验证）；
- **内核版本锁定**：开启 project feature 后，旧内核（< 4.5）无法挂载该文件
  系统——升级/回滚内核要评估；
- **挂载选项必须配套**：`prjquota` 挂载选项与 quota feature 配套，部署脚本
  要保证两者一致（漏挂 = quota 不生效但不报错）。

### 3.3 环境矩阵

| 环境 | quota | 说明 |
|---|---|---|
| 生产（目标机 XFS） | ✅ 生效 | 本文档要求 |
| 测试机（OrbStack 容器内 XFS loop） | ✅ 生效 | 需 `--privileged` + losetup 挂载 |
| 开发机（macOS / Docker Desktop / OrbStack 默认 VM） | ⏭️ 降级跳过 | E2B 检测不到 XFS 则跳过 + 警告 |
| CI（Linux 任意 fs） | mock | 单测不依赖真实 quota |

## 4. 验收清单（部署后）

1. `grep " / " /proc/mounts` 含 `prjquota`；
2. `xfs_quota -x -c "state" /` 显示 project quota enabled；
3. 创建沙箱后 `xfs_quota -x -c "report -p" /` 能看到该沙箱的 project 与限额；
4. 沙箱内写超配额 → 命令报 `EDQUOT`/写入失败；
5. 删除沙箱后 project 记录清理；
6. 迁移沙箱（worker 间）→ 配额保留；
7. 非 XFS 环境创建沙箱 → 正常创建 + 日志警告"project quota unavailable"。

## 5. NFS 共享存储形态（E6.4 实测结论与部署要求）

多节点共享 workspace/volume 采用 NFS（或等价 CSI）时，必须满足以下
语义；实测（2026-09-02，容器内核 nfsd + XFS prjquota 导出 + 两个 NFS
客户端挂载）结论如下。

### 5.1 路径语义与迁移

- **路径语义**：同一导出挂在两个 worker 上，`volume/<sandbox_id>/` 与
  workspace 目录完全一致（两挂载点文件列表、md5 相同），worker 侧无需
  区分“本机目录”与“共享目录”。
- **迁移保留文件**：`E2B_SHARED_WORKSPACE_ROOT` / `E2B_SHARED_VOLUME_ROOT`
  下的迁移只切路由、不搬文件（keepFiles），实测 NFS 上文件与 projid
  均保留；跨挂载点读写正常（迁移目标 worker 直接读到源文件）。
- **各节点挂载选项必须一致**：`rw,sync,no_subtree_check`（及 NFSv3
  `nolock` 如无锁服务）；`vers` 与 `sec` 需一致，否则配额与权限行为
  随节点漂移。

### 5.2 配额专项（服务器端 XFS + prjquota）

配额在 NFS **服务器端**执行；客户端通过 NFS 拿到的行为：

1. **projid 继承**：服务器端 `xfs_quota project -s` 设置
   `volume/<sandbox_id>/` 后，NFS 客户端在该目录创建的文件（含子目录
   递归）projid 正确继承（实测 `lsattr -p` = 目标 projid）。
2. **EDQUOT 传播形态**：服务器端超限返回 EDQUOT，NFS 客户端表现为
   **ENOSPC（errno 28）**，不是 EDQUOT——沙箱内工具需同时处理
   ENOSPC（多数只处理 ENOSPC 的程序反而正确）。
3. **sync 挂载**（建议，E2.6 concern）：写入在达到硬限额的写调用上
   立即返回 ENOSPC（探针断言：客户端写入计数 = 限额且 errno 28；服务器端
   `stat -c %s` = 限额字节，文件恰在限额处截断）。
4. **async 挂载**（不推荐）：全部写入进入页缓存后，`close()` 与
   `fsync()` 均返回 ENOSPC（探针断言：客户端写入 `限额+8` MiB 全部成功、
   两个错误路径均 errno 28；服务器端 `stat -c %s` 落盘量**少于客户端
   写入量且不固定**——本轮实测 close.bin 22MiB / fsync.bin 0MiB，异步
   突发下服务器可能已越过硬限额——忽略延迟错误会**静默丢数据**）。生产
   必须 `sync` 挂载，且命令执行器在写路径上显式 `fsync` 并透传错误。
5. **多 worker 独立限额**：两个 worker 同时写各自
   `volume/<id>/`（不同 projid），各自独立达到硬限额，服务器端 report
   分项目记账、互不串扰（探针断言：两客户端各自写入计数 = 限额、errno 28，
   report 显示 projid 1004/1005 各自 16MiB 截断）。
6. **root_squash / uid 映射**：
   - `root_squash` 下客户端 root 映射为 nobody：projid 继承与配额记账
     不受影响（实测文件属 nobody、projid 仍继承、限额仍生效）；
   - 但**沙箱目录权限必须可写**：volume 根建议 `1777` + 每沙箱独立
     子目录（E3.2 模型），否则 squashed 写被 EACCES 拒绝（实测 755
     root 属主目录 → EACCES）；
   - **E5.1 独立 uid 模型与 root_squash 冲突**：sandbox 内 uid 0 经
     NFS 被 squash 后失去独立 uid 语义，文件统一为 nobody 属主，租户间
     NFS 文件隔离退化为“目录权限”而非“uid”。生产若开启 per-sandbox
     uid + NFS 共享卷，建议导出用 `no_root_squash`（内网可信 NFS）或
     改用 `anonuid=<per-sandbox-uid>` 映射（需 NFS 支持 per-export
     配置），并在部署验证中逐项核对。

### 5.3 部署待验证项（环境限制）

本次实测在 OrbStack 容器内内核 nfsd 完成；OrbStack 宿主自带 NFS 代理
占用标准端口（2049/20048）且状态随宿主变化，容器化 nfsd 的线程/导出
表为 VM 内核全局状态，重复自动化运行不稳定（ESTALE / 挂载竞态），因此
**以下项需在真实生产 NFS（Linux 目标机）上复核**：

- `deploy/scripts/nfs_quota_probe.sh` 在目标机 NFS 挂载点重跑全 6 项
  （A projid 继承 / B sync ENOSPC + 服务端截断 / C async close/fsync
  延迟报错 + 服务端落盘量核对（少于客户端写入） / D 多 worker 独立限额 /
  E root_squash / F 共享路径+迁移保留）；
- 生产 NFS 的 `no_root_squash` 与 per-sandbox uid 组合是否保留 uid 隔离；
- NFSv4 与 v3 在目标内核/导出配置下的行为差异（本次用 v3）；
- 配额巡检（`xfs_quota report -p`）在服务器端持续校验，与 worker
  心跳告警对齐。
