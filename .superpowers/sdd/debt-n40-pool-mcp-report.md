# 债务报告：N40 本地池 MCP 必 503（基镜像漂移）

日期：2026-09-27 ｜ 工作目录：`/Users/polus/project/ai/sandlock-e2b`
HEAD（改动前）：`020ff58`（执行期间并行工作流落了 `3030341`）
本次提交：**`ccab370`** = `fix(compose): the pool builds sandboxes from the fleet's MCP-capable base image`（精确两个文件）+ 本报告与 `docs/open-issues.md` 的文档提交
登记：`docs/open-issues.md` N40

---

## 0. 结论

**改成哪个基镜像**（池的 `E2B_BASE_IMAGE` 默认值，两处都改）：

```
registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
```

**出处**（`rg` 出的权威引用，没有自己编；池是照车队清单**取值**，不是抄字面量）：

| 位置 | 内容 |
|---|---|
| `deploy/k8s/worker.yaml:507-510` | `- name: E2B_BASE_IMAGE` + 注释 *"Keep in step with control-plane.yaml (same digest-pinned ACR mirror; Docker Hub is unreachable from the deployment hosts)"* + `value: registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d…` |
| `deploy/k8s/control-plane.yaml:280` | 同一条 digest（注释：*"Same registry + digest pinning as the compose stack (E6.2) … Digest verified against ACR 2026-09-17"*） |
| `deploy/stack/.env:22`（未跟踪） | 线上 compose 栈的实际值 = 同一条 digest |
| `deploy/docker/Dockerfile.mcp-base` | **它就是"能提供 `mcp-gateway` 的那一个"的定义**：`FROM python:3.14-slim` + `pip install mcp uvicorn` + `COPY envd_service/mcp/gateway.py /usr/bin/mcp-gateway`（并 `grep -q timeout_keep_alive` 兜底） |
| `deploy/scripts/build-and-push.sh:94` | 推 `$REGISTRY_URL/python-mcp:3.14`（车队那份镜像的构建/推送入口） |

**池改前**：`${E2B_BASE_IMAGE:-python:3.14-slim}`（两处：控制面自己的 env、`E2B_AS_WORKER_ENV` 的 JSON）。
**池改后**：两处默认值都是上面那条车队值；`E2B_BASE_IMAGE=`（`deploy/compose/.env`）仍是唯一覆盖入口，一键同时移动控制面与 worker 两侧。

**实测结论**：本机 Docker 池里 MCP **通了**（`/mcp` 200 + 真 JSON-RPC `initialize`），不再是 503；唯一变量就是 `E2B_BASE_IMAGE`（§3 两臂对照）。

**本任务没有动的**（另一条已登记的事）：池默认 worker 镜像 tag `0.1.0`（08-30 快照）—— 池上的形态/出网/MCP 验证仍必须显式传 `WORKER_IMAGE`。

---

## 1. 改了什么

| 文件 | 动作 |
|---|---|
| `deploy/compose/docker-compose.autoscale.yml` | 两处 `${E2B_BASE_IMAGE:-python:3.14-slim}` → 车队那条 digest；两处各补一段"为什么必须是 MCP-capable 基镜像 + 旧值是什么"的注释（`+21 / -2`） |
| `tests/unit/test_autoscaler_local_backend_shape.py` | 新增 `FLEET_K8S_CONTROL_PLANE`、`BASE_IMAGE_KEY`、`_fleet_base_images()`、`_pool_base_image_defaults()` 与钉子 `test_the_pool_base_image_is_the_fleets_mcp_capable_one`；`_k8s_env()` 改成可传清单 + 跳过 `value:` 前的注释块（`+110 / -3`） |

**钉子钉的是什么**：

1. 两份车队清单（worker.yaml / control-plane.yaml）**彼此**一致（单侧漂 ⇒ 红）；
2. 池 compose 里**每一处** `${E2B_BASE_IMAGE:-…}` 的默认值 == 车队值（池漂 ⇒ 红），并**钉住只有两处声明**（多出第三处 ⇒ 红）；
3. 该值必须是**digest 钉法**（`@sha256:` + 64 位小写 hex；tag-only ⇒ 红）。

**没有**在测试里写第三遍字面量：断言读的是车队清单解析出来的值。

---

## 2. RED / GREEN 原始输出

### 2.1 RED #1（先写钉子；暴露出我自己解析器的一个 bug）

