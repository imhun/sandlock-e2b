# O3 凭据管理（从"明文/Secret 混用"到"有轮换流程"）实施计划

> **执行状态（2026-09-27 更新）**：**已收口** —— 轮换 runbook（四张表）、指纹对账与统一验收落 `docs/k8s-deployment.md`《凭据管理》；线上已开 `E2B_SECRET_MASTER_KEY`（降级告警消失）。api/internal key 走**双窗轮换**，redis 按裁定**接受 10–30 s 中断**（不做 ACL 双用户）。
> **仍有效的决定**：三类凭据分开处理、双窗 vs 接受中断的取舍。**已作废的假设**：把"开 master key"当成"既有 `_secrets` 明文自动加密"——**不成立**，既有明文需一次性清理（`deploy/scripts/cleanup-plaintext-secrets.py`，见 `docs/k8s-deployment.md` §4.5.1）。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 k8s 形态的凭据从"人手工 `kubectl create secret` + 一份降级态的 secret 存储"变成**有脚本、有指纹、有轮换窗口**的流程：先做零窗口加固（开启 `E2B_SECRET_MASTER_KEY`，让 `_secrets` 不再是共享卷上的明文），再把每个凭据的**轮换步骤与影响面**写成可执行 runbook（哪些服务要重启、有没有不可逆窗口）。

**Architecture:** 三类凭据分开处理。**有双窗的**（`E2B_API_KEYS`、`E2B_INTERNAL_API_KEY`、`E2B_SECRET_MASTER_KEY`）：走"新 key 与旧 key 并存 → 滚动 → finalize 移除旧 key"，中间不断服；`upgrade.sh` 已经有 internal key 与 master key 的两窗算法（`deploy/scripts/upgrade.sh:122-220`），k8s 侧只是没有等价脚本。**无双窗的**（`E2B_REDIS_PASSWORD`、`E2B_QUOTA_AGENT_TOKEN`）：redis 只有一个 `requirepass`，必然有 10–30 s 中断——要么接受，要么先把它改成 ACL 双用户（redis 8 支持 `ACL SETUSER`，本仓的镜像是 `redis:8-alpine`）。**只在开发机上的明文文件**（`deploy/scripts/acr.env`、`deploy/scripts/bastion.env`，均已 gitignore）单独收口：轮换 + 权限修正。

**Tech Stack:** kubectl/kustomize（`deploy/k8s-k0s/apply.sh`）、bash（新 `deploy/k8s-k0s/secrets.sh`、`deploy/k8s-k0s/rotate-secret-master.sh`）、redis 8 ACL、Fernet/HKDF（`control_plane/registry/secrets.py`）、`openssl rand`、pytest 单测（清单/脚本的静态契约）。

## Global Constraints

- 临时文件一律放本仓库 `tmp/`（AGENTS.md），不使用系统 `/tmp`、`$TMPDIR`。
- 测试断言必须精确匹配；禁用 `toContain` / `includes` / 部分匹配；禁止新增 skip 或用 ignore 掩盖失败。
- 改了部署清单先看差异：`DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl diff -f -`。
- 任何 kubectl 都必须显式带 `KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`（判据见 `docs/deploy-clusters.md` §2）。
- 本机测试命令是 `tmp/testenv/bin/python -m pytest`（`.venv` 缺 `fakeredis`）。
- **任何脚本、日志、提交信息里都不许出现凭据明文**：只打印指纹（照 `deploy/scripts/c4-prjquota-window.sh:232` 的既有做法——`sha256(前16)`，值不出目标机）。
- **凭据只从 Secret 取**：不在清单里写明文、不在 `kubectl get secret -o yaml` 的输出里回贴明文；验收项里"Secret 内容不含明文"要按可判定的方式写。
- **不可逆点必须先声明**：每个轮换表都要写"哪一步之后就回不去了"（finalize 移除旧 key、删旧 redis 口令、删旧公钥）。
- `worker` 滚动 = **杀掉全部 running 沙箱**（沙箱是 worker pod 内的进程，N20 语义）——凡是要滚 worker 的轮换，必须在低峰/维护窗口做。

---

### Task 1: Step 0 零窗口加固（开 `E2B_SECRET_MASTER_KEY` + Secret 脚本化）

**Files:**
- Create: `deploy/k8s-k0s/secrets.sh`
- Modify: `deploy/k8s/control-plane.yaml:150-182`（新增两个 `secretKeyRef`）
- Modify: `deploy/k8s-k0s/README.md`（部署顺序里插入"先跑 secrets.sh"）
- Create: `tests/unit/test_k0s_secrets_script.py`

