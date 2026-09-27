# 安全审计基线（2026-09-16）

审计起点：确认"改动前的项目真实状态"，作为后续所有"已修复/未修复"结论的对照。

| 项 | 值 |
|---|---|
| commit | `39f1cc9fd0fb93766d426eb2b7f6227df4b7f636` |
| 时间 | 2026-09-16T10:19Z |
| 测试镜像 | `e2b-sandlock-test:latest` (`b1f0200714f7`) |
| 运行时内核 | Docker 29.4.0 / OrbStack，容器内 Landlock ABI = **8** |
| 基镜像 | `python-mcp:3.14`（本地 registry `127.0.0.1:5080` 预置） |

## 基线命令

```bash
E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
E2B_BASE_IMAGE=python-mcp:3.14 \
UNPRIVILEGED_PHASE=0 \
./deploy/scripts/test-prod-shaped.sh tests/unit tests/contract tests/security
```

## 基线结果

```
1475 passed, 3 skipped, 0 failed, 3545 warnings in 394.02s (0:06:34)
```

三条 skip 均为**运行时能力**跳过，不是放过的问题：

1. `tests/contract/test_pure_shape_workspace_ownership.py:113` — pure-sandlock 契约需要空 `E2B_BASE_IMAGE`（gate B 形态）；
2. `tests/security/test_template_isolation.py:164` — 两个生产清单发的非 root worker 形态；
3. `tests/unit/test_uid_pool.py:465` — 非 root worker 形态无法在 root 下断言。

**注意**：直接 `docker run` + `pytest tests/unit tests/contract tests/security`（不带 lane）会得到
160 failed / 177 errors —— 全部是缺基础设施（本地 registry 未透传 `E2B_REGISTRY_MIRRORS`、
XFS prjquota 不可用、缺 `SYS_ADMIN/SYS_PTRACE` 导致 `uid` 映射失败）。**基线必须用 lane 跑**，
否则"失败"是环境噪声而不是代码状态。

## 探针运行器

本审计自建的可复用运行器（`deploy/scripts/acceptance/sec-run-probe.sh`）复刻 lane 的能力集，默认挂**部署用**的
seccomp profile（worker 在无过滤器下会拒绝启动：`SECCOMP_FILTER_MISSING`）：

```bash
./deploy/scripts/acceptance/sec-run-probe.sh python tmp/<probe>.py
SECCOMP_PROFILE=unconfined ./deploy/scripts/acceptance/sec-run-probe.sh python -m pytest tests/security/escape -q
```