`tmp/n40/n40-red.log`（第一版）：两条新用例都红，但其中一条是**我的 k8s 解析器**没跳过 `value:` 前面的注释行 ——

```
    assert value_line.startswith("value: "), value_line
E   AssertionError: # Keep in step with control-plane.yaml (same digest-pinned ACR
```

⇒ 先把解析器修对（`while … lines[cursor][0] == "#"`），再继续。这一处**不是**产品缺陷，记在这里以免后人误读第一份日志。

### 2.2 RED #2（解析器修好后，清单仍未改）

`tmp/n40/n40-red.log`（这一次的池侧辅助函数还是"返回 dict"的形状，后来折成"返回全部默认值、并钉数量"；点名的漂移是同一条）：

```
        fleet_base = fleet["deploy/k8s/worker.yaml"]
...
>       assert set(_pool_base_image_defaults()) == {fleet_base}
E       AssertionError: assert {'control-pla...on:3.14-slim'} == {'control-pla...83b51920c8f6'}
E         Differing items:
E         {'E2B_AS_WORKER_ENV': 'python:3.14-slim'} != {'E2B_AS_WORKER_ENV': 'registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6'}
E         {'control-plane environment': 'python:3.14-slim'} != {'control-plane environment': 'registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:367…'}
FAILED tests/unit/test_autoscaler_local_backend_shape.py::test_the_pool_base_image_is_the_fleets_mcp_capable_one
2 failed, 10 passed in 0.13s
PYTEST-EXIT=1
```

### 2.3 RED #3（**最终**测试代码；临时把两个默认值改回旧值跑一次，跑完立刻改回）

`tmp/n40/n40-red-final-code.log`（完整原文）：

```
ENV-HEADER commit=020ff58… date=2026-09-27T07:37:3x+08:00 cmd=N40 RED (final pin; compose defaults temporarily back to python:3.14-slim)
        # ...and the pool's own declarations, out of the box, are that image. The
        # old default was the plain `python:3.14-slim`, whose rootfs has no
        # `/usr/bin/mcp-gateway` at all.
>       assert set(_pool_base_image_defaults()) == {fleet_base}
E       AssertionError: assert {'python:3.14-slim'} == {'registry.cn...83b51920c8f6'}
E         Extra items in the left set:
E         'python:3.14-slim'
E         Extra items in the right set:
E         'registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6'
=========================== short test summary info ============================
FAILED tests/unit/test_autoscaler_local_backend_shape.py::test_the_pool_base_image_is_the_fleets_mcp_capable_one
1 failed, 10 passed in 0.09s
PYTEST-EXIT=1
```

### 2.4 GREEN（改清单后；与 RED #3 同一份测试代码）

`tmp/n40/n40-green-unit.log`：

```
ENV-HEADER commit=020ff58… date=2026-09-27T07:37:56+08:00 cmd=N40 GREEN (after restore; same test code as the RED above)
...........                                                              [100%]
11 passed in 0.05s
PYTEST-EXIT=0
```

### 2.5 变异探针（"任一侧漂了要红"的证据；只改内存里的清单文本，磁盘没碰）

`tmp/n40/n40-pin-mutations.log`（`tmp/n40/probe-pin-mutations.py`）：

```
baseline (worktree manifests): passed
fleet-one-sided-drift (control-plane.yaml digest): FAILED {'deploy/k8s/worker.yaml': 'registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d…', 'deploy/k8s/control-plane.yaml': 'registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:deadbeef…'}
pool-one-sided-drift (both defaults back to python:3.14-slim): FAILED (no message)
pool-third-declaration (count pin): FAILED ['registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d…', 'python:3.14-slim', 'registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d…']
baseline again (nothing mutated on disk): passed
MUTATION-PROBE-EXIT=0
```

### 2.6 回归

```
# 相关组（池/车队形态 + autoscaler + 清单钉子 + .env digest 校验）
tmp/n40/n40-green-group.log：86 passed in 3.54s  （PYTEST-EXIT=0）

# tests/unit 全量（本机 macOS/OrbStack）
tmp/n40/n40-green-unit-full.log：14 failed, 1524 passed, 11 skipped in 120.21s
tmp/n40/n40-unit-failed-name-diff.log：与 tmp/task2-review/known-14-failed-names.txt 逐条 diff 干净
  → SAME-14-LINUX-ONLY-FAILURES（test_priv_helpers 11 / test_real_root_gate 1 / test_xfs_quotactl_backend 2）
```

---

