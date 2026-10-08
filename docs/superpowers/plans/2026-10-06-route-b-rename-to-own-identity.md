# route B → own_identity：把「哪条路」换成「以谁的身份」 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `route A/B` 这套设计评审遗留的字母名，换成按本质命名的一套词：**`own_identity`（选型：这份工作以谁的 uid 在宿主上发生）+ `slot`（承载那个身份的进程池）+ `identity_grant`（那个身份怎么授予）**。部署面只动 1 个环境变量条目，且新旧名同批并存一个版本。

**Architecture:** 纯改名 + 一层别名兼容。代码标识符/模块名/配置键一次到位；环境变量走「新名优先、旧名接受并告警一次」；**跨进程与跨版本的契约串一律冻结**（槽位文档的实例名、线协议路径、磁盘目录取值）。历史文档不改 —— 改它等于篡改记录。

**Tech Stack:** Python 3.12 / pytest；k8s 清单（kustomize，`deploy/k8s-k0s/apply.sh`）；docker compose；k0s 集群验收。

**Spec:** 本文件「命名冻结」一节；`docs/isolation-boundaries.md` §1（userns 是身份翻译）；`envd_service/route_b.py` 自己的两句原文 —— *"a sandbox's route-B mediator must run as that sandbox's own host uid (that identity is the whole point of route B)"* 与 *"mediating as the sandbox's own uid is what makes mediated writes belong to the sandbox (T5)"*。

## 命名冻结

现在一个名字盖住了三件不同的事，这是本次改名的全部理由：

| 概念 | 旧名（今天） | 新名 | 部署里用到了吗 |
|---|---|---|---|
| **后端选型**：这份沙箱的工作以谁的 uid 在宿主上做 | `route_b` / `RouteB` / `E2B_ROUTE_B` | `own_identity` / `OwnIdentity` / `E2B_OWN_IDENTITY` | 否（生产用默认 `auto`） |
| **进程池**：承载那个身份，一个 uid = 一个进程 = 一代 | `E2B_ROUTE_B_SLOTS` / `_TMP_ROOT` / `_TRANSPORT` / `_VERB_TIMEOUT_S` | `E2B_MAX_SLOTS` / `E2B_SLOT_TMP_ROOT` / `E2B_SLOT_TRANSPORT` / `E2B_SLOT_VERB_TIMEOUT_S` | **是 —— 只有 `E2B_ROUTE_B_TMP_ROOT`**（`worker.yaml` + `control-plane.yaml`） |
| **身份授予**：那个身份怎么落到进程上 | `E2B_SLOT_IDENTITY` / `E2B_SLOT_IDENTITY_{WAIT,REPORT}_TIMEOUT_S` / `slot_identity.py` | `E2B_IDENTITY_GRANT` / `E2B_IDENTITY_GRANT_{WAIT,REPORT}_TIMEOUT_S` / `identity_grant.py` | 否（唯一取值 `agent-grant` 就是默认） |

对照的另一半（今天的「route A」）在代码里本来就叫 in-process，本次不改名，只在文档里与 `own_identity` 并称。

## Global Constraints

- **冻结串（改名不得触碰，逐字节）**：`gateway_common.paths.route_b_instance_name()` 的**返回字符串**（CP 与 worker 共用的槽位文档路径，D20）、`POST /internal/nodes/{id}/slot-identity`、file-op 作用域名 `scope-slot-document`、`--control-fd` / `--events-fd`、磁盘目录名 `.route-b` 与清单里的取值 `/var/lib/e2b/state/.route-b`、`E2B_PER_SANDBOX_UID`（它回答「沙箱拿到哪个 uid」，与「以谁的身份做」正交）。
- **环境变量一个版本内新旧并存**：新名优先；只设旧名时照旧生效并打**一次** WARNING 点名新名；两者都设且不同值时**新名胜出**并告警。
- **历史文档不改**：`docs/reports/**`、`docs/superpowers/**`、`docs/security-audit/**`，以及 `docs/deploy-clusters.md` 的 §7.x 发版记录 —— 它们记的是当时的事实。
- **一次机械改名一次提交**；改名不顺手改行为（别名读取与其告警是唯一例外）。
- 测试规范：精确断言（禁用 `contains` 类部分匹配）、禁止 SKIP、每个行为改动 RED→GREEN。
- E2B 侧 `tests/unit` 有 3 条 **macOS 固有红**（`test_real_root_gate::test_the_probe_asks_for_the_pivot_root_this_architecture_has`、`test_xfs_quotactl_backend` 两条）；本次不得新增红。
- 集群操作前 `deploy/scripts/open-cluster-tunnel.sh`，每条 `kubectl` 带 `KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`。

