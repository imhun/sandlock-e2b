# 租户隔离方案（per-tenant 资源授权）

## 1. 背景与问题

当前控制面所有 API key 权限相同：持有任一 key 可列/删**所有**沙箱与
volume、读所有快照/模板、挂载任意 volume 读内容。多租户场景下 key A
用户可操作 key B 用户的数据——无资源归属/授权模型。

现状确认：

- 资源记录（`SandboxRecord` / `VolumeRecord` / `SnapshotRecord` /
  `TemplateRecord` / `SecretRecord`）均无归属字段；
- `require_api_key`（`control_plane/auth.py`）只校验 key 在列表内，不区分
  调用者，返回 key 字符串。

## 2. 核心模型：tenant + key 映射

### 配置（新增环境变量）

```env
# 租户映射：tenant_id -> API key 列表
E2B_TENANTS={"t1": ["keyA", "keyB"], "t2": ["keyC"]}
# 管理员 key（绕过租户隔离，运维用）
E2B_ADMIN_API_KEYS=admin-key
# 每租户资源配额（可选；未配置 = 共享全局配额，不隔离资源抢占）
E2B_TENANT_LIMITS={"t1": {"max_sandboxes": 20, "max_total_memory_mb": 4096,
                          "max_total_cpu_percent": 200, "max_total_disk_mb": 10240,
                          "max_total_processes": 512},
                   "t2": {"max_sandboxes": 50, "max_total_memory_mb": 8192}}
# 每租户创建限流（每分钟；未配置 = 用全局 E2B_CREATE_RATE_LIMIT_PER_MIN）
E2B_TENANT_RATE_LIMITS={"t1": 60, "t2": 300}
```

- **未配置 `E2B_TENANTS` = 单租户兼容模式**：所有 key 共享，无过滤
  （现状不变，存量部署零影响）；
- 配置后：请求 key 解析出 tenant，资源按 tenant 隔离。

### 归属字段

每个资源记录增加 `tenant_id: str | None`：

- 创建时从请求 key 解析 tenant 写入；
- 兼容模式（未配置租户）下 `tenant_id = None`（不隔离）。

## 2.1 per-tenant 配额（防资源抢占）

全局资源池（`E2B_MAX_SANDBOXES`、总内存/CPU/磁盘）是共享的，租户可占满
导致其他租户创建失败。`E2B_TENANT_LIMITS` 为每个租户设独立上限：

- **记账维度**：`SandboxRegistry` 增加 tenant 维度记账
  （`_tenant_reserved: dict[tenant, dict[dim, int]]`），与全局记账并行；
- **准入检查**：创建沙箱时同时校验全局 + tenant 配额，任一不足返回 503；
- **预留/释放**：创建预留、kill/TTL 释放，与全局记账同流程；
- **未配置的租户**：只受全局配额约束（不隔离抢占，文档注明）；
- **admin key**：创建资源时不受 tenant 配额限制（但仍受全局约束）。

配额维度对齐现有全局维度：`max_sandboxes`、`max_total_memory_mb`、
`max_total_cpu_percent`、`max_total_disk_mb`、`max_total_processes`。

## 2.2 per-tenant 创建限流

现有创建限流是 per-key（`SlidingWindowRateLimiter`），同租户多 key 可
绕过单 key 限流。`E2B_TENANT_RATE_LIMITS` 增加 tenant 维度：

- 创建沙箱时校验 **key 限流 + tenant 限流**（两者独立记账，都通过才放行）；
- 未配置的租户：回落到全局 `E2B_CREATE_RATE_LIMIT_PER_MIN`。

## 3. 授权检查（API 层）

| 接口类型 | 行为 |
|---|---|
| 列表（GET /sandboxes、/volumes、/snapshots、/templates、/secrets） | 按 tenant 过滤（admin 看全部） |
| 单资源（get/delete/update /sandboxes/{id} 等） | 校验归属，不匹配返回 **404**（防存在性泄露） |
| 跨资源操作 | 创建沙箱挂 volume / 从快照模板创建 / secret 注入：**资源 tenant 必须与沙箱 tenant 一致**，否则 403 |

