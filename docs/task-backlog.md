# 待完成任务总清单（路线图）

汇总 2026-08-31 ~ 09-01 所有分析产生的待办，按优先级分组。

## 归属方说明

| 归属 | 代码库 | 改动/验证方式 |
|---|---|---|
| **sandlock** | `third_party/sandlock` 子模块（Rust fork） | 改 fork → 构建 wheel（`build-sandlock-wheels.sh`）→ 子模块提交 |
| **E2B 服务端** | 本仓库 `control_plane/` `envd_service/` `gateway_common/` `deploy/` | 直接改 + 单测 + 部署 |
| **运维** | 部署环境（目标机/代理） | 配置变更 + 维护窗口 |

---

## sandlock fork 侧任务

### S1. 安全与隔离

| # | 任务 | 优先级 | 状态 |
|---|---|---|---|
| S1.1 | PID namespace（`CLONE_NEWPID`，fork 结构改造，随独立 uid 评估） | P2 | 未开始 |
| S1.2 | 独立 uid 的 userns 单 entry 约束验证（配合 E2B 分配 uid） | P2 | 未开始 |
| S1.3 | 无特权运行验证（uid 65534 全绿，配合 E2B worker 非 root） | P4 | 未开始 |

### S2. per-sandbox 网络隔离（方案 1，技术已验证）

> PoC 已证实：非 root 下 `process_vm_readv` + ADDFD 注入 userns 子进程可行，
> 注入 fd 数据面 RTT 0.006ms / ~14 Gbps（纯内核）。
> 参考 `docs/netns-isolation-fd-injection.md`、`tmp/addfd_probe.c`。

| # | 任务 | 状态 |
|---|---|---|
| S2.1 | connect handler 改造：宿主建连 + ADDFD 注入（适配返回 fd 号语义） | 未开始 |
| S2.2 | 沙箱创建 `unshare(CLONE_NEWNET)` + `lo up` | 未开始 |
| S2.3 | DNS 适配（getaddrinfo 通知解析；网关搬进沙箱 netns） | 未开始 |
| S2.4 | UDP 适配（connected 注入 / datagram on-behalf） | 未开始 |
| S2.5 | 入站端口映射（可选，E2B 入站低需求） | 未开始 |

### S3. 收尾

| # | 任务 | 状态 |
|---|---|---|
| S3.1 | 已实现改动（notify_rate_limit）构建 wheel 后**子模块提交** | 待执行 |
| S3.2 | Rust 全量测试（Linux 回归，lib 745+） | 待执行 |

---

## E2B 服务端侧任务

### E1. P0：部署已实现的安全修复（最高优先，生产漏洞仍在）

| # | 任务 | 状态 | 验证 |
|---|---|---|---|
| E1.1 | 构建镜像并推送 ACR（含新 sandlock wheel） | 待执行 | `build-and-push.sh` |
| E1.2 | 部署（upgrade.sh，自动保留/生成 redis 密码） | 待执行 | `upgrade.sh` |
| E1.3 | 远程复测内网隔离（OPEN 沙箱应拒绝内网 TCP） | 待执行 | `tmp/security-probe2.py` |
| E1.4 | 控制面 API 加 TLS（代理层或 HTTPS） | 未开始 | curl https 检查 |

### E2. P1：XFS project quota 磁盘配额（方案已定稿）

> 目标机已确认：XFS + projid32bit=1 + 内核 6.12，当前 `noquota` 需启用。

| # | 任务 | 状态 | 验证 |
|---|---|---|---|
| E2.1 | `_xfs_project_supported` 检测 + 非 XFS 降级 + 单测 | 未开始 | mock 单测 |
| E2.2 | 沙箱创建/删除的 project 管理（xfs_quota project/limit） | 未开始 | 目标机/OrbStack 实测 |
| E2.3 | 命令串行锁（ProcessManager per-sandbox asyncio.Lock） | 未开始 | 并发命令排队/429 |
| E2.4 | 孤儿 project 清理 + 磁盘水位监控 | 未开始 | worker 启动扫描 |
| E2.5 | volume 独立配额（quota_mb 元数据 + 写前检查） | 未开始 | volume 专项 |