## Review Focus

1. **冻结串被顺手改掉**：`route_b_instance_name()` 的输出是 CP 与 worker 共用的磁盘契约 —— 函数可以改名，**输出不能变**，否则槽位文档当场找不到。（Task 1 守卫用例）
2. **新旧同时设置时静默任选一边**：别名里最坏的形状 —— 必须是**新名胜出 + 告警**。（Task 2 用例）
3. **清单与镜像错序**：新清单 + 旧镜像 ⇒ `E2B_SLOT_TMP_ROOT` 被旧镜像忽略、退回默认 `/tmp/sandlock-route-b`，槽位文档落到非共享目录 ⇒ 建箱失败（具名失败，不是静默降级）。（Task 3 用例 + Task 7 的顺序要求）
4. **历史文档被「顺手统一」**：把 §7.47 里的 route B 改成新名等于篡改发版记录。（Task 5 的双向断言）
5. **`E2B_PER_SANDBOX_UID` 被误并入**：它回答「哪个 uid 归这个沙箱」，与「以谁的身份做」正交，两套名字必须能各自独立地被搜索到。（Task 3 断言）

---

### Task 1: Python 标识符与模块 —— `route_b` → `own_identity`

**Files:**
- Rename: `envd_service/route_b.py` → `envd_service/own_identity.py`
- Rename: `tests/unit/test_sandlock_executor_route_b.py` → `…test_sandlock_executor_own_identity.py`、`tests/unit/test_route_b_wiring.py` → `…test_own_identity_wiring.py`、`tests/unit/test_route_b_event_pump.py` → `…test_own_identity_event_pump.py`、`tests/contract/test_route_b_executor.py` / `test_route_b_slot_pool.py` / `test_nonroot_route_b.py` 同理
- Modify: `envd_service/executors/sandlock.py`（`_route_b_active` / `_route_b_decline_reason` / `_open_route_b_instance` / 两个 warn-once 开关）、`envd_service/executors/factory.py`、`envd_service/config.py`（`Settings.route_b` → `Settings.own_identity`）、`envd_service/app.py`、`envd_service/agent.py`、`control_plane/config.py`、`control_plane/file_ops.py`、`gateway_common/paths.py`
- Test: `tests/unit/test_own_identity_naming.py`（新建，守卫用例）、`tests/unit/test_c3_slot_document_naming.py`（D20 那条把 CP 与 worker 的命名规则钉在一起的用例，必须跟着改）；另加 45 个命中测试文件里的 import 与符号

**Interfaces:**
- Produces: `envd_service.own_identity.OwnIdentityConfig` / `OwnIdentityInstance` / `OwnIdentityExecProcess`；`Settings.own_identity: str`；`gateway_common.paths.own_identity_instance_name(sandbox_id: str) -> str` 与 `OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES = 64`。
- **冻结**：`own_identity_instance_name()` 的返回值与今天 `route_b_instance_name()` 逐字节相同（见下）。
- Consumes（本轮不动）：`slot_pool_for`、`SlotHandle`、`W1SlotPool`，以及 `OwnIdentityConfig` 的**字段名**（`mode` / `slots` / `tmp_root` / `transport` / `verb_timeout_s` / `slot_identity`）—— 它们归 Task 2/3/4。

- [x] **Step 1: 写守卫用例（新名字 + 三种输入形态的冻结输出）**

```python
def test_the_own_identity_instance_name_is_frozen() -> None:
    from gateway_common.paths import (
        OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES,
        own_identity_instance_name,
    )

    assert OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES == 64
    assert own_identity_instance_name("sbx_0123456789abcdef") == "sbx_0123456789abcdef"
    # 超长 id 换哈希：这一支是规则的一部分，CP 不照做就会找不到文档目录（D20）
    assert own_identity_instance_name("sbx_" + "a" * 80) == "sbx_b926db5e21e80dd2"
    # 恰好到达上限的 id 原样通过
    at_limit = "sbx_" + "a" * 60
    assert own_identity_instance_name(at_limit) == at_limit
```