**Interfaces:**
- Consumes: `control_plane/config.py:280-292`（`E2B_SECRET_MASTER_KEY` / `E2B_SECRET_MASTER_KEYS`）、`control_plane/registry/secrets.py`（无 master key 时降级成"内存 + 明文落盘"，并只打一条启动告警）、`control_plane/app.py:392-397`（`_secrets` 目录挂在 `workspace_base` 下）
- Produces: `deploy/k8s-k0s/secrets.sh`（幂等创建/更新 `e2b-secrets`，只打印指纹）；清单里两个新的 `secretKeyRef`

- [ ] **Step 1: 确认现状（这一步是"零窗口加固"的全部理由）**

  Run: `rg -n "SECRET_MASTER" deploy/k8s/ deploy/k8s-k0s/ ; echo "exit=$?"`

  Expected: **无命中**（`exit=1`）——k8s 清单完全没有 master key，于是 `SecretRegistry` 处于降级态：secret 明文落在**共享 NAS 卷**的 `<workspace_base>/_secrets/**` 上，而 worker pod 把整卷 RW 挂进来（`deploy/k8s/worker.yaml:593-595`），任何拿到 pod root 的人都能读。

  Run: `rg -n "workspace_base.*_secrets|_secrets" control_plane/app.py | head -3`

  Expected: 命中 `app.py:392` 一带（`SecretRegistry((workspace_base or settings.workspace_base) / "_secrets", ...)`）。

  Run: `rg -n "E2B_SECRET_MASTER_KEY|E2B_SECRET_MASTER_KEYS" docs/k8s-deployment.md deploy/k8s-k0s/README.md`

  Expected: 无命中（文档也没写怎么设）——这就是 Task 1 要补的第二件事。

- [ ] **Step 2: 写"会失败"的脚本契约测试**

  `tests/unit/test_k0s_secrets_script.py`：

  ```python
  from pathlib import Path

  SCRIPT = Path("deploy/k8s-k0s/secrets.sh")

  def test_script_generates_every_key_the_manifests_read():
      text = SCRIPT.read_text()
      for key in ("E2B_API_KEYS", "E2B_INTERNAL_API_KEY", "E2B_REDIS_PASSWORD", "E2B_SECRET_MASTER_KEY"):
          assert f"{key}" in text

  def test_script_is_idempotent_and_never_prints_secrets():
      text = SCRIPT.read_text()
      assert "--dry-run=client -o yaml | kubectl apply -f -" in text
      assert "sha256" in text                      # 只打指纹
      assert "echo \"$E2B_API_KEYS\"" not in text   # 不回显明文
      assert "set -x" not in text                   # 不许开 trace（会打印变量）

  def test_script_refuses_to_overwrite_a_live_secret_without_a_flag():
      text = SCRIPT.read_text()
      assert "--rotate" in text
      assert "kubectl -n" in text and "get secret e2b-secrets" in text
  ```

- [ ] **Step 3: 写脚本 + 改清单**

  1. `deploy/k8s-k0s/secrets.sh`：
     - `NAMESPACE="${NAMESPACE:-sandlock}"`；先 `kubectl -n "$NS" get secret e2b-secrets -o json`（不存在 ⇒ 全新建；存在且没给 `--rotate` ⇒ 只补**缺失**的键，不覆盖已有的）；
     - 用 `openssl rand -hex 32` 生成新值；用
       `kubectl -n "$NS" create secret generic e2b-secrets --from-literal=... --dry-run=client -o yaml | kubectl apply -f -`
       幂等落盘（这条正是 k8s 侧唯一"可重放"的写法）；
     - 打印每个键的 `sha256(前16)` 指纹（照 `deploy/scripts/c4-prjquota-window.sh:232` 那套：在目标机上算，值不出机器）；
     - **不回显明文、不开 `set -x`**；
     - `--rotate` 才允许换值（默认只补缺）。
  2. `deploy/k8s/control-plane.yaml` 在 `E2B_REDIS_PASSWORD` 那组 env（`:150-182`）旁加：

     ```yaml
     - name: E2B_SECRET_MASTER_KEY
       valueFrom:
         secretKeyRef:
           name: e2b-secrets
           key: E2B_SECRET_MASTER_KEY
           optional: true        # 兼容尚未升级的 Secret：缺键时仍然起，但要打告警
     - name: E2B_SECRET_MASTER_KEYS
       valueFrom:
         secretKeyRef:
           name: e2b-secrets
           key: E2B_SECRET_MASTER_KEYS
           optional: true
     ```

     （`optional: true` 是刻意的过渡态：先 apply 清单、再跑 `secrets.sh` 补键，两件事顺序无关都不至于让 CP 起不来；键补上后 `kubectl rollout restart deploy/control-plane` 让 CP 真正读到。）
  3. `deploy/k8s-k0s/README.md` 的部署顺序里，把"密钥"那一步从 `docs/k8s-deployment.md:51-56` 的手工命令改成 `deploy/k8s-k0s/secrets.sh`，并说明它幂等。