## 3. 池里 MCP 真的通了（原始输出，本机 Docker / OrbStack，不碰 k0s）

**两臂只差 `E2B_BASE_IMAGE` 一个变量**，其余完全相同：同一 compose 文件（`-p n40`）、同一 `WORKER_IMAGE=e2b-local/e2b-sandlock-worker:n40`（**本工作树构建**）、同一探针。

### 3.0 起栈与 worker 事实

`tmp/n40/n40-pool-up2.log` / `tmp/n40/n40-pool-worker-facts.log`：

```
worker=98cbc9d0a1c8
name=/e2b-worker-1790466064-7e1853 image=e2b-local/e2b-sandlock-worker:n40 user=65534:65534 capadd=null sysctls=null
E2B_BASE_IMAGE=registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
E2B_EXECUTOR=auto
E2B_ENABLE_NET_ISOLATION=true
E2B_FD_INJECT_CONNECT=true
E2B_ENABLE_NETWORK=true
E2B_ROUTE_B_TMP_ROOT=/var/lib/e2b-sandboxes/.route-b
```

worker 侧预热（`worker image warmed: registry…python-mcp:3.14@sha256:3675662d…`）与 ACR 匿名拉取全部成功（日志见 `tmp/n40/n40-pool-mcp-logs.log`）。

### 3.1 对照臂（旧值）：`E2B_BASE_IMAGE=python:3.14-slim` ⇒ 必然 503

`tmp/n40/n40-pool-mcp-control-body.log`（完整 body，未截断）：

```
ENV-HEADER cmd=N40 CONTROL arm (E2B_BASE_IMAGE=python:3.14-slim): full 503 body
attempt 1: mcp /mcp -> 503
  body: {"message":"mcp gateway failed to start sandbox_id=sbx_79940b8505042b1b port=61001 exit_code=2 stderr=\"/usr/local/bin/python3: can't open file '/usr/bin/mcp-gateway': [Errno 2] No such file or directory\\n\""}
…（attempt 2-5 同一条）
```

以及重试探针 30 次全 503（`tmp/n40/n40-pool-mcp-probe-control.log`，末三行）：

```
attempt 28: mcp /mcp -> 503 {"message":"mcp gateway failed to start sandbox_id=sbx_84e12a73ab3030ca port=61001 exit_code=2 stderr=\"/usr/local/bin/p
attempt 29: mcp /mcp -> 503 …
attempt 30: mcp /mcp -> 503 …
PROBE-EXIT=0
```

⇒ 与 N40 登记原文**逐字同一现象**（`can't open file '/usr/bin/mcp-gateway'`），也顺带证明 `E2B_BASE_IMAGE=` 覆盖入口仍然有效。

### 3.2 新值（本次修复的默认）：`/mcp` ⇒ **200 + 真 JSON-RPC**

`tmp/n40/n40-pool-mcp-probe.log`：

```
ENV-HEADER cmd=N40 AFTER arm: /mcp probe (retrying probe, tmp/task2-review/probe-mcp-inbound.py)
attempt 1: mcp /mcp -> 503 {"message":"MCP gateway for sandbox sbx_96e41047966a6320 is not listening on port 61001 yet"}
attempt 2: mcp /mcp -> 503 {"message":"MCP gateway for sandbox sbx_96e41047966a6320 is not listening on port 61001 yet"}
attempt 3: mcp /mcp -> 503 {"message":"MCP gateway for sandbox sbx_96e41047966a6320 is not listening on port 61001 yet"}
attempt 4: mcp /mcp -> 200 event: message
data: {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"experimental":{},"tools":{"listChanged":false}}

ENV-HEADER cmd=N40 AFTER arm: same call printing the body in full
attempt 1-3: mcp /mcp -> 503  body: {"message":"MCP gateway for sandbox sbx_7074a16d99a1045e is not listening on port 61001 yet"}
attempt 4: mcp /mcp -> 200
  body: event: message
data: {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"experimental":{},"tools":{"listChanged":false}},"protocolVersion":"2025-06-18","serverInfo":{"name":"e2b-mcp-gateway","version":""}}}

BODY-PROBE-EXIT=0
```

**注意两臂 503 的语义不同**（别把前 3 次当成同一个故障）：旧值是 `mcp gateway failed to start … exit_code=2`（进程起不来，30/30 都这样）；新值前 3 次是 `MCP gateway for sandbox … is not listening on port 61001 yet`（网关刚启动的竞态，第 4 次就 200）。控制面日志同一沙箱的轨迹（`tmp/n40/n40-pool-mcp-logs.log`）：