另一个文件里加 import 冻结：

```python
def test_the_backend_types_are_reachable_under_their_new_names() -> None:
    from envd_service.own_identity import (
        OwnIdentityConfig,
        OwnIdentityExecProcess,
        OwnIdentityInstance,
    )

    assert OwnIdentityConfig(mode="off").mode == "off"
```

（三个字面量取自今天的实现，逐字节抄：短 id 原样、84 字节 id → `sbx_b926db5e21e80dd2`、64 字节 id 原样。）

- [x] **Step 2: 跑它确认红**

Run: `.venv/bin/python -m pytest tests/unit/test_own_identity_naming.py -q`
Expected: FAIL —— `ImportError: cannot import name 'own_identity_instance_name'`。

- [x] **Step 3: 机械改名**

`git mv envd_service/route_b.py envd_service/own_identity.py`；符号替换 `route_b_instance_name` → `own_identity_instance_name`、`ROUTE_B_INSTANCE_NAME_MAX_BYTES` → `OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES`、`RouteBConfig` → `OwnIdentityConfig`、`RouteBInstance` → `OwnIdentityInstance`、`RouteBExecProcess` → `OwnIdentityExecProcess`、`_route_b_active` → `_own_identity_active`、`_route_b_decline_reason` → `_own_identity_decline_reason`、`_open_route_b_instance` → `_open_own_identity_instance`、`_route_b_no_starter_warned` / `_route_b_no_fd_client_warned` → `_own_identity_…`；`Settings.route_b` → `Settings.own_identity`。**函数体一字不动**（冻结串在里面）；同批改 45 个测试文件的 import 与符号。

- [x] **Step 4: 跑测试确认绿**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: 与基线同绿，**仍然只有那 3 条 macOS 固有红**；新守卫用例通过。

- [x] **Step 5: Commit**

```bash
git add -A envd_service control_plane gateway_common tests
git commit -m "refactor(own-identity): route_b becomes own_identity in the code"
```

**提交时用显式路径，不要 `git add -A tests`**：工作树里有一个**并行会话未提交**的改动
（`tests/unit/test_docs_only_point_at_repo_artifacts.py`，与本次改名无关），`-A` 会把它卷进我们的提交。

### Task 2: 选型键 `E2B_ROUTE_B` → `E2B_OWN_IDENTITY`（别名 + 一次告警）

**Files:**
- Create: `envd_service/env_alias.py`、`tests/unit/test_env_alias.py`
- Modify: `envd_service/config.py`（`own_identity` 字段的读取那一行）

**Interfaces:**
- Produces: `env_alias.read(name: str, *, legacy: str, default: str = "") -> str` —— 新名非空则用它；否则读旧名并 `logger.warning` **一次**（每个旧名一次，进程级 `set`）；都没有则返回 `default`。
- Consumes: 无。

- [x] **Step 1: 写失败用例**

```python
def test_the_new_name_wins_when_both_are_set(monkeypatch, caplog) -> None:
    monkeypatch.setenv("E2B_OWN_IDENTITY", "off")
    monkeypatch.setenv("E2B_ROUTE_B", "on")
    assert _settings().own_identity == "off"
    assert "E2B_ROUTE_B" in caplog.text and "E2B_OWN_IDENTITY" in caplog.text


def test_the_legacy_name_still_works_and_warns_once(monkeypatch, caplog) -> None:
    monkeypatch.delenv("E2B_OWN_IDENTITY", raising=False)
    monkeypatch.setenv("E2B_ROUTE_B", "on")
    assert _settings().own_identity == "on"
    assert caplog.text.count("E2B_ROUTE_B") == 1
    assert _settings().own_identity == "on"      # 第二次不再告警
```

- [x] **Step 2: 跑它确认红**

Run: `.venv/bin/python -m pytest tests/unit/test_env_alias.py -q`
Expected: FAIL —— `E2B_OWN_IDENTITY=off` 被忽略，`own_identity` 仍是 `"on"`。

- [x] **Step 3: 实现 `env_alias.read()`，把 `Settings.own_identity` 接上**