- [ ] **Step 4: 跑测试 + 看清单差异，确认通过**

  Run: `bash -n deploy/k8s-k0s/secrets.sh && tmp/testenv/bin/python -m pytest tests/unit/test_k0s_secrets_script.py -q`

  Run: `KUBECONFIG="$PWD/tmp/k0s/kubeconfig" DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl -n sandlock diff -f -`

  Expected: 差异只有 `control-plane` 的两个新 env（`optional: true`），没有别的行。

- [ ] **Step 5: 上线并在集群上验收（零窗口，但会滚 CP）**

  ```bash
  export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
  deploy/k8s-k0s/secrets.sh                      # 只补缺，打印指纹
  KUBECONFIG="$PWD/tmp/k0s/kubeconfig" deploy/k8s-k0s/apply.sh
  kubectl -n sandlock rollout status deploy/control-plane --timeout=300s
  kubectl -n sandlock logs deploy/control-plane --tail=200 | rg -n "master key|secret" | head
  ```

  Expected：CP 启动日志**不再出现**"no master key / plaintext 降级"那条告警；`_secrets/*/secret.json` 的内容是 Fernet 密文（用 `kubectl -n sandlock exec deploy/control-plane -- head -c 40 /var/lib/e2b-sandboxes/_secrets/<id>/secret.json` 抽样，看到 `gAAAAA` 前缀即密文）；Redis 里出现 `e2b:secret:*` 键。最后：

  Run: `tmp/testenv/bin/python -m pytest tests/contract/test_secrets.py -q`

  Expected: PASS。

- [ ] **Step 6: 提交**

  ```bash
  git add deploy/k8s-k0s/secrets.sh deploy/k8s-k0s/README.md deploy/k8s/control-plane.yaml tests/unit/test_k0s_secrets_script.py
  git commit -m "O3(1/6): k8s secret 脚本化 + 开启 E2B_SECRET_MASTER_KEY（零窗口加固）"
  ```

---

### Task 2: 主 key 轮换（三拍：rotate → 全副本滚动 → finalize）

**Files:**
- Create: `deploy/k8s-k0s/rotate-secret-master.sh`
- Create: `tests/unit/test_rotate_secret_master_script.py`
- Modify: `docs/k8s-deployment.md`（新小节：凭据轮换 runbook，见 Task 5 汇总）

**Interfaces:**
- Consumes: `deploy/scripts/upgrade.sh:170-220`（compose 侧已验证的两窗算法：`E2B_SECRET_MASTER_KEY` + 旧 key 进 `E2B_SECRET_MASTER_KEYS`；`finalize` 从列表移除旧 key）、`control_plane/registry/secrets.py::rotate_master_key`（按 key 重加密已有记录）
- Produces: `deploy/k8s-k0s/rotate-secret-master.sh rotate|finalize`，语义与 compose 版一致，作用于 k8s Secret

- [ ] **Step 1: 写"会失败"的脚本契约测试**

  ```python
  def test_rotate_script_has_the_same_three_beats_as_the_compose_one():
      text = Path("deploy/k8s-k0s/rotate-secret-master.sh").read_text()
      assert "rotate" in text and "finalize" in text
      assert "E2B_SECRET_MASTER_KEYS" in text
      assert "cannot remove the current master key" in text   # 与 upgrade.sh 同一句守卫

  def test_finalize_checks_for_stale_ciphertext_before_it_is_allowed():
      text = Path("deploy/k8s-k0s/rotate-secret-master.sh").read_text()
      assert "e2b:secret:" in text          # 扫 Redis 里的 legacy 密文
      assert "rollout status" in text       # 确认没有旧副本在跑
  ```

