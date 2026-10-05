# c3-agent 后续：compose 同步、启动自检、default-deny、集群复验

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把上一轮明确推迟和没做的部分做完。

**Architecture:** 三块互相独立，各自可单独上线，故分成 A/B/C 三部分。

**Tech Stack:** docker compose、pytest、Kubernetes NetworkPolicy、`deploy/scripts/` 的集群门禁。

**Spec:** `docs/security-audit/c3-agent-syscall-filter-2026-10-05.md`（上一轮的实测记录；A2 是它 §4 第 1 条的自动化）+ `docs/security-audit/remediation-SEC-R3-01.md` §8.6。

## Global Constraints

- `c3-agent.yaml`（k8s）的文本禁令不变：`SYS_ADMIN`/`SYS_PTRACE`/`NET_RAW`/`privileged`/`hostNetwork`/`allowPrivilegeEscalation`/`no-new-privileges` 一个都不能出现，注释里也不能。
- `c3_agent` 的运行镜像只含 `c3_agent` + `gateway_common`（`deploy/docker/Dockerfile.agent`）。**不得** import `envd_service` 或 sandlock wheel。
- agent 的 seccomp 档在 compose 里**不是**显式项：实测 `docker run --security-opt seccomp=default` 报 `open default: no such file or directory`（Docker 29.4.0），不接受该写法。compose 侧"默认档"由**不写 `security_opt`** 表达。
- 只读根、启动自检都不得破坏现有绿基线：`python -m pytest tests/unit -q` 的失败集合必须保持是那 3 个平台项。

---

## Part A：仓库侧（不碰集群）

### Task A1: compose 三个形态同步只读根，并钉住 seccomp 默认档

**Files:**
- Modify: `deploy/compose/docker-compose.multinode.yml`（`c3-agent`、`c3-agent-maint`）
- Modify: `deploy/compose/docker-compose.prod.yml`（同上两个）
- Modify: `deploy/stack/docker-compose.prod.yml`（同上两个）
- Test: `tests/unit/test_c3_agent_manifest.py`

**Interfaces:**
- Consumes: 无
- Produces: 不变量「三个形态的每个 agent 服务 `read_only is True`，且不含 `seccomp=unconfined`」；Task A3 的渲染 pin 建立在同一份文件清单上。

- [ ] **Step 1: 写失败用例**

```python
def test_every_compose_agent_runs_read_only_under_the_default_profile() -> None:
    """k8s 侧的 `readOnlyRootFilesystem` + `seccompProfile: RuntimeDefault` 在
    compose 里的对应物。

    seccomp 那一半是**负**断言：compose 没有"默认档"的写法（实测
    `--security-opt seccomp=default` 被 Docker 29.4.0 拒绝），默认档由**不写
    `security_opt`** 表达 —— 所以这里钉的是"没人给它加 unconfined"。
    """
    for path in AGENT_COMPOSE:
        services = _compose(path)["services"]
        for name in ("c3-agent", "c3-agent-maint"):
            service = services[name]
            assert service.get("read_only") is True, (path.name, name)
            opts = service.get("security_opt") or []
            assert not any("seccomp" in opt for opt in opts), (path.name, name, opts)
```

`AGENT_COMPOSE` 是那三份文件的列表（模块级常量，与已有 `_compose` helper 同风格）。

- [ ] **Step 2: 跑，确认失败**

Run: `python -m pytest tests/unit/test_c3_agent_manifest.py::test_every_compose_agent_runs_read_only_under_the_default_profile -q`
Expected: FAIL（`read_only` 缺失 → `assert None is True`）

- [ ] **Step 3: 六个服务各加一行**

在 `c3-agent` 与 `c3-agent-maint` 的 `cap_add:` 块之后（`environment:` 之前）：

```yaml
    # The k8s shape's `readOnlyRootFilesystem`; nothing in this image writes to
    # its own rootfs (see docs/security-audit/c3-agent-syscall-filter-2026-10-05.md §2b).
    read_only: true
```

- [ ] **Step 4: 跑整档**

Run: `python -m pytest tests/unit/test_c3_agent_manifest.py -q`
Expected: PASS

- [ ] **Step 5: 实测只读根下两个 face 都起得来**

用 `--read-only` 起 face A 与 face B，各探一次自己的端口
（49985 / 49986），Expected: 两条都 `SERVICE-OK`。