模块级 `_warned: set[str]`；`logger = logging.getLogger(__name__)`；告警文案同时点名旧名与新名。

- [x] **Step 4: 跑测试确认绿**

Run: `.venv/bin/python -m pytest tests/unit/test_env_alias.py tests/unit/test_own_identity_wiring.py -q`
Expected: PASS。

- [x] **Step 5: Commit**

```bash
git add envd_service/env_alias.py envd_service/config.py tests/unit/test_env_alias.py
git commit -m "feat(own-identity): E2B_OWN_IDENTITY, with E2B_ROUTE_B as a warned alias"
```

### Task 3: 池旋钮归位 `E2B_ROUTE_B_*` → `E2B_SLOT_*` / `E2B_MAX_SLOTS`（**唯一动部署的一步**）

**Files:**
- Modify: `envd_service/config.py`（`route_b_slots` / `_transport` / `_verb_timeout_s` / `_tmp_root` 四个字段的读取）、`envd_service/own_identity.py`（`tmp_root` 默认值注释）
- Modify: `deploy/k8s/worker.yaml`、`deploy/k8s/control-plane.yaml`、`deploy/compose/docker-compose.multinode.yml`、`deploy/compose/docker-compose.prod.yml`、`deploy/stack/docker-compose.prod.yml`
- Test: `tests/unit/test_worker_env_key_sets.py`（`KEY_CLASSES["route_b_root"]` → `KEY_CLASSES["slot_tmp_root"]`，表项与它的**两处联合引用**都要跟）

**Interfaces:**
- Produces: `Settings.max_slots: int`、`Settings.slot_transport: str`、`Settings.slot_verb_timeout_s: float`、`Settings.slot_tmp_root: Path` 及对应四个新键。
- Consumes: `env_alias.read()`（Task 2）。

- [x] **Step 1: 写失败用例**

```python
def test_the_slot_tmp_root_carries_the_new_name(monkeypatch) -> None:
    monkeypatch.setenv("E2B_SLOT_TMP_ROOT", "/var/lib/e2b/state/.route-b")
    assert _settings().slot_tmp_root == Path("/var/lib/e2b/state/.route-b")


def test_the_env_key_table_names_the_slot_root() -> None:
    assert KEY_CLASSES["slot_tmp_root"] == {"E2B_SLOT_TMP_ROOT"}


def test_per_sandbox_uid_is_not_part_of_this_rename() -> None:
    assert "E2B_PER_SANDBOX_UID" in Path("deploy/k8s/worker.yaml").read_text()
```

- [x] **Step 2: 跑它确认红**

Run: `.venv/bin/python -m pytest tests/unit/test_worker_env_key_sets.py -q`
Expected: FAIL —— 新键被忽略（`slot_tmp_root` 退回默认 `/tmp/sandlock-route-b`）、表项仍是旧键、`KEY_CLASSES["slot_tmp_root"]` 不存在。

- [x] **Step 3: 改四个字段的读取 + 五个清单/compose + 表项**

清单**只换键名**，`value` 逐字节不动（`/var/lib/e2b/state/.route-b`，目录名 `.route-b` 是冻结串）。`KEY_CLASSES` 的键改名时，把同文件里引用它的两处集合联合（`KEY_CLASSES["route_b_root"] | …`）一起改 —— 漏一处会让"允许缺失/允许额外"的判定悄悄变宽。

- [x] **Step 4: 跑测试确认绿**

Run: `.venv/bin/python -m pytest tests/unit/test_worker_env_key_sets.py tests/unit/test_c3_agent_manifest.py tests/unit/test_worker_manifest_permissions.py -q`
Expected: PASS（含「三个 multinode worker 声明同一组 env key」那条）。

- [x] **Step 5: Commit**

```bash
git add envd_service deploy tests
git commit -m "refactor(slot): the pool knobs take the slot namespace, and the manifests follow"
```

### Task 4: 授予方式正名 —— `E2B_SLOT_IDENTITY` → `E2B_IDENTITY_GRANT`