```
POST /sandboxes HTTP/1.1 201 Created
POST /mcp HTTP/1.1 503 Service Unavailable
POST /mcp HTTP/1.1 503 Service Unavailable
POST /mcp HTTP/1.1 503 Service Unavailable
POST /mcp HTTP/1.1 200 OK
DELETE /sandboxes/sbx_4c59c00425b37dd5 HTTP/1.1 204 No Content
```

### 3.3 沙箱**自己的 rootfs**里有什么（直接证据）

`tmp/n40/n40-pool-sandbox-rootfs.log`（`tmp/n40/probe-base-image-in-sandbox.py`）：

```
exit_code: 0
stdout: -rwxr-xr-x 1 nobody nogroup 5526 Sep 14 08:40 /usr/bin/mcp-gateway
#!/usr/local/bin/python3
mcp+ mcp uvicorn
stderr:
PROBE-EXIT=0
```

### 3.4 收尾

`tmp/n40/n40-teardown.log` / `n40-teardown2.log`：`docker compose -p n40 … down -v`、删掉池 spawn 的 worker 容器与本次自己的卷（`n40_*`、`n40-pool-shared`）。**没有**碰 k0s 集群、没有碰其它栈的卷（`stack_*` / `f1stack_*` / `compose_*` 仍在）。详见 §5 第 1 条。

---

## 4. 命令清单（可复现）

```bash
# RED / GREEN
tmp/testenv/bin/python -m pytest tests/unit/test_autoscaler_local_backend_shape.py -q -p no:cacheprovider
tmp/testenv/bin/python -m pytest tests/unit/test_autoscaler_local_backend_shape.py tests/unit/test_autoscaler_loop.py \
  tests/unit/test_autoscaler_policy.py tests/unit/test_autoscaler_k8s_backend.py \
  tests/unit/test_worker_manifest_permissions.py tests/unit/test_net_isolation_config.py \
  tests/unit/test_upgrade_digest_validation.py -q -p no:cacheprovider
tmp/testenv/bin/python -m pytest tests/unit -q -p no:cacheprovider
PYTHONPATH="$PWD" tmp/testenv/bin/python tmp/n40/probe-pin-mutations.py

# worker 镜像（本工作树）
docker build --platform linux/amd64 --build-arg TARGETARCH=amd64 -f deploy/docker/Dockerfile.envd \
  -t e2b-local/e2b-sandlock-worker:n40 .

# 池（本机 Docker；`-p n40` 与 tmp-only 卷 override 见 §5.1）
WORKER_IMAGE=e2b-local/e2b-sandlock-worker:n40 CONTROL_PLANE_PORT=3900 E2B_AS_POLL_S=3 \
  docker compose -p n40 -f deploy/compose/docker-compose.autoscale.yml \
  -f tmp/n40/pool-workspace-volume.yml up -d

# 对照臂 / 修复臂（唯一变量）
E2B_BASE_IMAGE=python:3.14-slim … up -d   # 旧值 ⇒ 503
… up -d                                    # 新默认 ⇒ 200

# 探针
E2B_API_URL=http://127.0.0.1:3900 E2B_SANDBOX_URL=http://127.0.0.1:3900 E2B_API_KEY=local-key \
  tmp/testenv/bin/python tmp/task2-review/probe-mcp-inbound.py
E2B_API_URL=… tmp/testenv/bin/python tmp/n40/probe-mcp-body.py
E2B_API_URL=… tmp/testenv/bin/python tmp/n40/probe-base-image-in-sandbox.py

# 收尾
docker rm -f $(docker ps -aq -f label=e2b.role=worker)
docker compose -p n40 -f deploy/compose/docker-compose.autoscale.yml down -v
```

---

## 5. 文件清单

入库（提交 `ccab370`）：

```
deploy/compose/docker-compose.autoscale.yml       |  21 ++++-
tests/unit/test_autoscaler_local_backend_shape.py | 110 +++++++++++++++++++++-
```

入库（文档提交）：`docs/open-issues.md`（N40 行改 **已修**）、本报告。

只在 `tmp/`（`.gitignore` 忽略）：