### E3. P2：安全后续评估项（架构级）

| # | 任务 | 等级 | 状态 |
|---|---|---|---|
| E3.1 | 租户隔离（资源归属 + per-tenant 配额/限流，见 tenant-isolation.md） | 高 | 未开始 |
| E3.2 | 每沙箱独立 uid 分配 + volume 权限模型（sandlock 配合验证） | 中-高 | 未开始 |
| E3.3 | volume token 过期/吊销 | 中 | 未开始 |
| E3.4 | template upload token 上传后失效 | 中 | 未开始 |
| E3.5 | 模板构建并发/资源限流 | 中 | 未开始 |
| E3.6 | internal key 轮换机制 | 低-中 | 未开始 |

### E4. P3：内存 DoS 修复

| # | 任务 | 状态 |
|---|---|---|
| E4.1 | 命令输出缓存上限（ProcessManager.captured capped buffer） | 未开始 |
| E4.2 | 文件写入 API body 大小限制 + 流式写盘（volumecontent/template/files.write） | 未开始 |

### E5. P4：权限/供应链/加固

| # | 任务 | 状态 |
|---|---|---|
| E5.1 | worker/supervisor 非 root（Dockerfile USER + 权限适配，sandlock 配合验证） | 未开始 |
| E5.2 | 核心依赖锁版本（== 或 lockfile） | 未开始 |
| E5.3 | metadata/envVars、模板/快照/构建产物大小限制 | 未开始 |
| E5.4 | secret 加密/持久化、凭据管理（.env/bastion.env） | 未开始 |

### E6. P5：运维一致性

| # | 任务 | 状态 |
|---|---|---|
| E6.1 | 节点失联沙箱清理/worker 恢复对账（僵尸沙箱） | 未开始 |
| E6.2 | 镜像 digest 固定（E2B_BASE_IMAGE @sha256:） | 未开始 |
| E6.3 | MCP 端口回收复用（_next_mcp_port 只增不减） | 未开始 |

### E9. 资源争用与驱逐（新增，见 resource-contention.md）

| # | 任务 | 状态 |
|---|---|---|
| E9.1 | 空闲检测（last_active_at + 阈值配置） | 未开始 |
| E9.2 | pause 释放配额改造（前置） | 未开始 |
| E9.3 | 驱逐选择器 + kill/pause + 通知 | 未开始 |
| E9.4 | 创建排队/分层池集成 | 未开始 |

### E7. 网络隔离联动（方案 1 的 E2B 侧）

| # | 任务 | 状态 |
|---|---|---|
| E7.1 | MCP/gateway 到沙箱内 MCP 的路径适配 | 未开始 |
| E7.2 | 网络配置透传与全量 SDK/安全测试回归 | 未开始 |
| E7.3 | Linux 集成验证（配合 sandlock S2） | 未开始 |

### E8. 收尾

| # | 任务 | 状态 |
|---|---|---|
| E8.1 | 部署后远程 smoke 回归 | 待执行 |
| E8.2 | 本地测试基线确认（267 passed unit/contract） | ✅ 已确认 |

---

## 运维侧任务

| # | 任务 | 状态 |
|---|---|---|
| O1 | XFS `prjquota` 启用：fstab + remount（维护窗口） | 未开始 |
| O2 | TLS 证书/代理层配置（配合 E1.4） | 未开始 |
| O3 | 凭据管理（ACR 密码、API key、redis 密码、SSH 口令上密钥管理） | 未开始 |

---

## 执行顺序建议

1. **E1 全部**（部署 + 复测 + TLS）——生产漏洞清零；
2. **E2**（XFS quota + 串行锁）——磁盘配额落地；
3. **E3**（租户隔离 + 独立 uid）——授权与内核级隔离；
4. **E4**（内存 DoS）——远程可触发的资源耗尽；
5. **S2/E7**（网络隔离方案 1）——独立项目，与 E5/E6 按业务优先级排期；
6. **S1/S3/E5/E6**（纵深/供应链/一致性）——穿插进行。