**Files:**
- Rename: `envd_service/slot_identity.py` → `envd_service/identity_grant.py`；`tests/unit/test_slot_identity.py` → `tests/unit/test_identity_grant.py`（Task 1 已把原名里的 `route_b_` 前缀去掉，这里再正名）
- Modify: `envd_service/config.py`（`slot_identity`、`slot_identity_timeout_s`）、`envd_service/worker_identity.py`、`envd_service/own_identity.py`（`slot_identity` 字段与其取值校验文案、`_spawn_slot_identity` → `_spawn_slot_child`）
- 部署清单：**本次不动** —— 查下来 `E2B_SLOT_IDENTITY*` 在生产清单与 compose 里都没设过（唯一取值 `agent-grant` 就是代码默认）。

**Interfaces:**
- Produces: `Settings.identity_grant: str`、`Settings.identity_grant_report_timeout_s: float`、`envd_service.identity_grant` 模块、`IdentityGrantConfig` 的对应字段；环境变量 `E2B_IDENTITY_GRANT` / `E2B_IDENTITY_GRANT_REPORT_TIMEOUT_S` / `E2B_IDENTITY_GRANT_WAIT_TIMEOUT_S`。
- **保留**：对已退役取值 `spawn` 的具名拒绝（C3 / N52）；旧名三个键仍被读，并各告警一次。

- [x] **Step 1: 写失败用例**

```python
def test_the_legacy_grant_key_warns_and_names_the_new_one(monkeypatch, caplog) -> None:
    monkeypatch.delenv("E2B_IDENTITY_GRANT", raising=False)
    monkeypatch.setenv("E2B_SLOT_IDENTITY", "agent-grant")
    assert _settings().identity_grant == "agent-grant"
    assert "E2B_IDENTITY_GRANT" in caplog.text


def test_the_retired_grant_value_is_still_refused_by_name(monkeypatch) -> None:
    monkeypatch.setenv("E2B_IDENTITY_GRANT", "spawn")
    with pytest.raises(ValueError, match="must be 'agent-grant'"):
        _settings()
```

- [x] **Step 2: 跑它确认红**

Run: `.venv/bin/python -m pytest tests/unit/test_identity_grant.py -q`
Expected: FAIL —— 第一条没有任何关于新名的告警；第二条 `E2B_IDENTITY_GRANT` 被忽略、默认值让构造成功（应当具名拒绝）。

- [x] **Step 3: 改名 + 接上别名**

`git mv envd_service/slot_identity.py envd_service/identity_grant.py`；`slot_identity_timeout_s()` → `identity_grant_timeout_s()`、`SlotProcess` → `SlotProcess`（不动，它本来就是 slot 概念）、`_await_identity` / `spawn_child` / `_keep_inheritable` 函数体一字不动。

- [x] **Step 4: 跑测试确认绿**

Run: `.venv/bin/python -m pytest tests/unit/test_identity_grant.py tests/unit/test_sandlock_executor_own_identity.py -q`
Expected: PASS（含 Task 1 之后这两族的全部用例）。

- [x] **Step 5: Commit**

```bash
git add -A envd_service tests
git commit -m "refactor(identity-grant): the grant mechanism stops being called 'slot identity'"
```

（同样用显式路径提交，理由同 Task 1。）

### Task 5: 文档统一（现状改新名，历史冻结）

**Files:**
- Modify（现状文档）: `docs/isolation-boundaries.md`、`docs/env-vars.md`、`docs/k8s-deployment.md`、`docs/production-deployment-requirements.md`、`docs/chroot-workspace-exec.md`、`docs/c3-privilege-relocation.md`、`docs/c2-ownership-frontload.md`、`docs/build-test-deploy-pitfalls.md`、`docs/checkpoint-restore-e2b-half.md`、`docs/pure-shape-decision.md`、`docs/sandbox-disk-quota.md`、`docs/security-architecture.md`、`docs/sandlock-upstream-issues.md`、`docs/task-backlog.md`、`docs/n25-remainder-plan.md`、`docs/create-local-first-design.md`、`docs/create-local-first-layout.md`、`docs/HANDOFF.md`
- Modify（活表，只在新行用新名）: `docs/open-issues.md`
- **不改**：`docs/reports/**`、`docs/superpowers/**`、`docs/security-audit/**`、`docs/deploy-clusters.md`

**Interfaces:**
- Produces: 现状文档里 `route B` → `own identity`（句子自然改写，不逐字替换）、`E2B_ROUTE_B*` → 新键、`slot identity` → `identity grant`；`docs/isolation-boundaries.md` §1 加一行「旧称 route B（2026-10 按本质改名）」；`deploy/scripts/acceptance/` 里的探针**文件名不动**（历史证据的引用），只改它们 import 的模块名。

