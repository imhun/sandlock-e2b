# O2 入口 TLS / 代理层收口实施计划

> **⚠️ 本轮搁置（2026-09-26 用户裁定）**：本计划**不执行**，原文保留备用。
> 这一条本质是翻 N29④ 早已定下的"不改入口、靠异步绕过"，用户裁定"暂时不做"。
> **触发条件**（满足任一即回来执行）：① 入口侧真的出现同步路径/长请求撞 504；
> ② 拿到入口主机的 SSH 或控制台入口（目前只有 VIP）。裁定记录见
> `docs/superpowers/plans/2026-09-26-decisions.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"API key 与沙箱数据经公网代理明文传输"（`docs/security-hardening.md:153-157`，P0）在**部署侧**收口：先给出一个可在维护窗口执行的入口 TLS 方案（谁签证书、放哪、怎么验、怎么回滚），再给一个可选的"集群内控制面 TLS"第二窗口方案；并把**只能由人或外部系统完成**的步骤单列成清单。

**Architecture:** 分两步，可分别回滚。**第一步（必做，只动云上入口）**：在云上 nginx/VIP `172.18.78.49:3000` 前终止 TLS，同时把那个"一次 504 就把唯一 upstream 摘掉约 30 s"的行为关掉（双 upstream + `max_fails=0`），并给合法长请求留出 `proxy_read_timeout` —— 集群内保持明文 HTTP，回滚只是 `nginx -s reload`。**第二步（可选，另一个窗口）**：给集群内控制面挂 `E2B_TLS_CERT`/`E2B_TLS_KEY`（代码侧 E1.4 已完成，`control_plane/config.py:398-425`），并把 CA 分发到 worker 与 autoscaler，因为它们的 httpx 客户端没有 `verify=` 覆盖。仓库能做的产出是**两份文件**（入口配置模板 + 验证脚本）与**文档改动**；证书签发、nginx reload、VIP/DNS 由人执行。

**Tech Stack:** nginx（云上入口，本仓库改不到）、openssl（证书检查）、`deploy/scripts/gen-tls-cert.sh`（自签，仅用于集群内验证）、k8s Secret + kustomize overlay（第二步）、既有的 `deploy/scripts/deployment_smoke.py` / `multinode_smoke.py` 做回归。

## Global Constraints

- 临时文件一律放本仓库 `tmp/`（AGENTS.md），不使用系统 `/tmp`、`$TMPDIR`。
- 测试断言必须精确匹配；禁用 `toContain` / `includes` / 部分匹配；禁止新增 skip 或用 ignore 掩盖失败。
- 改了部署清单先看差异：`DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl diff -f -`。
- 任何 kubectl 都必须显式带 `KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`（判据见 `docs/deploy-clusters.md` §2）。
- 本机测试命令是 `tmp/testenv/bin/python -m pytest`（`.venv` 缺 `fakeredis`）。
- **证书与私钥永不进仓库**：仓库里只放配置模板与验证脚本；私钥只落在代理机上，权限 `600`（本计划不改这条既有规矩）。
- **入口是单点**：`.140` 一旦重启，`172.18.78.49:3000` 这条就断（`deploy/k8s-k0s/README.md:112-114`）——方案里必须把它换成双目标（`.94` + `.140`）。
- **第一步不改集群**；第二步（集群内 TLS）必须**成对**改（控制面开 TLS + 三个消费者改 URL/信任链同一次窗口），半改会让心跳断。
- 本仓库改不了云上 nginx/VIP：涉及入口的每条命令，凡是要在那台机器上执行的，都必须写在"需要人执行的步骤清单"里，不能伪装成仓库里的自动化。

---

### Task 1: 把决定与现状写进文档（含"这一步翻回了 N29④"的显式记账）

**Files:**
- Modify: `docs/security-hardening.md:153-157`（§2 P0）
- Modify: `docs/k8s-deployment.md:1852-1921`（§22.5.14 末尾）
- Modify: `docs/open-issues.md:50`（入口代理行）、`:24`（N29④ 行）
- Modify: `docs/deploy-clusters.md:101-112`（§5 入口表）

**Interfaces:**
- Consumes: `control_plane/config.py:292-300`（`E2B_TLS_CERT/KEY`）、`:398-425`（`tls_enabled` / `uvicorn_ssl_kwargs`）、`tests/contract/test_tls.py:89-142`（E1.4 的既有验收）
- Produces: 一篇写清"两步 + 各自回滚方式 + 谁来做"的文档结论；入口表里标明"明文 HTTP"与"待 TLS"

- [ ] **Step 1: 确认现状（代码侧已完成、部署侧一处都没有）**

  Run: `rg -n "E2B_TLS" deploy/ --glob '!*.md'`

  Expected: 只命中 `deploy/stack/docker-compose.prod.yml:111-112` 与 `deploy/compose/docker-compose.prod.yml:91-92`（compose 侧预留）+ `deploy/scripts/gen-tls-cert.sh` 的用法注释；**k8s 清单里一处都没有** ⇒ 集群控制面今天监听明文 HTTP。

  Run: `tmp/testenv/bin/python -m pytest tests/contract/test_tls.py -q`

  Expected: PASS（`test_https_health_200` / `test_plain_http_against_tls_port_fails` / `test_tls_pairing_validation` / `test_tls_enabled_property` 都在），这就是"代码侧 E1.4 已完成"的证据。

- [ ] **Step 2: 先写下"入口为什么是 P0、以及这一步与 N29④ 的关系"**

  在 §2 那一段（`docs/security-hardening.md:153-157`）后面加：

  > **处置（2026-09-26，分两步）**：① 在云上入口终止 TLS（外部动作，见 `docs/k8s-deployment.md` §22.5.15 的"需要人执行的步骤清单"），集群内保持明文；② 可选、随后单独窗口：给控制面挂 `E2B_TLS_CERT/KEY` 并把 CA 分发给 worker/autoscaler。
  >
  > ⚠ **这一步与 N29④ 的决定冲突，需要显式翻案**：`docs/open-issues.md` 记的是"不改入口（选项 b），靠异步形态绕过"——那一条针对的是**入口参数**（`proxy_read_timeout` / `max_fails` / `proxy_next_upstream`），前提是"现在只有 VIP、拿不到入口主机的 SSH 或控制台"。**TLS 是另一件事**：不做 TLS 就等于接受"API key 与沙箱数据明文过公网"，那是 P0 的产品决定，不是超时参数的技术决定。二者若一起做（同一个窗口），入口只需改一次。

- [ ] **Step 3: 写 §22.5.15（新小节，紧跟 §22.5.14）**

  `docs/k8s-deployment.md` 在 `#### 22.5.14` 之后新增 `#### 22.5.15 入口 TLS 与代理层参数（O2，2026-09-26）`，内容四段：

  1. **现状**：入口链路 `云上 nginx/VIP 172.18.78.49:3000 → .140:31907（NodePort）→ Service gateway 49983→3000`；指纹（MAC `ee:ff:ff:ff:ff:ff`、无 `Server:` 头、`https://172.18.78.49:3000` 不可用）记在 `docs/open-issues.md:50`；集群内同样是明文（`deploy/k8s-k0s/gateway-nodeport.yaml` 文件头自己写着"⚠ 明文 HTTP …要 TLS 就在前面加 ingress/证书"）。
  2. **第一步**：入口终止 TLS + 双 upstream + 超时参数；配置模板与本仓库的关系（模板在 `deploy/ingress/nginx-sandlock-edge.conf`，**由人拷到代理机**）。
  3. **第二步**：集群内 TLS 的四个连带面（控制面清单、worker、autoscaler、gateway 的 loopback 特例），以及"必须成对改"的纪律。
  4. **回滚**：第一步 = 恢复上一份 nginx 配置 + `nginx -s reload`；第二步 = 去掉两个 env + 三个 URL 改回 `http://` + 滚动重启。