- [ ] **Step 6: commit**

### Task A2: agent 的启动自检 —— 档在不在、NNP 有没有

**Files:**
- Modify: `c3_agent/config.py`（新增 `require_filter` 设置）
- Modify: `c3_agent/__main__.py`（`_startup_error` 增加两条具名拒绝）
- Test: `tests/unit/test_c3_agent_service.py`

**Interfaces:**
- Consumes: 无
- Produces: `_startup_error(settings, *, status_text: str | None = None) -> str | None`（`status_text` 可注入，与 `envd_service/config.py::check_seccomp_filter` 同形）

- [ ] **Step 1: 写失败用例**（5 条：无 /proc 跳过；Seccomp:0 拒；NoNewPrivs:1 拒；两者正常放行；`require_filter=False` 降级）
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现**：`c3_agent/config.py` 加 `require_filter: bool = field(default_factory=lambda: os.getenv("E2B_C3_AGENT_REQUIRE_FILTER", "1") not in ("0","false","no"))`；`c3_agent/__main__.py` 读 `/proc/self/status`（读不到就 debug 跳过，macOS 开发机）
- [ ] **Step 4: 跑整档**
- [ ] **Step 5: 实测**：`docker run` 起服务，默认档下应正常起；加 `--security-opt no-new-privileges` 应被这两条拒掉（NNP 那条），错误文本精确
- [ ] **Step 6: commit**

### Task A3: 上一轮推迟的四个小项

**Files:**
- Modify: `docs/security-audit/c3-agent-syscall-filter-2026-10-05.md`（口径笔误 + 注释去重对应的说明）
- Modify: `deploy/k8s/c3-agent.yaml`（把 `seccompProfile` 的解释收敛到 face A，其余三处留中性一句）
- Test: `tests/unit/test_c3_agent_manifest.py`（新增**渲染产物** pin）

- [ ] **Step 1**: 文档 §0 的"三条"→"四条"（STATIC-6 五条里落地四条，只有 hostPID 没落）
- [ ] **Step 2**: 三个非 face A 容器的注释改成中性一句，机制解释只留 face A
- [ ] **Step 3**: 新增渲染 pin —— `kubectl kustomize deploy/k8s-k0s` 的输出里每个 agent 容器仍有 `seccompProfile: RuntimeDefault` 与 `readOnlyRootFilesystem: true`（覆盖"将来 overlay 长出 patch"那个口子）
- [ ] **Step 4**: 跑整档 + commit

---

## Part B：namespace 级 default-deny

### Task B1: 一条 default-deny + 逐条具名放行

**Files:**
- Create: `deploy/k8s/default-deny.yaml`
- Modify: `deploy/k8s/kustomization.yaml`
- Test: `tests/unit/test_network_default_deny.py`

**Interfaces:**
- Consumes: Part A 的渲染 pin 方法
- Produces: namespace `sandlock` 的默认拒绝，叠加在各 workload 现有的具名策略之上

- [ ] **Step 1: 先只读核对真实流量**（`KUBECONFIG` + 隧道自检），把清单落进计划：CP↔redis、CP→buildkit(sock)、gateway 入站(:3000)、worker→CP、agent↔CP(49985/49986/3000)、all→kube-dns(53)
- [ ] **Step 2: 写失败用例**（放行项逐条断言；default-deny 与既有 `e2b-c3-agent` 策略并存）
- [ ] **Step 3: 写策略 + 注册进 kustomization**
- [ ] **Step 4: `kubectl kustomize` 渲染 + 整档**
- [ ] **Step 5: `kubectl --dry-run=server`**（只 dry-run，不 apply）
- [ ] **Step 6: commit**

---

## Part C：集群侧（需要明确放行才做）

### Task C1: 上节点复验
- `/proc/<agent-pod>/status` 的 `Seccomp` / `NoNewPrivs`
- 一次真实授予（或在 pod 内跑 A2 的自检）—— 它是 §4 第 1 条，A2 落地后由启动自检自动覆盖

### Task C2: 部署
- 构建 + 推送镜像（含 `remediation-SEC-R3-01.md` §8.6 里未上线的 SEC-R3-01 / STATIC-5 修复）
- `deploy/k8s-k0s/apply.sh`（自带集群身份闸门）

**C 是活集群上的 rollout，且要动 ACR 凭据与版本号。做完 A、B 后单独确认再动。**