- [ ] **Step 2: 跑它，确认失败**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_rotate_secret_master_script.py -q`

  Expected: `FileNotFoundError`。

- [ ] **Step 3: 实现脚本（把 compose 的算法原样搬过来，只换介质）**

  - `rotate`：读当前 Secret 的 `E2B_SECRET_MASTER_KEY` → 生成新值 → 新值写 `E2B_SECRET_MASTER_KEY`、旧值**追加**进 `E2B_SECRET_MASTER_KEYS` → `kubectl apply`（Secret）→ `kubectl -n sandlock rollout restart deploy/control-plane` + `rollout status`（CP 滚动时旧密文被主 key 重新加密）。
  - 守卫与 compose 版**逐句一致**：不能把"当前主 key"finalize 掉；`E2B_SECRET_MASTER_KEYS` 为空时 finalize 直接报错（"旧 key 已不在生效列表"）。
  - `finalize <旧key>`：先做两个前置检查——① `kubectl -n sandlock rollout status deploy/control-plane` 且 `deploy` 的 `observedGeneration` 与最新一致（没有旧副本在跑）；② 扫 Redis 的 `e2b:secret:*`（确认没有仍由旧 key 加密的记录），任一不满足就**拒跑**；检查通过才从 `E2B_SECRET_MASTER_KEYS` 移除旧 key。
  - 指纹打印：rotate 前后各打一次 `sha256(前16)`（只打指纹）。

- [ ] **Step 4: 在集群上演练一次（可用一次性 key 值，不做 finalize）**

  ```bash
  export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
  deploy/k8s-k0s/rotate-secret-master.sh rotate
  kubectl -n sandlock logs deploy/control-plane --tail=100 | rg -n "secret|master" | head
  # 验证：既有 secret 仍可读（GET /secrets 或既有契约用例），再决定是否 finalize
  tmp/testenv/bin/python -m pytest tests/contract/test_secrets.py -q
  ```

  Expected: rotate 后既有 secret 仍可读（两 key 并存窗口成立）；`test_secrets.py` 全绿。**不跑 finalize**（那是不可逆点，留给人批）。

- [ ] **Step 5: 提交**

  ```bash
  git add deploy/k8s-k0s/rotate-secret-master.sh tests/unit/test_rotate_secret_master_script.py
  git commit -m "O3(2/6): k8s 版 secret master key 轮换（照 compose 三拍 + finalize 前置检查）"
  ```

---

### Task 3: 有双窗的凭据——`E2B_API_KEYS` 与 `E2B_INTERNAL_API_KEY`

**Files:**
- Modify: `deploy/k8s/worker.yaml:207-213`、`deploy/k8s/control-plane.yaml:160-166`、`deploy/k8s/autoscaler.yaml:65-69`（新增 `E2B_INTERNAL_API_KEYS`，指向同一个 Secret 的键）
- Modify: `deploy/k8s-k0s/secrets.sh`（`--rotate-internal-key` 支持写 `E2B_INTERNAL_API_KEYS` 列表）
- Modify: `tests/unit/test_worker_manifest_permissions.py`（断言三个工作负载都读得到双窗列表）

**Interfaces:**
- Consumes: `envd_service/config.py:430-438`（worker 接受 `E2B_INTERNAL_API_KEYS` 里的每一个）、`control_plane/config.py:274-278`/`:378-382`（CP 侧 `all_internal_api_keys()` 同一语义）
- Produces: 一份"internal key 轮换"的 runbook（含"worker 滚动 = 杀光沙箱"的代价声明），以及机器可判定的"三处都要有双窗"断言

- [ ] **Step 1: 写会失败的测试**

  ```python
  def test_all_three_workloads_can_accept_an_old_and_a_new_internal_key(rendered):
      for kind, name in (("Deployment", "control-plane"), ("StatefulSet", "e2b-worker"), ("Deployment", "autoscaler")):
          env = _env_of(_rendered_workload(rendered, kind, name))
          assert "E2B_INTERNAL_API_KEYS" in env, f"{name} 缺双窗键位"
          assert env["E2B_INTERNAL_API_KEYS"]["valueFrom"]["secretKeyRef"]["name"] == "e2b-secrets"
  ```

- [ ] **Step 2: 跑它，确认失败**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py -q -k three_workloads`

  Expected: FAIL（今天三处只有单值 `E2B_INTERNAL_API_KEY`）。

- [ ] **Step 3: 改清单 + 扩展脚本**

  1. 三个工作负载各加一个 env，都从 `e2b-secrets` 的 `E2B_INTERNAL_API_KEYS` 键取（`optional: true`，因为轮换窗口外这个键是空的）。
  2. `secrets.sh --rotate-internal-key`：把**当前** `E2B_INTERNAL_API_KEY` 追加进 `E2B_INTERNAL_API_KEYS`，再生成新值写入 `E2B_INTERNAL_API_KEY`（与 `upgrade.sh:122-148` 同一套逻辑）。
  3. `secrets.sh --finalize-internal-key-rotation <旧key>`：从列表移除旧 key（守卫：不能移除当前主 key；列表里找不到要移除的 key 就报错退出）。

