# c3-agent syscall 过滤与 pod 卫生 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `e2b-c3-agent`（这个节点上唯一的 root 组件）补上 syscall 过滤与 pod 卫生，且不破坏 face A 的 file-capability 身份授予。

**Architecture:** 用 `seccompProfile: RuntimeDefault`，不用自建 Localhost 档。实测（Task 3 的记录）证明运行时的默认档足以让 face A 的授予路径（`setgroups` → `setresgid` → `setresuid`）走通 ⇒ 不需要 `seccomp-installer` 往节点铺第二份 profile、不需要新档文件、不需要新的 apply 顺序。卫生字段里唯一**不能**加的是 `allowPrivilegeEscalation`：它令 NNP=1，内核从此忽略 `as_uid` 的 file capabilities，授予静默失败。

**Tech Stack:** Kubernetes manifest（`deploy/k8s/c3-agent.yaml`）、pytest（`tests/unit/test_c3_agent_manifest.py`）、Docker 实测。

**Spec:** `docs/security-audit/c3-agent-syscall-filter-2026-10-05.md`（本计划 Task 3 产出，同时是 Task 1/2 全部判据的来源）。上游缺口账见 `docs/security-audit/remediation-SEC-R3-01.md` §8.6 的 STATIC-6。

## Global Constraints

- `tests/unit/test_c3_agent_manifest.py::test_the_agent_manifest_never_names_a_forbidden_privilege` 钉死：`c3-agent.yaml` 的**文本**里不得出现 `SYS_ADMIN`、`SYS_PTRACE`、`NET_RAW`、`privileged`、`hostNetwork`、`allowPrivilegeEscalation`、`no-new-privileges`。**注释里也不行**，中文注释里也不得夹带这七个英文词。
- face A 的 `securityContext` 保持 `runAsUser: 65534` + `capabilities: {add: [SETUID, SETGID]}`；`allowPrivilegeEscalation` 不得出现。
- face B 与两个 init 容器的 capability 集逐字保持 `{drop: [ALL], add: [CHOWN, DAC_OVERRIDE, FOWNER]}`。
- 只改 `deploy/k8s/c3-agent.yaml`、`tests/unit/test_c3_agent_manifest.py`，新增一个 `docs/security-audit/` 记录。不碰 worker、seccomp-installer、NetworkPolicy。
- 基线：`python3 -m pytest tests/unit/test_c3_agent_manifest.py -q` 当前全绿。

## Review Focus

按"最可能咬人"排序，每条在它所属任务的步骤里都有对应的测试或验证：

1. **备注里混进禁用词** —— 文本级 pin 会因为中文注释里夹着一个英文词而变红，这是最容易犯的错。
2. **`seccompProfile` 写错层级** —— 只写在 pod 级或只写在某一个容器上，会让其余容器仍然不过滤；判据必须逐容器断言。
3. **`readOnlyRootFilesystem: true` 打断运行** —— Python 无处写 `__pycache__`、uvicorn 无处落临时文件。本机实测能起，但换运行时可能翻车，所以要有"服务真的绑定成功"的验证，不是只看 manifest。
4. **默认档不够用** —— `RuntimeDefault` 是运行时的档，本机用 Docker 的默认档代表它；containerd 那份是另一实现。差异会让 face A 静默授不出身份，所以 grant 路径必须实测，不能推理。
5. **`automountServiceAccountToken: false` 切断了在用的路径** —— 需要确认 agent 不用 API server（identity 来自 `spec.nodeName` 的 env，不是 API 查询）。

---

### Task 1: 四个容器都上 syscall 过滤

**Files:**
- Modify: `deploy/k8s/c3-agent.yaml`（`agent`、`maint`、`storage-init`、`workspace-root-init` 四个 `securityContext`）
- Test: `tests/unit/test_c3_agent_manifest.py`（新增用例）

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces: 不变量「`c3-agent.yaml` 每个容器的 `securityContext.seccompProfile == {"type": "RuntimeDefault"}`」。Task 2 用同一个 `_pod_containers()` helper 逐容器断言。

- [ ] **Step 1: 写失败用例**