- [x] **Step 1: 写断言（把「历史冻结」变成可运行的检查）**

```bash
# 现状文档里不应再出现旧环境变量名（历史目录与发版记录除外）
rg -n 'E2B_ROUTE_B' docs --glob '!docs/reports/**' --glob '!docs/superpowers/**' \
   --glob '!docs/security-audit/**' --glob '!docs/deploy-clusters.md'
# 历史记录里必须仍然有旧名（证明没被改写）
rg -c 'route B' docs/deploy-clusters.md docs/superpowers/plans/2026-10-06-n80-clone3-and-unshare-retirement.md
```

- [x] **Step 2: 跑它确认当前是红的**

Run: 上面第一条
Expected: 有命中（旧键仍留在现状文档里）。

- [x] **Step 3: 逐文件改写，并在述评处留一处「旧称」**

- [x] **Step 4: 跑断言确认绿**

Run: 同样两条
Expected: 第一条 **0 命中**；第二条两边都 > 0。

- [x] **Step 5: Commit**

```bash
git add docs
git commit -m "docs(own-identity): the current docs use the name, the history keeps its own"
```

### Task 6: 删别名（**下一版，本轮不做**）

删掉 `E2B_ROUTE_B*` 与 `E2B_SLOT_IDENTITY*` 的别名读取、`envd_service/env_alias.py`、那两条弃用告警，以及 `tests/unit/test_env_alias.py`；断言改成「旧名不再被识别」。等本版上线并稳定一个版本之后再开 —— 提前删会让"清单尚未更新"的部署形态静默退回默认值。

### Task 7: 发布（**需授权；镜像与清单同批**）

**顺序不能反**：

1. `VERSION=<new> deploy/scripts/build-and-push.sh`（四镜像 + 基础镜像镜像；第一次 ACR push 失败就原样重跑一次，§7.47 记过这个现象）；
2. `DRY_RUN=1 deploy/k8s-k0s/apply.sh` 渲染 → `kubectl diff -f -` 核对：本次预期**只有** workload 的 image tag（+ 若清单键名变了则是那一处 env 的 key），**不应**出现 seccomp 档变化；
3. `deploy/k8s-k0s/apply.sh`，等 `ds/e2b-c3-agent` → `sts/e2b-worker`，再等 `ds/seccomp-installer` 收敛；
4. 收尾读数：9/9 pod Running / 0 重启、三处镜像 tag ≡ `deploy/stack/.version`、`GET /sandboxes` 为 `[]`、`DRY_RUN=1 apply.sh | kubectl diff -f - | wc -l` = 12（NetworkPolicy generation 噪声）；
5. 两条冒烟（`E2B_API_URL=http://172.18.78.49:3000`，凭据从 `secret/e2b-secrets` 取、`env -u http_proxy -u https_proxy -u all_proxy`）：`MULTI-NODE SMOKE OK` + `DEPLOYMENT SMOKE OK`。

**为什么必须同批**：新清单里的 `E2B_SLOT_TMP_ROOT` 旧镜像读不懂，会退回代码默认的 `/tmp/sandlock-route-b` —— 槽位文档落到非共享目录，建箱直接失败（Review Focus 3）。新镜像读得懂两边（Task 3 的别名），所以"镜像先、清单后"不行，"同批"才是安全形状。

---

## 收尾（2026-10-08：Task 1–5、7 已完，Task 6 仍留到下一版）

写这段的原因：Task 1–5 的复选框当时一个都没勾，读计划的人以为没开工，而事实是
**0.1.0-1151-g274b3a5 已经纯改名上线**（发版记录见 `docs/deploy-clusters.md` §7.57，线上 9/9 +
两条冒烟）。所以：上面对 25 个 Step 的勾是照证据补的，逐条出处如下。