- [ ] **Step 4: 看差异 + 跑测试，确认通过**

  Run: `KUBECONFIG="$PWD/tmp/k0s/kubeconfig" DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl -n sandlock diff -f -`

  Expected: 只有三处新增 env。

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py tests/unit/test_k0s_secrets_script.py -q`

  Expected: PASS。

- [ ] **Step 5: 写 runbook（两张表）**

  **表 1：`E2B_API_KEYS`（外部 API key，客户端持有）**

  | 步 | 动作 | 影响面 | 不可逆点 |
  |---|---|---|---|
  | 1 | `secrets.sh --rotate-api-keys`：新 key **追加**进列表 | 无（两个 key 都有效） | — |
  | 2 | `kubectl -n sandlock rollout restart deploy/control-plane` + `rollout status` | 两台 CP 副本滚动；期间 API 可用（`maxUnavailable: 1`） | — |
  | 3 | 客户端切到新 key，逐个验证 | 只影响未切换的客户端 | 未切换的客户端在下一步会 401 |
  | 4 | 从列表移除旧 key + 再滚动一次 | 未切换的客户端立即 401 | **移除旧 key** |

  **表 2：`E2B_INTERNAL_API_KEY`（worker/CP/autoscaler 之间）**

  | 步 | 动作 | 影响面 | 不可逆点 |
  |---|---|---|---|
  | 1 | `secrets.sh --rotate-internal-key`（旧 key 进列表，新 key 成主 key） | 无（列表里两个都认） | — |
  | 2 | `kubectl -n sandlock rollout restart deploy/control-plane` | CP 无感（滚动） | — |
  | 3 | `kubectl -n sandlock rollout restart statefulset/e2b-worker` | **杀掉全部 running 沙箱**（沙箱是 worker pod 内进程；树与卷数据保留） | — |
  | 4 | `kubectl -n sandlock rollout restart deploy/autoscaler` | autoscaler 无感 | — |
  | 5 | `secrets.sh --finalize-internal-key-rotation <旧key>` + 滚动 CP | 旧 key 立即失效 | **finalize** |

  两张表都写进 `docs/k8s-deployment.md` 的新小节（Task 6 汇总），并注明第 3 步必须低峰/窗口。

- [ ] **Step 6: 提交**

  ```bash
  git add deploy/k8s/ deploy/k8s-k0s/secrets.sh tests/unit/test_worker_manifest_permissions.py
  git commit -m "O3(3/6): 三处工作负载支持 internal key 双窗 + API key/internal key 轮换 runbook"
  ```

---

### Task 4: 无双窗的凭据（redis 口令、quota-agent token）

**Files:**
- Modify: `docs/k8s-deployment.md`（runbook 的第三张表）
- Modify: `deploy/k8s/redis.yaml:27-37`（**仅在采纳 ACL 方案时**：`--requirepass` 之外加 `--aclfile`/启动期 `ACL SETUSER`）
- Modify: `tests/unit/test_worker_manifest_permissions.py`（"redis 认证必须来自 Secret"这条既有性质不许退化）

**Interfaces:**
- Consumes: `deploy/k8s/redis.yaml:25`（镜像 `redis:8-alpine` ⇒ 支持 ACL）、`deploy/k8s/control-plane.yaml:174-181`（`redis://:$(E2B_REDIS_PASSWORD)@redis:6379/0` 由 kubelet 展开）、`deploy/quota_agent/__main__.py:15-19`（token 缺失即 `sys.exit`，无双窗）
- Produces: 一张"必须停机"的表 + 一个"可以不删数据"的证据（`appendonly yes`）

- [ ] **Step 1: 确认现状：只有单口令，没有双口令能力**

  Run: `sed -n '25,40p' deploy/k8s/redis.yaml`

  Expected: `command: ["redis-server", "--appendonly", "yes", "--requirepass", "$(REDIS_PASSWORD)"]` —— 单个 `requirepass`，**没有** ACL 用户；CP 与 autoscaler 都用 `$(E2B_REDIS_PASSWORD)` 拼 URL（`control-plane.yaml:174-181`、`autoscaler.yaml` 的 redis URL）。redis 一重启到 CP 拿到新口令之间，共享后端（配额/节点/限流/单飞）不可用 ⇒ 建箱与路由失败，量级 10–30 s。

  Run: `rg -n "QUOTA_AGENT" deploy/k8s/ deploy/k8s-k0s/ ; echo "exit=$?"`

  Expected: 无命中（`exit=1`）——**k8s 形态今天没部署 quota-agent**（`docs/production-deployment-requirements.md` §2.4.4 W4），所以 token 轮换现在没有影响面，只需记账。