追加到 `tests/unit/test_c3_agent_manifest.py` 末尾（沿用文件里已有的 `_load_all` / `_only` / `_pod_containers`）：

```python
def test_every_agent_container_runs_under_a_syscall_filter() -> None:
    """STATIC-6 的第一条：全舰队唯一的 root 组件此前一个 syscall 都不拦。

    `RuntimeDefault` 而不是 Localhost：运行时的默认档已经允许 face A 的授予路径
    （`setgroups`/`setresgid`/`setresuid`，本机实测通过），所以不需要
    `seccomp-installer` 往节点上再铺一份 profile。
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    containers = _pod_containers(agent)
    assert sorted(containers) == [
        "agent",
        "maint",
        "storage-init",
        "workspace-root-init",
    ]
    for name, container in containers.items():
        assert container["securityContext"]["seccompProfile"] == {
            "type": "RuntimeDefault"
        }, name
```

- [ ] **Step 2: 跑，确认失败**

Run: `python3 -m pytest tests/unit/test_c3_agent_manifest.py::test_every_agent_container_runs_under_a_syscall_filter -q`
Expected: FAIL — `KeyError: 'seccompProfile'`

- [ ] **Step 3: 在四个 `securityContext` 块末尾各加两行**

`deploy/k8s/c3-agent.yaml`，`storage-init`、`workspace-root-init`、`agent`、`maint` 各一处：

```yaml
            seccompProfile:
              type: RuntimeDefault
```

缩进与同块内的 `runAsUser:` 对齐。不要为此写带禁用词的注释。

- [ ] **Step 4: 跑整档，确认通过**

Run: `python3 -m pytest tests/unit/test_c3_agent_manifest.py -q`
Expected: PASS（含 `test_the_agent_manifest_never_names_a_forbidden_privilege`）

- [ ] **Step 5: 实测：默认档下 face A 的授予路径仍然通**

```bash
IMG=e2b-sandlock-agent:c3-task1-test
docker volume create c3cap-verify >/dev/null
docker run --rm --user 0:0 -v c3cap-verify:/p --entrypoint /bin/sh "$IMG" -c \
  'cp "$(readlink -f /usr/local/bin/python3)" /p/py && chmod 0755 /p/py && setcap cap_setuid,cap_setgid+ep /p/py && chmod 0755 /p'
docker run --rm --user 65534:65534 --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  -v c3cap-verify:/p --entrypoint /p/py "$IMG" -c '
import os
os.setgroups([])
os.setresgid(10001, 10001, 10001)
os.setresuid(10001, 10001, 10001)
print("GRANT-PATH-OK", os.getuid(), os.getgid(), os.getgroups())'
docker volume rm c3cap-verify >/dev/null
```

Expected: `GRANT-PATH-OK 10001 10001 []`

再跑一次**生产形态**的对照 —— 注意 entrypoint 必须是**未加 cap** 的 `/bin/sh`，由它去 exec
那个 file-capped 的二进制：容器 PID 1 自己那次 exec 拿到 cap 不算数，NNP 是**在第一次 exec
之后**才落到进程上的，只有"后续 exec"才反映生产路径（`python3 -m c3_agent` 后来 exec
`as_uid`）。

```bash
PY='import os
os.setgroups([]); os.setresgid(10001,10001,10001); os.setresuid(10001,10001,10001)
print("GRANT-PATH-OK", os.getuid(), os.getgid(), os.getgroups())'
docker run --rm --user 65534:65534 --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  -v c3cap-verify:/p --entrypoint /bin/sh "$IMG" -c "/p/py -c '$PY'"
docker run --rm --security-opt no-new-privileges --user 65534:65534 \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  -v c3cap-verify:/p --entrypoint /bin/sh "$IMG" -c "/p/py -c '$PY'"
```

Expected: 第一条 `GRANT-PATH-OK 10001 10001 []`；第二条
`PermissionError: [Errno 1] Operation not permitted` —— 那是本计划**不做**
`allowPrivilegeEscalation` 的实测依据（不只是推理），Task 3 把它记进文档。

- [ ] **Step 6: commit**

```bash
git add deploy/k8s/c3-agent.yaml tests/unit/test_c3_agent_manifest.py
git commit -m "fix(c3-agent): every container now runs under the runtime syscall filter"
```