- [ ] **Step 4: 对齐入口表与 open-issues**

  1. `docs/deploy-clusters.md` §5 的入口表：三行都加"**明文 HTTP**"标注，并在表下加一句"TLS 处置见 §22.5.15（第一步在云上入口，第二步可选在集群内）"。
  2. `docs/open-issues.md:50`（入口代理行）：保留"需要 SSH/控制台才能改"，出处加 `docs/k8s-deployment.md` §22.5.15。
  3. `docs/open-issues.md:24`（N29④ 行）：状态仍写"不做（带触发）"，但在出处里补一句"入口参数的处置现被 O2 第一步一并覆盖（若用户批准翻案）"。

- [ ] **Step 5: 提交**

  ```bash
  git add docs/security-hardening.md docs/k8s-deployment.md docs/deploy-clusters.md docs/open-issues.md
  git commit -m "O2(1/6): 入口 TLS 分两步的结论、与 N29④ 的关系、以及回滚方式写进文档"
  ```

---

### Task 2: 仓库内产出——入口配置模板（语法必须能被 `nginx -t` 通过）

**Files:**
- Create: `deploy/ingress/nginx-sandlock-edge.conf`
- Create: `tests/unit/test_ingress_template.py`

**Interfaces:**
- Consumes: `docs/k8s-deployment.md:1878-1884` 关于 `proxy_read_timeout` 与 `proxy_next_upstream` 的原文要求；`deploy/k8s-k0s/README.md:112-114` 的单点告警
- Produces: 一份可直接拷到代理机的配置模板（含双 upstream 与 TLS server block），以及"这份模板不退化"的单测

