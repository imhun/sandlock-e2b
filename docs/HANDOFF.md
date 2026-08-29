# 会话交接记录（2026-08-29）

> 供新会话快速接续。当前基线：Linux 容器（privileged + host 网络）
> `225 passed, 1 skipped`；macOS `200 passed, 6 skipped`
> （unit + contract + sdk/python + sdk/js，JS SDK 本会话已在宿主跑通）。

## 本会话已完成

1. **P0 并发迁移锁**：`SandboxRegistry.try_acquire_migration/release_migration`
   —— Redis 多副本用 `SETNX` 标记 + TTL（WATCH 对比删除，兼容 fakeredis），
   单进程用等价内存锁；并发 migrate 第二个请求返回 409；失败路径
   `finally` 释放标记。单元测试覆盖跨副本互斥、过期释放、错误 token 不能
   释放；契约测试覆盖持锁 409 + 释放后可迁移。
2. **P1 双活窗口关闭**：迁移先调源节点 `DELETE ?keepFiles=true`（停
   runtime、杀进程树、保留文件）再导出/导入/切路由；失败时自动在源节点
   重新 provision（`_provision_local` 现支持幂等替换过期挂载符号链接），
   并回滚已持久化的 node_id。契约测试：目标 provision 失败（指向控制面
   自身地址 → 快速 404）后沙箱命令仍可用。
3. **P1 rootfs 缓存失效**：`resolve_image_rootfs` 缓存目录名加入镜像
   digest（`docker image inspect` 的 RepoDigests/Id），tag 更新自动换新
   rootfs；先 pull + login 再算 digest，避免首次解析重复解包。README 补充
   说明与手动清理方式。
4. **P2 JS SDK 测试在宿主跑通**：`pytest tests/sdk/js`（npm 在宿主机），
   vitest 全量通过；README 测试表更新为 macOS / Linux 均可。

## 本会话验证结果

```text
macOS: 200 passed, 6 skipped（tests/unit + tests/contract + tests/sdk/python + tests/sdk/js）
Linux: 225 passed, 1 skipped（全量含 Sandlock/registry/真实 Redis/模板隔离）
```

## 本会话已完成

1. **Template COPY 文件上下文**：`GET /templates/{id}/files/{hash}`（201）返回
   带 token 的上传 URL，`PUT .../upload` 校验 token 并存储归档；构建时解包进
   `ctx/` 作为 docker build context；COPY 步骤生成 Dockerfile
   （`--chown/--chmod`）；构建先 push 成功才置 ready。
2. **真实 Redis 多副本**：`SandboxRegistry.save()` 持久化 node_id/TTL/暂停等
   变更；Redis 模式下 `get/list` 总读共享存储；`E2B_REDIS_URL` 生效；
   `tests/contract/test_redis_multireplica_e2e.py` 用真实 redis（容器内
   `redis-server` 进程，macOS 回退 docker redis:7）。
3. **节点故障迁移**：`POST /sandboxes/{id}/migrate`（可选 `nodeID`）——
   agent `export/import`（tar.gz）、配额转移、源清理、gateway 路由失效
   （`E2B_GATEWAY_URL`）；非共享卷按卷亲和约束。
4. **命令输出日志**：`command-logs.jsonl` 落盘（stdout/stderr/PTY、ANSI
   剥离、1MB/命令 + 16MB/文件截断），`GET /sandboxes/{id}/logs` 合并返回；
   远程经 `GET /agent/sandboxes/{id}/logs` 拉取。
5. **共享 workspace 模式**：`E2B_SHARED_WORKSPACE_ROOT` 时迁移只切路由
   （跳过 export/import），源目录不删（`DELETE /agent/sandboxes/{id}
   ?keepFiles=true` 仅释放 runtime）。
6. **镜像仓库分发**：`E2B_IMAGE_REGISTRY` 构建后 tag+push，模板镜像名切到
   `{registry}/{templateID}`；worker resolver 本地无镜像先 `docker pull`。
7. **镜像仓库认证**：`E2B_IMAGE_REGISTRY_USERNAME/PASSWORD`（控制面+worker），
   `docker login --password-stdin`（密码不进 argv）。

## 未完成 / 待办（按优先级）