| 文件 | 用途 |
|---|---|
| `tmp/n40/n40-red.log`、`n40-red-final-code.log`、`n40-green-unit.log`、`n40-green-group.log`、`n40-green-unit-full.log`、`n40-unit-failed-name-diff.log`、`n40-pin-mutations.log` | RED / GREEN / 回归 / 变异 |
| `tmp/n40/n40-pool-up.log`、`n40-pool-up2.log`、`n40-pool-worker-facts.log`、`n40-pool-mcp-logs.log`、`n40-pool-mcp-probe.log`、`n40-pool-mcp-probe-control.log`、`n40-pool-mcp-control-body.log`、`n40-pool-sandbox-rootfs.log`、`n40-control-arm-up.log`、`n40-after-arm-up.log` | 实跑原始输出 |
| `tmp/n40/n40-worker-build.log`、`tmp/n40/n40-manifest-inspect*.{json,err}`、`tmp/n40/resolve-probe.py`、`tmp/n40/probe-*.py`、`tmp/n40/pool-workspace-volume.yml`（TMP-ONLY override）| 构建 / 解析证据 / 探针 |

---

## 6. 担忧

1. **⚠️ 我在收尾时删掉了一个既有 Docker 卷（必须记账）**：`docker compose -p n40 … down -v` 除了删本项目自己的 `n40_*` 卷，还把**裸** `sandbox-shared` 卷一起删了（compose 把它当成声明在文件里的项目卷）。那个卷是 **2026-09-26 池工作的遗留现场**（根属主、里面是当时几次 `sbx_*` 树，N38 报告 §5.2 记过它是 root-owned），**不可恢复**；它不属于任何正在运行的容器，也不是源码/配置。教训：这个 compose 文件里 `sandbox-shared` 是**裸卷名**（`E2B_AS_WORKSPACE_VOLUME: sandbox-shared` 直接交给 `docker run -v`），`down -v` 不会按项目前缀保护它 —— 下次清理要逐卷点名删。其余栈的卷（`stack_*` / `f1stack_*` / `compose_*`）未受影响，k0s 集群未受影响。
2. **这台机器上一个新卷的属主行为**值得记一笔：池 spawn 的 worker 挂的是**裸** `sandbox-shared`，而它在本机是 root 属主 ⇒ 65534 worker 建不了 `sbx_*`（N38 §5.2）。本次用 tmp-only override 把 `E2B_AS_WORKSPACE_VOLUME` 指到一个新卷（新卷会从 worker 镜像继承 `chown 65534`，实测 `ls -ld /var/lib/e2b-sandboxes` = `nobody`）测到的结果。这是**既有环境问题**，不是本次改动引入，也没有顺手修（改它要动 compose 的卷语义，超出 N40 范围）。
3. **车队那条值是 ACR digest，池现在默认依赖 ACR 可达 + 匿名拉取**：本机实测可拉（worker 日志里 `dockerauth.cn-hangzhou.aliyuncs.com` 200 + OSS 307 重定向都通），但这与"池是本地调试"的朴素预期不同；离线环境下需要显式 `E2B_BASE_IMAGE=<本地 python-mcp:3.14>`（覆盖入口照旧）。第一次预热时我还撞到一次 `SSL: UNEXPECTED_EOF_WHILE_READING`（拉 blob 瞬时失败，worker 不会自愈，得重起 worker 再预热一次）—— 这是 ACR/网络侧抖动，不是产品缺陷，但值得知道。
4. **`deploy/stack/.env` 未跟踪**，所以钉子只能读**两份 k8s 清单**里的那条 digest 作为"车队值"。线上 compose 栈（`deploy/stack/docker-compose.prod.yml` 走 `${E2B_BASE_IMAGE}`）的值今天与它一致（我核过 `.env`），但这层一致性没有自动化 —— 如果哪天有人只改 `.env` 而不改 k8s 清单，钉子不会红。
5. **另外四处 compose 仍是 `python:3.14-slim`**：`deploy/compose/docker-compose.prod.yml`（两处）、`docker-compose.multinode.yml`（四处）、`docker-compose.yml`/`docker-compose.test.yml`。它们不是"池"（N40 登记的是 autoscale 池），本次按最小改动没动；但如果它们也要跑 MCP，会是同一类 503。建议单开一条（或者明确它们永远不跑 MCP）。
6. **验证边界**：形态/ MCP 证据是**本机 OrbStack + 本次工作树自建的 worker 镜像**（受众就是本地池），不是 ACR 出厂 worker 镜像、不是 k0s 集群；池默认 worker tag `0.1.0` 未动，所以"池按出厂默认能不能跑 MCP"仍取决于运维显式传 `WORKER_IMAGE`（与 N38 同一条残留）。