- [ ] **Step 1: 写会失败的测试**

  `tests/unit/test_ingress_template.py`（纯文本契约，精确匹配）：

  ```python
  from pathlib import Path

  TEMPLATE = Path("deploy/ingress/nginx-sandlock-edge.conf")

  def test_template_exists():
      assert TEMPLATE.is_file()

  def test_upstream_has_both_nodes_and_disables_passive_health_pullout():
      text = TEMPLATE.read_text()
      assert "server 172.18.80.94:31907" in text
      assert "server 172.18.80.140:31907" in text
      # max_fails=0 必须是 upstream 里 server 的参数（不是一条独立指令，
      # 独立写会在 nginx -t 直接报错）
      assert text.count("max_fails=0;") == 2
      for line in text.splitlines():
          assert line.strip() != "max_fails=0;"

  def test_timeouts_cover_legitimate_long_requests_and_never_retry_non_idempotent():
      text = TEMPLATE.read_text()
      assert "proxy_read_timeout   300s;" in text
      assert "proxy_next_upstream error timeout;" in text
      assert "non_idempotent" not in text

  def test_tls_block_keeps_the_plaintext_transition_port():
      text = TEMPLATE.read_text()
      assert "listen 443 ssl" in text
      assert "listen 3000;" in text          # 过渡期保留，避免旧客户端一刀切断
      assert "ssl_protocols TLSv1.2 TLSv1.3;" in text
  ```