---

### Task 2: pod 卫生 —— 无 API 凭据、无可写根、face A 的 gid 显式化

**Files:**
- Modify: `deploy/k8s/c3-agent.yaml`（pod spec + 四个 `securityContext`）
- Test: `tests/unit/test_c3_agent_manifest.py`（新增用例）

**Interfaces:**
- Consumes: Task 1 的 `seccompProfile` 不变量（同一批容器，同一 helper）
- Produces: 不变量「pod 的 `automountServiceAccountToken is False`；四个容器 `readOnlyRootFilesystem is True`；face A `runAsGroup == 65534`」

- [ ] **Step 1: 写失败用例**

```python
def test_the_agent_pod_carries_no_api_credential_and_no_writable_root() -> None:
    """STATIC-6 剩下的三条：没有 SA token、没有可写根、face A 的 gid 是显式的。

    identity 来自 `spec.nodeName` 的 env（D12），不是 API 查询，所以 agent 不需要
    挂载任何 service account 凭据 —— 被拿下的 agent 进程应当连 API server 都敲不到。

    face A 的 `runAsGroup` 此前没有声明，落到了运行时的默认值上；而 compose 那侧一直
    是 `65534:65534`，`/var/lib/e2b-priv` 也是 `0710 root:65534`（只有那一个组位能进
    去）。把它写成显式值，两个发行形态才是同一个口径。
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    pod = _pod_spec(agent)
    assert pod["automountServiceAccountToken"] is False
    for name, container in _pod_containers(agent).items():
        assert container["securityContext"]["readOnlyRootFilesystem"] is True, name
    assert _containers(agent)["agent"]["securityContext"]["runAsGroup"] == 65534
```

- [ ] **Step 2: 跑，确认失败**

Run: `python3 -m pytest tests/unit/test_c3_agent_manifest.py::test_the_agent_pod_carries_no_api_credential_and_no_writable_root -q`
Expected: FAIL — `KeyError: 'automountServiceAccountToken'`

- [ ] **Step 3: 加字段**

`deploy/k8s/c3-agent.yaml` 的 pod spec（与 `hostPID: true` 同级）加：

```yaml
      automountServiceAccountToken: false
```

四个容器的 `securityContext` 里各加（与 `runAsUser:` 同级）：

```yaml
            readOnlyRootFilesystem: true
```

face A（`agent`）的 `securityContext` 里再加：

```yaml
            runAsGroup: 65534
```

- [ ] **Step 4: 跑整档**

Run: `python3 -m pytest tests/unit/test_c3_agent_manifest.py -q`
Expected: PASS

- [ ] **Step 5: 实测：只读根下服务真的起得来**

```bash
IMG=e2b-sandlock-agent:c3-task1-test
docker run --rm -d --name agentro --user 65534:65534 --cap-drop ALL --read-only \
  -e E2B_C3_AGENT_NODE_ID=probe -e E2B_C3_AGENT_TOKEN=t "$IMG" >/dev/null
sleep 4
docker logs agentro 2>&1 | tail -3
docker run --rm --entrypoint python3 --network container:agentro "$IMG" -c \
  "import socket; s=socket.create_connection(('127.0.0.1',49985),2); print('SERVICE-OK', s.getpeername())"
docker rm -f agentro >/dev/null
```

Expected: 日志出现 `Application startup complete.` 与 `Uvicorn running on http://0.0.0.0:49985`，探针打印 `SERVICE-OK ('127.0.0.1', 49985)`。

若失败并报写盘错误，就为 `/tmp` 挂一个 `emptyDir`（`medium: Memory`）—— 那是本步骤允许的修正，其余修正视为"代码错"，走 `superpowers:systematic-debugging`。

- [ ] **Step 6: commit**

```bash
git add deploy/k8s/c3-agent.yaml tests/unit/test_c3_agent_manifest.py
git commit -m "fix(c3-agent): no API credential, no writable container root"
```

---

### Task 3: 把实测结论记进 security-audit

**Files:**
- Create: `docs/security-audit/c3-agent-syscall-filter-2026-10-05.md`
- 不改 `docs/security-audit/security-framework.md`（有人正在改它，避免撞车）

