# 本地直连远端 SDK 测试

从开发机直接用官方 e2b SDK 连接**远程部署实例**（代理地址
`http://172.18.78.49:3000`）跑 SDK 测试用例，不需要在远端起本地服务、
也不需要 SSH 隧道。

## 机制

测试框架（`tests/conftest.py` 的 `live_servers` fixture）检测到环境变量
`E2B_TEST_PROXY_URL` 时，跳过本地启动 control-plane / envd，直接把
`E2B_API_URL` / `E2B_SANDBOX_URL` / `E2B_VOLUME_API_URL` 指向该地址，
`E2B_API_KEY` / `E2B_INTERNAL_API_KEY` 从环境读取。SDK 测试因此全部
打到远端实例上。

## 前置

- 本机可直连代理 `http://172.18.78.49:3000`（目标机 `172.18.80.140:3000`
  本机**不可直连**，走代理）。
- 本机 Python 环境：`tmp/testenv`（Python 3.14，已装 e2b 2.46.0、pytest、
  httpx 等）。
- 远端 API key 与本地 `deploy/stack/.env` 一致（`upgrade.sh` 会同步），
  直接从中读取。

## 运行

```bash
cd /Users/polus/project/ai/sandlock-e2b

export E2B_TEST_PROXY_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(sed -n 's/^E2B_API_KEYS=//p' deploy/stack/.env | cut -d, -f1)
export E2B_INTERNAL_API_KEY=$(sed -n 's/^E2B_INTERNAL_API_KEY=//p' deploy/stack/.env)

# 核心 SDK 用例（已验证支持远端模式）
tmp/testenv/bin/python -m pytest \
  tests/sdk/python/test_sandbox.py \
  tests/sdk/python/test_commands.py \
  tests/sdk/python/test_files.py \
  tests/sdk/python/test_stdin.py \
  tests/sdk/python/test_pty.py \
  tests/sdk/python/test_features.py \
  -q -p no:cacheprovider
```

健康检查：

```bash
curl -s http://172.18.78.49:3000/
# => {"status":"ok","service":"e2b-sandlock"}
```

## 覆盖范围与限制

| 状态 | 测试文件 | 说明 |
|---|---|---|
| ✅ 支持远端 | `test_sandbox` / `test_commands` / `test_files` / `test_stdin` / `test_pty` / `test_features` | 41 个用例，2026-08-31 全过（45s） |
| ✅ 支持远端 | `test_mcp` | MCP gateway 经代理路由到沙箱内网关，2026-08-31 通过（6s） |
| ❌ 不支持远端 | `test_multinode` / `test_shared_volumes` / `test_templates` | fixture 总是起本地多节点 harness / 本地 registry |

多节点调度、文件迁移、卷隔离、模板构建等远端能力验证请用
`./deploy/scripts/smoke.sh`（在目标机本机跑 `multinode_smoke.py` +
`deployment_smoke.py`）。

## 最近验证记录

- 2026-08-31：代理 `172.18.78.49:3000`，核心套件 41 passed in 45.29s
  （sandbox 生命周期、commands、files、stdin、PTY、features：卷/secret/
  pause-resume/metrics）；`test_mcp` 1 passed in 6.13s（MCP gateway
  工具经代理路由验证）。