- [ ] **Step 2: 跑它，确认失败**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_ingress_template.py -q`

  Expected: `assert False`（模板不存在）。

- [ ] **Step 3: 写模板（语法上能被 `nginx -t` 接受）**

  `deploy/ingress/nginx-sandlock-edge.conf` 的形状（**注意 `max_fails`/`fail_timeout` 是 `upstream` 里 `server` 的参数，不能当独立指令**——原草稿写成独立的 `max_fails=0;`，那样 `nginx -t` 直接报错）：

  ```nginx
  # Sandlock E2B 入口（云上 nginx/VIP）。**这份文件要人工拷到代理机**，
  # 它不在任何 k8s 清单里，本仓库也部署不到那台机器上。
  #
  # 为什么是两个 upstream：今天入口钉在 .140 上，.140 一重启这条入口就断
  # （deploy/k8s-k0s/README.md:112-114）。NodePort 本身两个节点都服务。
  upstream sandlock_gateway {
      # max_fails=0 关掉被动健康检查的"摘除"：一次合法长请求吃掉 504 之后，
      # 入口以前会把唯一 upstream 摘掉约 30 s（docs/k8s-deployment.md §22.5.14）。
      server 172.18.80.94:31907  max_fails=0;
      server 172.18.80.140:31907 max_fails=0;
      keepalive 32;
  }

  # TLS 终止（第一步）。
  server {
      listen 443 ssl;
      http2 on;
      server_name <你的域名>;

      ssl_certificate     /etc/nginx/tls/fullchain.pem;   # 权限 600 的私钥同目录
      ssl_certificate_key /etc/nginx/tls/privkey.pem;
      ssl_protocols TLSv1.2 TLSv1.3;
      ssl_session_cache shared:SSL:10m;

      location / {
          proxy_pass http://sandlock_gateway;
          proxy_http_version 1.1;
          proxy_set_header Connection "";
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-Proto https;

          proxy_connect_timeout 5s;
          proxy_send_timeout   300s;
          proxy_read_timeout   300s;   # ≥ 合法长请求（2000 文件快照 ≈ 75 s）
          proxy_next_upstream error timeout;   # 明确不加 non_idempotent
          proxy_next_upstream_tries 1;         # 不做第二次上游尝试

          client_max_body_size 0;              # 或不小于上传上限，避免与产品上限不符
      }
  }

  # 过渡期：老客户端仍在用 http://<IP>:3000。**先并存，客户端切完再删这一段。**
  server {
      listen 3000;
      server_name _;
      location / { proxy_pass http://sandlock_gateway; }
  }
  ```

  模板里凡是有 `<...>` 的地方（域名、证书路径）都必须在文件头写清"部署时替换"，并在 Step 5 的提交信息里点名——**不留没有说明的占位符**。

- [ ] **Step 4: 跑测试，确认通过**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_ingress_template.py -q`

  Expected: PASS。

  如果本机装了 nginx，再跑一次语法检查（没有也无妨，测试里的断言已经把三条硬性质钉住了）：

  Run: `command -v nginx >/dev/null && nginx -t -c "$PWD/deploy/ingress/nginx-sandlock-edge.conf" 2>&1 | tail -3 || echo "nginx 未安装，跳过语法检查（模板仍由 §22.5.15 的人工步骤在代理机上 nginx -t）"`

- [ ] **Step 5: 提交**

  ```bash
  git add deploy/ingress/nginx-sandlock-edge.conf tests/unit/test_ingress_template.py
  git commit -m "O2(2/6): 入口 nginx 模板（双 upstream + max_fails=0 + 300s 读超时 + TLS 与过渡端口）"
  ```

---

### Task 3: 仓库内产出——验证脚本（证书 + 鉴权 + 长请求 + 回滚提示）

**Files:**
- Create: `deploy/scripts/verify-ingress-tls.sh`
- Create: `tests/unit/test_verify_ingress_script.py`

**Interfaces:**
- Consumes: `openssl s_client`、`curl`、既有的 `deploy/scripts/deployment_smoke.py` / `multinode_smoke.py`（它们只认 `E2B_API_URL` / `E2B_SANDBOX_URL`）
- Produces: 一条命令给出"这次入口改动是否成立"的判词，且失败时打印回滚步骤

- [ ] **Step 1: 写会失败的测试**

  ```python
  from pathlib import Path

  SCRIPT = Path("deploy/scripts/verify-ingress-tls.sh")

  def test_script_exists_and_is_executable_checked_in_as_a_script():
      assert SCRIPT.is_file()
      assert SCRIPT.read_text().startswith("#!/usr/bin/env bash")

  def test_script_checks_certificate_auth_and_the_long_request_regression():
      text = SCRIPT.read_text()
      assert "openssl s_client -connect" in text
      assert "subjectAltName" in text
      assert "-H \"X-API-Key:" in text
      assert "snapshots?async=1" in text
      assert "deployment_smoke.py" in text and "multinode_smoke.py" in text

  def test_script_prints_the_rollback_steps():
      text = SCRIPT.read_text()
      assert "nginx -t && nginx -s reload" in text
  ```

- [ ] **Step 2: 跑它，确认失败**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_verify_ingress_script.py -q`

  Expected: `assert False`（脚本不存在）。

- [ ] **Step 3: 写脚本**

  `deploy/scripts/verify-ingress-tls.sh <域名或IP> [<API_KEY>]`，按顺序做六件事，**每一步都把原始输出打出来**（证据要能被复核）：

  1. **明文必须死**：`curl -sS -o /dev/null -w '%{http_code}' http://<host>:3000/healthz` —— 若在 TLS 切完后仍返回 200 且未配置过渡段，判为"过渡未清理"（打印提示，不判失败，因为过渡期是有意保留的）。
  2. **证书链与 SAN**：`openssl s_client -connect <host>:443 -servername <域名> </dev/null 2>/dev/null | openssl x509 -noout -subject -dates -ext subjectAltName`，断言 SAN 含域名。
  3. **健康**：`curl -sS https://<域名>/health` → 200。
  4. **鉴权**：带正确 key → 200；带错 key → 401；不带 key → 401（后两条用精确状态码断言，不用 grep 文本）。
  5. **长请求不再吃 504**：`POST /sandboxes/{id}/snapshots?async=1` 必须毫秒级返回 **202**；再 `GET /snapshots/{id}` 轮询到 `completed`。同时**同步形态**不再 504（这条是"入口参数"真正的验收点）。
  6. **两条冒烟**：`E2B_API_URL=https://<域名> E2B_SANDBOX_URL=https://<域名> python deploy/scripts/deployment_smoke.py` 与 `multinode_smoke.py` 全绿。

  脚本最后无条件打印回滚段：

  ```bash
  echo "回滚（在代理机上）：恢复上一份配置 -> nginx -t && nginx -s reload"
  echo "回滚（集群内第二步，若已做）：去掉 E2B_TLS_CERT/KEY + 三个 URL 改回 http:// + 滚动重启"
  ```

  退出码约定：1–4 步任一失败 ⇒ 非零；⑤ 失败也非零（它正是 N29④ 的判别点）；⑥ 失败非零。

- [ ] **Step 4: 语法自检 + 跑测试，确认通过**

  Run: `bash -n deploy/scripts/verify-ingress-tls.sh && echo "syntax ok"`

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_verify_ingress_script.py -q`

  Expected: `syntax ok` + PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add deploy/scripts/verify-ingress-tls.sh tests/unit/test_verify_ingress_script.py
  git commit -m "O2(3/6): 入口 TLS 验证脚本（证书/鉴权/长请求/两条冒烟/回滚提示）"
  ```

---

### Task 4: 第二步（可选窗口）——集群内 TLS 的清单改造与信任链

**Files:**
- Modify: `deploy/k8s/control-plane.yaml`（新增 `E2B_TLS_CERT`/`E2B_TLS_KEY` 两个 `secretKeyRef` + 只读挂载）
- Modify: `deploy/k8s/worker.yaml:207-213`、`deploy/k8s/autoscaler.yaml:65-69`（URL scheme 或 CA 注入）
- Modify: `deploy/k8s-k0s/`（新增 overlay patch：CA ConfigMap + `SSL_CERT_FILE`）
- Modify: `tests/unit/test_worker_manifest_permissions.py`（新增一条：三个消费者的 URL/CA 必须同进同出）

**Interfaces:**
- Consumes: `control_plane/config.py:398-425`（半配置即报错，不降级）、`envd_service/agent.py:1611`、`:1828`、`:1875`（worker→CP 的 httpx 客户端**没有** `verify=` 覆盖）、`autoscaler/control.py:23`、`:32`（autoscaler 同样是明文客户端）、`envd_service/gateway.py:154-161`（只有 gateway 对 `https://127.0.0.1|localhost` 跳过校验）
- Produces: 一份"打开集群内 TLS"的清单补丁；以及"必须成对改"的机器可判定断言

- [ ] **Step 1: 写会失败的测试**

  在 `tests/unit/test_worker_manifest_permissions.py` 新增：

  ```python
  def test_in_cluster_tls_would_need_the_ca_in_every_consumer():
      # 这条钉的是"纪律"而不是"当前状态"：一旦有人打开集群内 TLS，
      # 三个消费者（worker/autoscaler/gateway）的 URL 与 CA 必须同时出现。
      worker = _rendered_workload(rendered, "StatefulSet", "e2b-worker")
      env = {e["name"]: e.get("value") for e in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
      tls_on = env.get("E2B_CONTROL_PLANE_URL", "").startswith("https://")
      if tls_on:
          assert env.get("SSL_CERT_FILE") == "/etc/e2b-ca/ca.crt"
      else:
          assert env.get("SSL_CERT_FILE") is None
  ```

  （照 `:608-661` 既有形状渲染 overlay；这条用例在 TLS 关闭时是"零断言通过"，打开时立刻约束三个消费者——它把"半改"变成 CI 里的红。）

- [ ] **Step 2: 跑它，确认现状是"关"**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py -q -k in_cluster_tls`

  Expected: PASS（当前 `E2B_CONTROL_PLANE_URL` 是 `http://control-plane:3000`，走 `else` 分支）。这条**不需要失败**——它的价值是锁住未来的半改；Step 2 的任务是确认它现在走的是哪一支。

  Run: `rg -n "control-plane:3000" deploy/k8s/*.yaml`

  Expected: 命中 `control-plane.yaml:185`、`worker.yaml:211`、`autoscaler.yaml:23`/`:66` —— 这就是"要一起改"的四个点（外加 `gateway.yaml:1-12` 的 `targetPort` 不变）。

- [ ] **Step 3: 写清单补丁（TLS 打开时才用）**

  1. 证书来源：`kubectl -n sandlock create secret tls e2b-control-plane-tls --cert=... --key=...`（证书 SAN 必须含 `control-plane`、`control-plane.sandlock.svc`、`127.0.0.1`——`deploy/scripts/gen-tls-cert.sh deploy/compose/tls control-plane 10.0.0.5` 就是现成的自签形状，仅用于验证）。
  2. `control-plane.yaml`：两个 env（`E2B_TLS_CERT=/tls/tls.crt`、`E2B_TLS_KEY=/tls/tls.key`）+ 把 Secret 只读挂到 `/tls`（照 `deploy/compose/docker-compose.prod.yml:125` 那句注释的形状）。
  3. 消费者：`E2B_CONTROL_PLANE_URL` / `E2B_GATEWAY_URL` / `E2B_AS_CONTROL_PLANE_URL` 改 `https://control-plane:3000`；把 CA 做成 ConfigMap 挂进 worker 与 autoscaler 并设 `SSL_CERT_FILE`（httpx 认这个变量，所以**不需要**改代码里的 `httpx.AsyncClient(...)`）。
  4. gateway 的 loopback 特例保留：`envd_service/gateway.py:154-161` 只对 `https://127.0.0.1|localhost` 跳过校验，合并形态的自签证书走这条；**不要**为了省事把 `verify=False` 放到别处。

- [ ] **Step 4: 在集群上验证（可选窗口，人批）**

  ```bash
  export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
  kubectl -n sandlock create secret tls e2b-control-plane-tls --cert=<crt> --key=<key> --dry-run=client -o yaml | kubectl apply -f -
  DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl -n sandlock diff -f -
  KUBECONFIG="$PWD/tmp/k0s/kubeconfig" deploy/k8s-k0s/apply.sh
  kubectl -n sandlock exec statefulset/e2b-worker -- curl -sS https://control-plane:3000/healthz
  ```

  Expected: `diff` 只含预期行；worker 内 `curl` 返回 `{"status":"ok"}`；`kubectl -n sandlock get --raw '/api/v1/namespaces/sandlock/services/control-plane:3000/proxy/healthz'` 也可用（集群内验证不依赖入口）。随后跑 Task 3 的脚本（把域名换成 NodePort 直连地址）与两条冒烟。

- [ ] **Step 5: 提交**

  ```bash
  git add deploy/k8s/ deploy/k8s-k0s/ tests/unit/test_worker_manifest_permissions.py
  git commit -m "O2(4/6): 集群内 TLS（可选窗口）：控制面证书 + 三个消费者同进同出 + CA 注入"
  ```

---

### Task 5: 需要人 / 外部系统执行的步骤清单（写进文档，不假装是代码任务）

**Files:**
- Modify: `docs/k8s-deployment.md:1852-1921`（§22.5.15 的最后一节）
- Modify: `docs/deploy-clusters.md`（§5 入口表下方加一行指向）

**Interfaces:**
- Consumes: Task 2/3 的模板与脚本
- Produces: 一份**只有人能做**的清单（每步写清"谁、在哪台机器、验收什么、失败怎么退"）

- [ ] **Step 1: 把清单写进 §22.5.15（照抄下面的形状，替换 `<...>`）**

  ```markdown
  **需要人 / 外部系统执行（本仓库改不了）**

  | # | 谁 | 在哪 | 做什么 | 验收 | 失败怎么退 |
  |---|---|---|---|---|---|
  | 1 | 域名/DNS 负责人 | 域名控制台 | 把 `<域名>` 解析到 `172.18.78.49` | `dig +short <域名>` = 该 IP | — |
  | 2 | 证书负责人 | 云证书服务或 ACME DNS-01 | 签发 DV 证书（**80 端口已被入口占用**，HTTP-01 可能不可用，所以用 DNS-01） | `openssl x509 -noout -ext subjectAltName` 含域名 | — |
  | 3 | 入口负责人 | 代理机（VIP/nginx） | 拷入 `deploy/ingress/nginx-sandlock-edge.conf`，替换 `<域名>`/证书路径，私钥权限 `600` | `nginx -t` 通过 | 不 reload，原配置不动 |
  | 4 | 入口负责人 | 代理机 | `nginx -s reload`（新开 443，3000 过渡段保留） | `curl -sS https://<域名>/health` = 200 | 恢复上一份配置 + reload（秒级） |
  | 5 | 入口负责人 | 代理机 | 确认双 upstream 生效：停掉一个节点上的 CP pod 后入口仍可用 | 连续请求 0 个 5xx | 把 upstream 加回第二行 |
  | 6 | 客户端负责人 | 各调用方 | 切到 `https://<域名>`（`E2B_API_URL` / `E2B_SANDBOX_URL`） | Task 3 的脚本全绿 | 切回 `http://…:3000` |
  | 7 | 入口负责人 | 代理机 | 客户端切完后删掉 3000 过渡段并 reload | `curl http://<域名>:3000/healthz` 不再 200 | 加回过渡段 |
  | 8 | 运维 | 集群（可选第二步） | 注入 CA（ConfigMap/Secret）到 worker 与 autoscaler | `kubectl exec ... curl -sS https://control-plane:3000/healthz` | 撤 CA + URL 回 http 并滚动重启 |
  ```

- [ ] **Step 2: 校验清单里的每条命令都真的可执行**

  Run: `rg -n "sandlock-edge.conf|verify-ingress-tls.sh" docs/`

  Expected: 两处文件名都命中（模板与脚本在文档里被点名，读者能找到它们）。

  Run: `ls -l deploy/ingress/nginx-sandlock-edge.conf deploy/scripts/verify-ingress-tls.sh`

  Expected: 两个文件都在；脚本可执行位已设（`chmod +x`，与 `deploy/scripts/` 里其它脚本一致）。

- [ ] **Step 3: 提交**

  ```bash
  git add docs/k8s-deployment.md docs/deploy-clusters.md
  git commit -m "O2(5/6): 把必须由人执行的入口步骤列成清单"
  ```

---

### Task 6: 验收（第一步做完就算 O2 的 P0 关掉）

**Files:**
- Modify: `docs/security-hardening.md:153-157`（把 §2 结论改成"已处置"，并保留第二条可选项）

**Interfaces:**
- Consumes: Task 1–5
- Produces: 一条可复跑的判据（`deploy/scripts/verify-ingress-tls.sh` 全绿 + 两条冒烟）

- [ ] **Step 1: 跑完整验收（第一步做完之后）**

  ```bash
  deploy/scripts/verify-ingress-tls.sh <域名> "$E2B_API_KEY"
  E2B_API_URL=https://<域名> E2B_SANDBOX_URL=https://<域名> python deploy/scripts/deployment_smoke.py
  E2B_API_URL=https://<域名> E2B_SANDBOX_URL=https://<域名> python deploy/scripts/multinode_smoke.py
  ```

  Expected: 脚本打印的六段全绿；两条冒烟通过且**没有任何一次 504**（脚本第 5 步会显式报这条）。

- [ ] **Step 2: 记录证据并收口 §2**

  把脚本输出（脱敏后）存 `tmp/k0s/o2-ingress-acceptance.log`，在 §2 写：验收日期、证书 SAN、`proxy_read_timeout` 的取值、双 upstream 的实测结果、以及"第二步（集群内 TLS）是否做"。

- [ ] **Step 3: 提交**

  ```bash
  git add docs/security-hardening.md
  git commit -m "O2(6/6): 入口 TLS 验收与 P0 收口记录"
  ```

---

## 验收判据（怎么算这条收口了）

1. **P0 关掉**：`https://<域名>/health` 与带 key 的 `/sandboxes` 都是 200；错 key / 无 key 是 401；**明文路径在过渡期结束后不再可用**（或明确记录"过渡期仍在"并有人负责关）。
2. **长请求不被入口打死**：`POST /sandboxes/{id}/snapshots?async=1` 毫秒级 202；同步形态不再 504；同一次改动后**不再**出现"一次 504 摘掉 upstream 约 30 s"（双 upstream + `max_fails=0` 的现场验证）。
3. **单点消除**：停掉任一节点上的控制面后入口仍可用。
4. **可回滚**：代理机恢复上一份配置 + reload 即回到改动前；第二步的回滚是"去 env + URL 改回 http + 滚动重启"，且**成对**。
5. 仓库内产出可复查：`tests/unit/test_ingress_template.py`、`tests/unit/test_verify_ingress_script.py` 全绿；`deploy/scripts/verify-ingress-tls.sh` 可执行。
6. 文档里"需要人执行"的步骤单独成表，不含任何"仓库会自动完成"的假承诺。

## 需人拍板 / 外部前提

1. **是否翻 N29④ 的决定**（当轮定的是"不改入口、靠异步绕过"）。TLS 与入口参数是两件事，但同一个窗口改一次最省；需要用户明确"这一步批准做"。
2. **443 新端口 vs 3000 直接上 TLS**：影响所有现存客户端脚本的 URL（临时端口并存是更稳的过渡，代价是要有人负责关掉过渡段）。
3. **谁签证书、谁续期**（云证书服务 / ACME DNS-01），以及续期后由谁 reload。
4. **是否本期就做第二步（集群内 TLS）**：它需要 worker 与 autoscaler 一起注入 CA，且回滚可回滚性更差（必须成对改）。
5. **入口主机的访问权**：没有 SSH 或控制台入口时，第一步整体做不了（这正是 `docs/open-issues.md:50` 记的那条触发条件）。