- [ ] **Step 2: 写下决策点与两种口径（先写，不实现）**

  写成第三张表：

  | 凭据 | 步骤 | 影响面 / 不可逆窗口 | 备选 |
  |---|---|---|---|
  | `E2B_REDIS_PASSWORD` | ① 择维护窗口 ② `secrets.sh --rotate-redis` ③ `kubectl -n sandlock rollout restart deploy/redis`（`appendonly yes` ⇒ **数据不丢**）④ 滚动 `deploy/control-plane` + `deploy/autoscaler` | 必然有 **10–30 s** 中断：redis 重起到 CP 拿到新口令之间，共享后端不可用（配额/节点视图/限流/单飞失效），建箱与路由失败；沙箱本身不受影响（不经过 redis） | **ACL 双用户**（推荐）：`ACL SETUSER` 建新用户 → CP/autoscaler 切 `redis://<new>:...` → 滚动 → 删旧用户 ⇒ **零停机**。代价是多一个窗口做验证，且要改 redis 的启动方式（ACL 用户必须持久化，否则重启丢） |
  | `E2B_QUOTA_AGENT_TOKEN` | 同时更新 worker 与 agent 的 Secret；**先重启 agent、再滚 worker**（顺序反了 worker 找不到 agent，但 worker 侧是降级的） | 单 token、启动即 fail-fast（`deploy/quota_agent/__main__.py:15-19`），**没有双窗**；worker 重启 = 杀沙箱 | **k8s 未部署 agent** ⇒ 现在无影响面。将来部署 agent 时必须**同时**设计双 token（列表 + 旧值窗口），别把这条留到上线当天 |

- [ ] **Step 3: 如果采纳 ACL（推荐），实现它**

  - 在 redis 里预置两个用户（`app` 与 `app_next`），CP/autoscaler 用变量拼 URL——**清单里只出现用户名，不出现口令**（口令仍走 Secret + kubelet 展开）。
  - `secrets.sh --rotate-redis` 在 ACL 形态下的语义：写新口令到 `E2B_REDIS_PASSWORD` + 把旧口令保留为 `E2B_REDIS_PASSWORD_OLD`；redis 启动时两个用户都建好。
  - 验收：轮换期间 `redis-cli -u redis://app:<新>@redis:6379 ping` = `PONG`，`redis-cli -u redis://app_next:<旧>@redis:6379 ping` = `PONG`；旧用户删除后旧口令被拒。