**Interfaces:**
- Consumes: Task 1 Step 5 与 Task 2 Step 5 的真实输出
- Produces: STATIC-6 的收口记录；containerd `RuntimeDefault` 与 Docker 默认档差异的待办

- [ ] **Step 1: 写记录**

内容必须包含（全部来自本会话实测，注明口径）：

1. **测量矩阵**（本机 Docker 29.4.0 / OrbStack kernel `7.0.14` x86_64，镜像 `e2b-sandlock-agent:c3-task1-test`，uid 65534、`cap-drop ALL`、子进程 exec file-capped 二进制）：

   | 条件 | Seccomp | NoNewPrivs | CapEff |
   |---|---|---|---|
   | Docker 默认档 | 2 | 0 | `0000000000000080` |
   | `sandlock-worker.json` | 2 | 0 | `0000000000000080` |
   | 二者 + `no-new-privileges` | 2 / 0 | 1 | `0000000000000000` |

   ⇒ **seccomp 档不设 NNP，所以不破坏 file capability；`allowPrivilegeEscalation: false` 才破坏。** 这就是本仓库 `c3-agent.yaml`"不得出现该字段"那条 pin 的机制依据（原注释只写了结论）。

2. **grant 路径实测**：`setgroups([])` → `setresgid` → `setresuid` 在 Docker 默认档下 `GRANT-PATH-OK 10001 10001 []`。

3. **跨 pod 隔离实测**（决定"要不要把 seccomp-installer 合进 c3-agent"）：同 host pid ns、uid 0、face B 的完整能力集（无 `CAP_SYS_PTRACE`）访问另一个 pod 的 `/proc/<pid>/root/<挂载>` → `Permission denied`；加上 `CAP_SYS_PTRACE` 后读写成写都成功。⇒ **两个 DaemonSet 之间今天的隔离全部押在"face B 没有 `CAP_SYS_PTRACE`"这一条上**，合并会让 agent 拿到写 worker seccomp 档的能力。

4. **待办**：本机用 Docker 默认档代表 `RuntimeDefault`，containerd 那份是另一实现；上集群前要在节点上复验一次 grant 路径。

- [ ] **Step 2: 验证文件成文且引用可达**

Run: `python3 -m pytest tests/unit/test_c3_agent_manifest.py -q` 与
`ls docs/security-audit/c3-agent-syscall-filter-2026-10-05.md`
Expected: 前者仍 PASS；后者存在。（文档任务没有可断言的代码行为，不新增用例——`md_lint` 只查重复标题与危险 git 命令，加一条空断言反而是噪声。）

- [ ] **Step 3: commit**

```bash
git add docs/security-audit/c3-agent-syscall-filter-2026-10-05.md
git commit -m "docs(security): the measured basis for c3-agent's syscall filter and why NNP stays out"
```

---

## 本计划不做（已裁定，理由记进 ledger）

| 项 | 裁定 |
|---|---|
| `allowPrivilegeEscalation: false` 加到 init/face B | **不做**。`test_the_agent_manifest_never_names_a_forbidden_privilege` 是文本级 pin，且上一轮 review 特意把它扩到了 init 容器（同一个文件里写明理由）。对已经是 root + `DAC_OVERRIDE` 的容器，这条的收益接近零；换来的风险是有人以后把带 file capability 的二进制挪进去时，毯式禁令不再兜底。 |
| 让 installer 多铺一份 `sandlock-agent.json` | **不做**。实测默认档够用（Task 1 Step 5），第二份档是净增的维护面。 |
| namespace 级 default-deny NetworkPolicy | **不做**。它需要一份完整的东西向流量清单（CP↔redis/buildkit、gateway 入站、autoscaler、DNS…），在没上集群核对的前提下写下去，风险是下一次 apply 直接断服务。留给独立计划。 |
| `hostPID: true` | **不做**。pod 级字段，face A 的 pid 反查需要它；已在 manifest 里被点名接受。 |
| 构建镜像 / 上集群 | **不做**。本计划的产物是仓库内的清单与测试，部署走本仓库自己的门禁。 |