统一封装：

- `tenant_of(request)`：key → tenant + admin 判断（`control_plane/auth.py`）；
- `_require_owned(request, record)`：归属校验 helper，各 handler 调用。

## 4. 关键设计点

1. **volume token 语义不变**：token 是独立凭证（96-bit 随机，持有即有权）；
   租户隔离管"列表/删除/挂载"，token 泄露由"过期/吊销"项单独处理
   （见 `docs/security-hardening.md` §4）；
2. **internal API 不走租户**：worker ↔ 控制面用 `X-Internal-Key`，与租户无关；
3. **ID 仍全局唯一**：沙箱/volume ID 不按租户命名空间，隔离靠过滤+校验；
4. **SDK 零改动**：SDK 本来就在传 `X-API-Key`；
5. **节点/调度不感知租户**：worker 共享，租户隔离只发生在控制面 API 层
   （数据面隔离由独立 uid / 网络策略承担，见 `docs/security-hardening.md`）。
6. **数据面边界（重要）**：租户隔离是**授权层**，不替代沙箱运行时隔离。
   租户 A 的沙箱与租户 B 的沙箱共享 worker/uid/netns，数据面风险由
   Landlock（文件路径）、网络策略（出站）、独立 uid / 网络隔离（未来）
   承担——两者必须一起做才完整。
7. **管理面**：admin key 可查看全部资源；新增只读管理端点
   （`GET /internal/tenants`，X-Internal-Key 鉴权）返回每租户用量
   （已用/配额），供运维对账与告警。

## 5. 兼容与迁移（关键决策点）

- 存量资源 `tenant_id = None`：**启用租户隔离前必须迁移归属**，否则存量
  数据变成"无主"；
- 推荐：部署时提供一次性迁移脚本（按 name/创建时间归租户，或全部归默认
  租户/admin）；
- 兼容模式 → 隔离模式的切换是**配置变更**，无代码路径差异；
- `E2B_ADMIN_API_KEYS` 为空时建议强制要求 `E2B_TENANTS` 非空（避免误配
  导致所有 key 变 admin）。

## 6. 实施步骤

1. registry 各记录加 `tenant_id` + 持久化（`sandbox.json`、volume 记录等）；
2. `auth.py`：`tenant_of` / `E2B_TENANTS` / `E2B_ADMIN_API_KEYS` 解析；
3. API handlers：列表过滤 + 单资源校验（统一 helper）；
4. 跨资源校验（volume/快照/模板/secret 与沙箱 tenant 一致性）；
5. `SandboxRegistry` 增加 tenant 维度配额记账 + 准入检查（§2.1）；
6. 创建限流增加 tenant 维度（§2.2）；
7. 管理端点 `GET /internal/tenants`（用量/配额）；
8. 迁移脚本 + 单测。

## 7. 测试矩阵

| 用例 | 预期 |
|---|---|
| t1 key 列表 | 只看到 t1 资源 |
| t1 key 访问 t2 资源 | 404（不泄露存在性） |
| t1 key 挂载 t2 volume | 403 |
| admin key | 全量可见可操作 |
| 兼容模式（未配置租户） | 无过滤，现状行为 |
| 存量资源迁移 | 归属正确，无"无主"资源 |
| t1 占满租户配额后 t1 再创建 | 503（tenant quota exceeded） |
| t1 占满配额不影响 t2 创建 | t2 正常（配额隔离） |
| t1 多 key 并发创建 | 受 tenant 限流约束（非仅 per-key） |
| 未配置 E2B_TENANT_LIMITS | 仅全局配额约束 |
| 管理端点 | admin/internal key 可查每租户用量 |

## 8. 工作量与排期

控制面中等-偏大改动（registry + auth + API 层 + 配额记账 + 限流 + 测试），
约 1.5-2 周。排期见 `docs/task-backlog.md` E3.1（P2）。