- [ ] **Step 4: 跑既有性质测试，确认没退化**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py tests/unit/test_k0s_secrets_script.py -q`

  Expected: PASS。另外确认"redis 的口令来自 Secret"这条没有变成明文：

  Run: `rg -n "requirepass|REDIS_PASSWORD" deploy/k8s/redis.yaml`

  Expected: 只出现 `$(REDIS_PASSWORD)` 与 `secretKeyRef`，**没有任何字面口令**。

- [ ] **Step 5: 提交**

  ```bash
  git add docs/k8s-deployment.md deploy/k8s/redis.yaml tests/unit/test_worker_manifest_permissions.py
  git commit -m "O3(4/6): redis 口令轮换口径（ACL 双用户消除中断）+ quota-agent token 记账"
  ```

---

### Task 5: 开发机上的明文文件（ACR / SSH）与长期方案

**Files:**
- Modify: `deploy/scripts/lib/helpers.sh:10-31`（加载 env 文件前先校验权限，不合格就拒绝并给出修复命令）
- Modify: `docs/security-hardening.md:196-205`（§7 的权限说法与实测对齐）
- Modify: `docs/k8s-deployment.md`（runbook 第四张表：ACR / SSH）
- Create: `tests/unit/test_env_file_permissions_guard.py`

**Interfaces:**
- Consumes: `deploy/scripts/lib/helpers.sh:29-31`（`. "$SCRIPT_DIR/bastion.env"` / `. "$SCRIPT_DIR/acr.env"`）、`deploy/scripts/build-and-push.sh:41`（`printf '%s' "$ACR_PASSWORD" | docker login ... --password-stdin`）、`deploy/scripts/upgrade.sh:86`/`:102`（目标机 `.env` 被 `chmod 600`）
- Produces: 一条"本地凭据文件必须 600，否则脚本拒绝读"的守卫；以及 ACR/SSH 的轮换步骤

- [ ] **Step 1: 确认现状（这里是文档与实测不符的地方）**

  Run: `ls -l deploy/scripts/acr.env deploy/scripts/bastion.env`

  Expected: **两个文件都是 `-rw-r--r--`（mode 644）** —— 也就是说本机的 ACR 口令与 SSH 口令是**同机其他用户可读**的。而 `docs/security-hardening.md:203-205` 写的是"明文落盘（权限 600）"：那句话对**目标机上的 `.env`** 成立（`deploy/scripts/upgrade.sh:86`/`:102` 会 `chmod 600`），对这两个**开发机文件**不成立。

  Run: `git check-ignore -v deploy/scripts/acr.env deploy/scripts/bastion.env`

  Expected: 命中 `.gitignore:11-12` —— 两个文件确实没进版本库（好），但"没进 git"不等于"别人读不到"。

- [ ] **Step 2: 写"会失败"的静态测试**

  ```python
  def test_helpers_refuses_a_world_readable_credential_file():
      text = Path("deploy/scripts/lib/helpers.sh").read_text()
      assert "600" in text                 # 期望的权限
      assert "refus" in text.lower()        # 不合格就拒绝，而不是警告后继续
  ```

- [ ] **Step 3: 加守卫（最小改动）**

  在 `deploy/scripts/lib/helpers.sh:29` 的两个 `.`（source）之前插入一个函数：对每个存在的本地凭据文件做 `stat -c %a`，不是 `600` 就打印

  ```
  refuse: <path> is mode <mode>, not 600 -- run: chmod 600 <path>
  ```

  并 `exit 1`。**不要**自动 `chmod`（那会掩盖"有人在共享目录里复制过这份文件"这件事，而且脚本可能以别的用户跑）。例外：允许通过环境变量显式绕过（`ALLOW_LOOSE_CREDENTIAL_FILES=1`），仅用于 CI 里从未落盘的口令。

- [ ] **Step 4: 修权限并跑测试**

  ```bash
  chmod 600 deploy/scripts/acr.env deploy/scripts/bastion.env
  ls -l deploy/scripts/acr.env deploy/scripts/bastion.env
  tmp/testenv/bin/python -m pytest tests/unit/test_env_file_permissions_guard.py -q
  bash -n deploy/scripts/lib/helpers.sh && echo "helpers syntax ok"
  ```

  Expected: 两个文件变成 `-rw-------`；测试 PASS；`helpers.sh` 语法通过。再跑一次 `deploy/scripts/open-cluster-tunnel.sh --check`（只读自检）确认加载路径没被打断。

- [ ] **Step 5: 写 ACR / SSH 的轮换表并提交**

  | 凭据 | 步骤 | 影响面 | 不可逆点 |
  |---|---|---|---|
  | ACR 推送凭据（`ACR_USERNAME`/`ACR_PASSWORD`） | ① 在云上新建一份专用凭据（RAM 子账号/AKR）② 更新 `deploy/scripts/acr.env`（600）③ `./deploy/scripts/build-and-push.sh` 验证 push ④ 若开私有拉取再同步 `E2B_IMAGE_REGISTRY_*`（k8s 未设，见 `control-plane.yaml:192` 注）⑤ 删旧凭据 | 只影响构建/推送与模板镜像拉取；运行中的构建在旧凭据删掉后会失败（可重试）。**集群运行时不受影响**（k8s 是匿名拉取） | **删除旧凭据** |
  | SSH 私钥 / 口令（`bastion.env`） | ① 在跳板机与两节点的 `authorized_keys` 追加新公钥 ② 更新 `bastion.env` 的 `SSH_KEY`/`SSH_PASSPHRASE` ③ 跑一次 `deploy/scripts/open-cluster-tunnel.sh` + 一次 `DRY_RUN=1 deploy/k8s-k0s/apply.sh` ④ 移除旧公钥 | 只影响运维通道（集群内不受影响） | **移除旧公钥**（之后未更新的本机失去部署能力） |

  ```bash
  git add deploy/scripts/lib/helpers.sh docs/security-hardening.md docs/k8s-deployment.md tests/unit/test_env_file_permissions_guard.py
  git commit -m "O3(5/6): 本地凭据文件权限守卫（644→600）+ ACR/SSH 轮换表"
  ```

---

### Task 6: 统一验收 + 收口文档

**Files:**
- Modify: `docs/k8s-deployment.md`（新增 `## 凭据管理` 一节：四张表 + 指纹对账 + 验收命令）
- Modify: `docs/security-hardening.md:196-205`（§7 收口：降级态已关闭、权限说法与实测对齐）
- Modify: `docs/open-issues.md:52`（O1/O2/O3 行）