### P2 — 真实 NFS 部署未验证

共享 workspace/volume 目前只在同一主机共享目录模拟；NFS/CSI 上的
root_squash、uid=1000 映射、命令 IO 延迟未实测。部署验证时注意
`E2B_SHARED_VOLUME_ROOT` / `E2B_SHARED_WORKSPACE_ROOT` 各节点路径语义一致。

### P3 — 遗留优化

- 迁移导出 tar 仍含卷挂载符号链接空条目（功能等价，可显式排除）；
- 未配置 `E2B_GATEWAY_URL` 时迁移后路由依赖 gateway 30s 缓存 TTL（文档已知）；
- spec.md 其余官方 API 面（network/iam/lifecycle 等）仍未支持，入口处
  `UNSUPPORTED_FIELDS`/`UNSUPPORTED_ENDPOINTS` 明确拒绝。

## 验证命令与基线

```bash
# macOS（端口绑定需提权）
tmp/venv/bin/python -m pytest tests/unit tests/contract tests/sdk/python \
  -q -p no:cacheprovider

# Linux 容器全量（含真实 Redis / registry 认证 / Sandlock 用例）
docker run --rm --privileged --network host \
  -e E2B_BASE_IMAGE=python:3.11-slim \
  -e E2B_HOST_PROJECT="$(pwd)" \
  -v ~/.orbstack/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd):/workspace" -w /workspace \
  e2b-sandlock-test:latest pytest tests --perf -q -p no:cacheprovider
```

- 测试镜像 `e2b-sandlock-test:latest`（Dockerfile.test-runner，国内源，
  已含 redis-server）；改依赖后需重建。
- `--network host` + `E2B_HOST_PROJECT` 是容器内 docker CLI 访问宿主
  localhost 端口 / 挂载宿主路径的前提（registry/Redis 端口映射、htpasswd
  挂载）。
- 多节点冒烟：`scripts/multinode_smoke.py`；compose：
  `docker compose -f docker-compose.multinode.yml up -d`。

## 关键文件索引

| 文件 | 内容 |
|------|------|
| `control_plane/api/sandboxes.py` | migrate（per-sandbox 锁 + 先停源 runtime + 失败回滚）、logs 合并、`_command_logs`、keep_files 销毁 |
| `control_plane/registry/manager.py` | Redis save/get/list、TTL 回收、`try_acquire_migration`/`release_migration`（SETNX + TTL / 内存锁） |
| `control_plane/api/templates.py` | COPY 上传链路、registry push/login |
| `control_plane/registry/nodes.py` | `select_and_reserve(exclude_node_id)`、`reserve_node` |
| `envd_service/agent.py` | export/import/logs/keepFiles 端点 |
| `envd_service/process/logs.py` | 命令输出 JSONL 采集 |
| `envd_service/runtime/image_resolver.py` | rootfs 解包、pull、registry login、digest 缓存 key |
| `envd_service/gateway.py` | 路由缓存 + `/internal/routes/{id}/invalidate` |
| `tests/conftest.py` | live/multinode/registry/redis fixtures（session 级） |
| `tests/contract/test_migration.py` | 迁移 + 共享 workspace + 持锁 409 + 失败回滚用例 |
| `tests/contract/test_redis_multireplica_e2e.py` | 真实 Redis 多副本 |
| `tests/contract/test_command_logs.py` | 命令日志合并（本地 + 远程） |
| `tests/contract/test_template_upload.py` | COPY 上传契约 |
| `tests/sdk/python/test_templates.py` | 构建、COPY、registry push/pull/认证 |
| `tests/unit/test_sandbox_registry.py` / `test_redis_multireplica.py` | 迁移锁单元测试（内存 + fakeredis） |

## 配置速查（新增项）

```text
E2B_SHARED_WORKSPACE_ROOT      共享工作目录（迁移只切路由）
E2B_IMAGE_REGISTRY             模板镜像 push 目标
E2B_IMAGE_REGISTRY_USERNAME    仓库认证（控制面 push / worker pull）
E2B_IMAGE_REGISTRY_PASSWORD    仓库认证
E2B_GATEWAY_URL                迁移后通知 gateway 失效路由
```