| Task | 状态 | 证据 |
|---|---|---|
| 1 标识符与模块 | ✅ | `envd_service/route_b.py` → `own_identity.py`、三个 `test_*route_b*` 用例改名、`gateway_common.paths.route_b_instance_name` → `own_identity_instance_name`；冻结输出由 `tests/unit/test_own_identity_naming.py::test_the_own_identity_instance_name_is_frozen` 钉住 |
| 2 环境变量别名 | ✅ | `envd_service/env_alias.py`（新名优先、旧名接受并告警一次、两者都设且不同值时新名胜出）；`tests/unit/test_env_alias.py` 全绿 |
| 3 槽位旋钮（唯一动部署面的一项） | ✅ | `E2B_MAX_SLOTS` / `E2B_SLOT_TMP_ROOT` / `E2B_SLOT_TRANSPORT` / `E2B_SLOT_VERB_TIMEOUT_S`；五份清单/compose 与表项已改，`tests/unit/test_c3_agent_manifest.py`、`test_worker_manifest_permissions.py`、`test_worker_env_key_sets.py` 钉住取值 |
| 4 身份授予 | ✅ | `slot_identity.py` → `identity_grant.py`、`E2B_IDENTITY_GRANT` / `E2B_IDENTITY_GRANT_{WAIT,REPORT}_TIMEOUT_S`；`tests/unit/test_identity_grant.py` 全绿 |
| 5 现状文档 | ✅ | `docs/isolation-boundaries.md` 留了那处「旧称」；计划自己的验收命令今天复跑：`rg 'E2B_ROUTE_B' docs`（排除三份归档 + 两份日期账）**0 命中**，而 `docs/deploy-clusters.md`（6）与本计划（12）**仍有**旧名 |
| 7 发布 | ✅ | §7.57：同批镜像 + 清单、9/9、`DEPLOYMENT SMOKE OK` + `MULTI-NODE SMOKE OK` |
| 6 删别名 | ⏳ **下一版** | 不变：提前删会让「清单尚未更新」的形态静默退回默认值 |

### 本轮补的那半：活字（2026-10-08，Task 1 的第 3 条断言）

Task 1–5 换的是**标识符、模块名、环境键、现状文档**；**活代码里的字**当时没换完
（`own_identity.py` 52 处、`executors/sandlock.py` 45 处，测试/清单/验收探针里同样成片），
而计划要换掉的正是这套设计评审遗留的字母名。两笔补齐：

* `11d6b21` —— 83 个文件的活字换完（含 worker 的就绪行与拒绝文本：`deploy/scripts/`
  里有**按字符串读它们**的验收脚本，必须同批改，否则车道会绿着却什么都没验证到）。
* `6aea5bb` —— 把它变成钉子（`tests/unit/test_own_identity_naming.py` 新增两条），
  免得再长回来；RED→GREEN 见该文件与提交信息。

**这轮执行的冻结清单**（计划「命名冻结」一节的落地版，钉子按它放行）：

| 冻结的东西 | 为什么 |
|---|---|
| `.route-b`、`/var/lib/e2b*/state/.route-b`、`/tmp/sandlock-route-b` | 磁盘目录名与它的派生默认值（CP 与 worker 都从它推导槽位文档路径） |
| `rb-<id>` | 无名字调用方的槽位叶子名（嵌入方/测试用；真实路径由 `own_identity_instance_name` 给） |
| `E2B_ROUTE_B*`、`E2B_SLOT_IDENTITY*` | Task 6 才删的别名，删早了会静默退回默认值 |
| `scope-slot-document`、`/internal/nodes/{id}/slot-identity` | 线协议/文件操作作用域名 |
| 四支验收探针的**文件名**（`routeb_cap_probe.py`、`routeb_statfs_probe.py`、`red-routeb-stderr-drain.py`、`rb_token_probe.py`） | 被现存文档与历史记录**按名**引用；改名等于让历史指向不存在的文件（正文里的字已换） |
| `docs/reports/**`、`docs/superpowers/**`、`docs/security-audit/**`、`docs/deploy-clusters.md`、`docs/HANDOFF.md` | 归档与两份日期账记的是那一天的事实，改它等于篡改记录（钉子反向断言它们**仍有**旧名） |

**唯一留在活文档里的旧名**：`docs/isolation-boundaries.md` 的
「旧称 route B（2026-10 按本质改名）」—— 让按旧名找过来的人能落地；钉子断言它还在。

**`third_party/sandlock` 不在范围内**：那是 fork 自己的仓库，它的
`docs/e2b-integration.md` / `supervise-identity-handoff.md` 是那个项目的记录。