**Interfaces:**
- Consumes: Task 1–5
- Produces: 一份"照着跑就能确认凭据状态"的对账方式

- [ ] **Step 1: 写统一验收（指纹对账 + 功能回归）**

  ```bash
  export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
  deploy/k8s-k0s/secrets.sh --fingerprint          # 只打印，不改动
  kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data}' | wc -c   # 只报大小，不回贴内容
  redis-cli -h redis -a "$E2B_REDIS_PASSWORD" ping   # 经 kubectl exec 到 redis pod 内执行
  E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000 python deploy/scripts/deployment_smoke.py
  E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000 python deploy/scripts/multinode_smoke.py
  tmp/testenv/bin/python -m pytest tests/contract/test_secrets.py tests/unit/test_k0s_secrets_script.py tests/unit/test_rotate_secret_master_script.py tests/unit/test_env_file_permissions_guard.py -q
  ```

  Expected：指纹清单打印出五个键（`E2B_API_KEYS`、`E2B_INTERNAL_API_KEY`、`E2B_REDIS_PASSWORD`、`E2B_SECRET_MASTER_KEY`、`E2B_INTERNAL_API_KEYS`）；未轮换的键指纹**逐字不变**；`redis-cli ping` = `PONG`；两条冒烟全绿；pytest 全 PASS。

- [ ] **Step 2: 收口 §7 与 open-issues**

  1. `docs/security-hardening.md:196-205`：把"未配置 master key 时保持降级"改成"**k8s 形态已不再降级**（2026-09-26 起 `E2B_SECRET_MASTER_KEY` 由 `deploy/k8s-k0s/secrets.sh` 注入）；compose 形态仍保留降级路径"；把本地文件权限那句改成实测口径（目标机 `.env` 600；开发机的 `acr.env`/`bastion.env` 由 `helpers.sh` 的守卫强制 600）。
  2. `docs/open-issues.md:52`（O1/O2/O3 行）：O3 状态从不复核改成"已收口（2026-09-26）"，出处指向 `docs/k8s-deployment.md` 的凭据管理节。

- [ ] **Step 3: 提交**

  ```bash
  git add docs/k8s-deployment.md docs/security-hardening.md docs/open-issues.md
  git commit -m "O3(6/6): 凭据管理与轮换 runbook 收口（四张表 + 指纹对账）"
  ```

---

## 验收判据（怎么算这条收口了）

1. **降级态关闭**：k8s 上没有 master key 告警；`_secrets/*/secret.json` 是 Fernet 密文；Redis 有 `e2b:secret:*`。
2. **每个凭据都有一张表**：四张（API key / internal key / redis / ACR+SSH），每张都写了"是否双窗、要不要滚 worker、不可逆点、回滚命令、验收命令"。
3. **可对账**：`secrets.sh --fingerprint` 的五个指纹在轮换前后变化符合预期，未轮换的键**逐字不变**。
4. **没有明文外泄**：脚本/日志/Secret dump 里不出现凭据明文；`kubectl get secret e2b-secrets -o json | wc -c` 只报大小。
5. **本地文件权限**：`deploy/scripts/acr.env` 与 `deploy/scripts/bastion.env` 是 `600`，且 `helpers.sh` 会对不合格的权限**拒绝启动**（不是警告后继续）。
6. 功能不回归：`deployment_smoke.py` + `multinode_smoke.py` 全绿；`tests/contract/test_secrets.py` 全绿。

## 需人拍板 / 外部前提

1. **redis 口令轮换**：接受 10–30 s 中断，还是先做 ACL 双用户改造（推荐后者，代价是多一个窗口做验证并改 redis 启动方式）。
2. **master key 的 finalize 时点**：必须确认"没有旧副本在跑 + Redis 里没有 legacy 密文"（脚本会拒跑，但最终按下去的是人）。
3. **ACR 凭据与 KMS 的授权由谁签发**（RAM 子账号/密钥的权限边界）。
4. **是否本期就上外部密钥管理**（KMS + External Secrets / CSI secrets-store）：有外部平台依赖，且要求集群侧有可用的凭证链路；本计划的四张表在它之前就已经可用。
5. **internal key 轮换要滚 worker**（= 杀光现有沙箱）：需要窗口批准，建议与 N27 的迁移窗口合并做一次。
