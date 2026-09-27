# §10.5 两条遗留结论收口（存储锁门槛 + 非 root worker 约束）实施计划

> **执行状态（2026-09-27 更新）**：**已收口** —— 两条结论已落 `docs/production-deployment-requirements.md` §5.4(a)/(b)（NFSv4.0 的 `flock` 是唯一跨节点互斥；网络文件系统上 worker 必须 `runAsUser: 0`），§10.5/§13.3/open-issues/task-backlog 指针对齐。
> **仍有效的决定**：这两条**已升级为基线显式约束**（换 NAS 或换挂载参数时按 §5.4(a) 复核）。**已作废的假设**：无（纯文档收口）。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `docs/k8s-deployment.md` §10.5 里那两条"要不要写进门槛"的悬置问题各用一个**可直接粘贴的段落**结掉，并落到唯一一处（`docs/production-deployment-requirements.md` 新增 §5.4），随后把 §10.5 / §13.3 / `docs/open-issues.md` / `docs/task-backlog.md` 的指针对齐。

**Architecture:** 纯文档收口，不改代码、不改清单。两条都不是新结论，而是把**已经实测过**的事写成**门槛**：① 多副本共用一份 base 时，`flock` 必须跨节点互斥——本集群只有 NFSv4.0 成立，v3+`nolock` 只是本机锁，跨节点**没有 CAS**，所以任何分布式单飞只能实现为"锁 + 记录"；② 「非 root worker」只在节点本地盘成立，workspace/volume 落到网络文件系统上时 worker 必须 `runAsUser: 0` + `runAsGroup: <worker gid>`，因为 `CAP_CHOWN` 不过网（NFS 只认 AUTH_SYS 凭据里的 uid）。§5.4 是这两条的家：它是"共享存储形态"那一节的延续，找存储选型的人会读到这里。

**Tech Stack:** Markdown 文档（`docs/k8s-deployment.md`、`docs/production-deployment-requirements.md`、`docs/open-issues.md`、`docs/task-backlog.md`）、既有单测 `tests/unit/test_worker_manifest_permissions.py`（本计划只是让文档与它互指，不改它）。

## Global Constraints

- 临时文件一律放本仓库 `tmp/`（AGENTS.md），不使用系统 `/tmp`、`$TMPDIR`。
- 测试断言必须精确匹配；禁用 `toContain` / `includes` / 部分匹配；禁止新增 skip 或用 ignore 掩盖失败。
- 改了部署清单先看差异：`DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl diff -f -`。
- 任何 kubectl 都必须显式带 `KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`（判据见 `docs/deploy-clusters.md` §2）。
- 本机测试命令是 `tmp/testenv/bin/python -m pytest`（`.venv` 缺 `fakeredis`）。
- **本计划零代码改动、零清单改动**：唯一允许改的是四个 `.md` 文件；任何"顺手把注释写进 YAML"的冲动都留给各自的开单（`worker-root.patch.yaml` 已经写着这段实测，见 `deploy/k8s-k0s/worker-root.patch.yaml:1-27`）。
- 两条结论都必须写成**门槛**（"必须 / 前置条件"），不写成"建议"或"注意"；同时必须写出**触发复核的时机**（换 NAS / 换挂载参数 / 换存储类型）。

---

### Task 1: (a) v3/`nolock` 与 v4 的锁语义差异 → 写成硬门槛

**Files:**
- Modify: `docs/production-deployment-requirements.md:1643-1711`（§5 末尾新增 `### 5.4 存储选型门槛`）
- Modify: `docs/k8s-deployment.md:414-431`（§10.5 第 3 条）
- Modify: `docs/k8s-deployment.md:634-663`（§13.3 的 F5 段落改成指向 §5.4）

**Interfaces:**
- Consumes: `docs/k8s-k0s/storage-nas.yaml:12-19` 的实测（v3+服务端锁 `ESTALE`；v3+`nolock` 只在本机互斥；v4.0 跨节点互斥成立；NAS 不支持 v4.1/4.2）、§13.3 的清单前置
- Produces: §5.4 里的"(a) 分布式锁门槛"段落（下面 Step 2 的引用块，一字不改地落地）

- [ ] **Step 1: 先确认现有三处的原文（Step 3 要改的就是它们）**

  Run: `rg -n "nolock|NFSv4.0|NFSv4" docs/k8s-deployment.md docs/production-deployment-requirements.md docs/deploy-clusters.md`

  Expected: 命中 `docs/k8s-k0s/storage-nas.yaml` 之外的三处叙述——§13.3 的"前置是存储的锁必须跨节点"、`docs/production-deployment-requirements.md` §5.1 的"各节点挂载选项必须一致"、以及 §10.5 第 3 条的留白；**没有任何一处**把它写成"门槛/前置条件"。

  Run: `sed -n '12,19p' deploy/k8s-k0s/storage-nas.yaml`

  Expected: 看到四行实测（v3+lock 全 `ESTALE`、v3+`nolock` 只单机有效、v4.0 跨节点被挡、v4.1/4.2 `EPROTONOSUPPORT`）——这就是 (a) 的证据来源，段落里必须引用它。

- [ ] **Step 2: 写出结论草稿（Step 3 就贴这段）**

  > **(a) 存储选型门槛（硬，N13/F5）：多副本 worker 共用一份 `E2B_WORKSPACE_BASE` 时，`<base>/.uid_pool.lock` 上的 `flock` 必须跨节点互斥。** 否则两个副本会各自把同一个 uid 发给不同沙箱，E3.2 的每沙箱 uid 隔离会在无人察觉的情况下失效——这不是"降级"，是**静默失效**。本集群实测（`deploy/k8s-k0s/storage-nas.yaml:12-19`）：**只有 NFSv4.0 成立**；`NFSv3 + 服务端锁` 在这台 NAS 的客户端上直接 `ESTALE`；`NFSv3 + nolock` 的锁**只是本机锁**（同机互斥、跨机不互斥）。跨节点**没有 CAS 原语**，因此任何分布式单飞（uid 分配、TTL 扫描、快照拷贝认领、限流窗口）只能实现为"**锁 + 记录**"，不能依赖 `O_EXCL` 之类的原子性假设。各节点的挂载选项（`vers`、`nolock`、`sec`）**必须一致**；**换 NAS 或换挂载参数时必须随部署复核这一条**。它同时是 `replicas ≥ 2` 的前置条件——不是可选优化。

- [ ] **Step 3: 落地**

  1. `docs/production-deployment-requirements.md` 在 §5.3 之后（文件末尾）新增：

     ```markdown
     ### 5.4 存储选型门槛（硬）

     <把 Step 2 的引用块整段贴进来>
     ```

     （§5.4 的第二段留给 Task 2 的 (b)；两段同属这一节，因为它们是同一个问题的两面：**当 workspace/volume 落在网络文件系统上时，哪些前提必须成立**。）
  2. `docs/k8s-deployment.md` §10.5 第 3 条：把"v3/`nolock` 与 v4 的锁语义差异要不要写进存储选型门槛"划掉，写成"✅ 已写成硬门槛（2026-09-26）：`docs/production-deployment-requirements.md` §5.4(a)"。
  3. `docs/k8s-deployment.md` §13.3 现有那段"前置是存储的锁必须跨节点"保留原文（它是现场叙述），末尾加一句"（门槛与复核时机：`docs/production-deployment-requirements.md` §5.4(a)）"。

- [ ] **Step 4: 校验**

  Run: `rg -n "nolock" docs/k8s-deployment.md docs/production-deployment-requirements.md`

  Expected: 命中新门槛文字（§5.4(a)）+ §13.3 的现场叙述 + §10.5 的指向；不再有"要不要写进门槛"这种留白。

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py -q`

  Expected: PASS（**本任务不该动它**——这条是"文档没改坏清单断言"的证据）。

- [ ] **Step 5: 提交**

  ```bash
  git add docs/production-deployment-requirements.md docs/k8s-deployment.md
  git commit -m "§10.5(a): 跨节点 flock 前置写成硬门槛（NFSv4.0 / 无 CAS）"
  ```

---

### Task 2: (b) 「非 root worker + 网络文件系统」→ 升级为基线显式约束

**Files:**
- Modify: `docs/production-deployment-requirements.md:1643-1711`（§5.4 第二段）
- Modify: `docs/k8s-deployment.md:414-431`（§10.5 第 3 条的后半）
- Modify: `docs/production-deployment-requirements.md:176-257`（§2.4.1 特权最小集：加一句指向 §5.4(b)）
- Modify: `deploy/k8s-k0s/worker-root.patch.yaml:1-27`（把末尾"通用的处置见 docs/k8s-deployment.md 与 task-backlog"改成指向 §5.4(b)）

**Interfaces:**
- Consumes: `tests/unit/test_worker_manifest_permissions.py:649-660`（断言 k0s overlay 真的给出 `runAsUser: 0` / `runAsGroup: 65534`）、`deploy/k8s-k0s/worker-root.patch.yaml` 的实测记录
- Produces: §5.4(b) 段落（Step 2 的引用块）+ 一处可判定的复核时机

- [ ] **Step 1: 确认现状：这条约束今天只以"overlay 差异"的形式存在**

  Run: `sed -n '1,32p' deploy/k8s-k0s/worker-root.patch.yaml`

  Expected: 看到实测结论（uid 65534 + `cap_chown` 的 broker 做 `chown 10000:65534` → **EPERM**；同挂载 root 做同样 chown → 成功）与那句"这不是 k0s 特有的：基线的『非 root worker + RWX PVC』组合在任何网络文件系统上都不成立"。

  Run: `rg -n "runAsUser|runAsGroup" tests/unit/test_worker_manifest_permissions.py | sed -n '1,6p'`

  Expected: `:659-660` 两条精确断言（`security["runAsUser"] == 0` / `security["runAsGroup"] == 65534`）——这就是 (b) 的**机器可判定**形式，段落里必须点名它。

- [ ] **Step 2: 写出结论草稿（Step 3 就贴这段）**

  > **(b) 基线约束（F4）：非 root worker 只适用于节点本地盘。** 当 workspace/volume 落在**网络文件系统**（NFS/CephFS/…）上时，worker 必须 `runAsUser: 0` 且 `runAsGroup = <worker gid>`（本集群为 `0:65534`）。原因：route B/c1 建箱时要把沙箱树交给池里的 uid（`0770 owner=<sandbox uid> group=<worker gid>`，E3.2），这一步由带 `cap_chown` 的 broker（`e2b-maint`）执行，而 **`CAP_CHOWN` 不过网**——NFS 服务端只读 AUTH_SYS 凭据里的 uid，不看你客户端的能力；实测（`deploy/k8s-k0s/worker-root.patch.yaml:1-27`）：uid 65534 的 broker 做 `chown 10000:65534` **EPERM**，同一挂载上 root 做同样 chown 成功。并且必须**保留 worker 的 gid** 作为属组，否则 worker 进不了 `0770 group=<worker gid>` 的沙箱树（这台 NAS 对 uid 0 也不给越权读别的 uid 的 `0600` 文件）**（2026-09-28 更正：这句已被 C2 P0 探针推翻 —— 同一挂载上 uid 0 能越权读 `0600`/进 `0700`/删条目，见 `docs/production-deployment-requirements.md` §5.4(b) 与 `docs/c2-ownership-frontload.md` §7；"保留 worker 的 gid" 的理由要读成"worker 靠组位进树"，不是"uid 0 也进不去"）**。安全代价是 worker 成为**可信中介**（沙箱仍跑在自己的池 uid 下，隔离模型不变），前提是导出允许 root 访问（`no_root_squash`）。该约束已由单测钉住（`tests/unit/test_worker_manifest_permissions.py:649-660`），因此**基线的 `deploy/k8s/worker.yaml` 在改用网络存储时必须同步这一设置**，不能沿用默认的非 root 形态；托管集群若用块存储/节点本地盘，可以保持非 root。**复核时机**：换共享存储类型（本地盘 ↔ 网络文件系统）、换导出权限（`no_root_squash` 变化）时必须回来核这一条。

- [ ] **Step 3: 落地**

  1. `docs/production-deployment-requirements.md` §5.4 追加第二段（标题写成 `**(b) ...**` 的形态，与 (a) 并列），并在 §5.4 开头用一句话说明这一节是"网络文件系统形态下的硬前提"。
  2. `docs/k8s-deployment.md` §10.5 第 3 条：把"F4 那条『非 root worker 与网络文件系统不兼容』是否要升级成基线的显式约束"划掉，写成"✅ 已升级为基线显式约束（2026-09-26）：§5.4(b)"。
  3. `docs/production-deployment-requirements.md` §2.4.1（特权最小集，`:176` 一带）末尾加一句指向：**非 root 形态的前提是节点本地盘**；网络文件系统上按 §5.4(b) 走 root + worker gid。
  4. `deploy/k8s-k0s/worker-root.patch.yaml` 文件头注释里"通用的处置见 `docs/k8s-deployment.md` 与 `task-backlog`"改成"通用门槛见 `docs/production-deployment-requirements.md` §5.4(b)"（只改这一行注释，不动 op 内容）。

- [ ] **Step 4: 校验**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py -q`

  Expected: PASS（单测断言的对象是 overlay 的 op 内容，注释行不参与断言；这一跑就是"没改坏"的证据）。

  Run: `rg -n "runAsUser: 0|§5.4" docs/production-deployment-requirements.md docs/k8s-deployment.md | head`

  Expected: §5.4(b) 命中，且 §10.5 指向它。

- [ ] **Step 5: 提交**

  ```bash
  git add docs/production-deployment-requirements.md docs/k8s-deployment.md deploy/k8s-k0s/worker-root.patch.yaml
  git commit -m "§10.5(b): 非 root worker 只适用于本地盘，升级为基线显式约束"
  ```

---

### Task 3: 收口账（§10.5 的三条遗留一起结、F7 顺手划掉）

**Files:**
- Modify: `docs/k8s-deployment.md:414-431`（§10.5 第 3 条整体重写）
- Modify: `docs/open-issues.md:28`（§10.5 遗留行）、`:51`（F5 存储锁行）
- Modify: `docs/k8s-deployment.md:392`（F7 行）、`docs/task-backlog.md:119`（N20 行，若仍列在未完成清单里）

**Interfaces:**
- Consumes: Task 1/2 的落地结果
- Produces: §10.5 不再有"收尾遗留"这一句；open-issues 的 "§10.5 遗留" 行状态改成"已收口"

- [ ] **Step 1: 找出这一条里的**第三**个悬置项（它其实早就做完了）**

  Run: `rg -n "F7" docs/k8s-deployment.md`

  Expected: 命中两处——§10.5 第 3 条把它列成"收尾遗留：worker 的稳定 node id（F7）"，而 §13.4 与 §15 明确写着"~~N20~~/F7 ✅ 2026-09-18 已修（换成 StatefulSet）"。也就是说 §10.5 第 3 条今天把**一件已完成的事**和两件待决策的事混在一条里。

- [ ] **Step 2: 跑一次确认现状（这条"现状与结论不符"就是本步骤要抓的）**

  Run: `rg -n "statefulset|StatefulSet" docs/k8s-deployment.md | sed -n '1,8p'`

  Expected: 看到 §15 整节 + `test_k8s_worker_is_a_statefulset_so_its_node_ids_survive_a_restart`；`kubectl -n sandlock get statefulset e2b-worker`（带 `KUBECONFIG`）应显示 2/2。

  Run: `KUBECONFIG="$PWD/tmp/k0s/kubeconfig" kubectl -n sandlock get statefulset e2b-worker`

  Expected: `e2b-worker   2/2`。若集群不可达（无隧道），跳过这条，只以仓库里的记录为准——**不要**因此把任务标成未完成。

- [ ] **Step 3: 重写 §10.5 第 3 条**

  改成三段（每段一句结论 + 指向）：

  ```markdown
  3. 收尾遗留：**已全部收口（2026-09-26）**。
     * ~~worker 的稳定 node id（F7）~~：✅ 2026-09-18 已修（StatefulSet，见 §15）；F7 在 §13.4 的旧列表里也早已划掉，本条不再重复记账。
     * v3/`nolock` 与 v4 的锁语义差异 → 已写成**硬门槛**：`docs/production-deployment-requirements.md` §5.4(a)。
     * F4「非 root worker 与网络文件系统不兼容」→ 已升级为**基线显式约束**：§5.4(b)。
  ```

- [ ] **Step 4: 对齐 open-issues 与 backlog**

  1. `docs/open-issues.md:28`（§10.5 遗留行）：状态 `待决策` → `已收口（2026-09-26）`，下一步改成"两条结论已落 §5.4(a)/(b)；换 NAS 或换挂载参数时按 §5.4(a) 复核"。
  2. `docs/open-issues.md:51`（F5 存储锁行）：保留"约束（已满足）"，出处加上 `§5.4(a)`。
  3. `docs/k8s-deployment.md:392` 的 F7 行与 `docs/task-backlog.md:119` 的 N20 行：确认都已标 ✅（N20 行已是 `~~N20~~` + ✅）；若仍有残留的"未完成"表述，按 §15 的实测补齐（这是纯记账对齐，不加新结论）。

- [ ] **Step 5: 校验 + 提交**

  Run: `rg -n "待决策|要不要" docs/k8s-deployment.md docs/open-issues.md | rg -n "§10.5|nolock|F4|F5"`

  Expected: 无命中（§10.5 相关的悬置表述清零）。

  ```bash
  git add docs/k8s-deployment.md docs/open-issues.md docs/task-backlog.md
  git commit -m "§10.5: 三条收尾遗留全部收口（F7 早已完成、两条写成门槛）"
  ```

---

### Task 4: 验收（门槛可被检索、可被判据复核）

**Files:**
- Modify: `docs/production-deployment-requirements.md`（§5.4 的"验收"小节，或 §4 验收清单新增一条）

**Interfaces:**
- Consumes: Task 1–3
- Produces: 一条可执行的复核清单（人照着做就能确认当前部署是否满足门槛）

- [ ] **Step 1: 给 §5.4 补"怎么核"（三条命令，写进文档）**

  ```markdown
  **§5.4 的复核（换存储/换挂载参数时跑一遍）**：

  ```bash
  # (a) 跨节点锁真的互斥：两个节点各持锁一次
  kubectl -n sandlock exec e2b-worker-0 -- flock /var/lib/e2b-sandboxes/.uid_pool.lock -c 'sleep 20' &
  kubectl -n sandlock exec e2b-worker-1 -- flock -n /var/lib/e2b-sandboxes/.uid_pool.lock -c true   # 必须非零退出

  # (b) worker 的身份与属组
  kubectl -n sandlock get statefulset e2b-worker -o jsonpath='{.spec.template.spec.containers[0].securityContext}'
  # 期望：{"runAsUser":0,"runAsGroup":65534,...}；并用一次 Sandbox.create() 验证 0770 的池 uid 树能建出来
  ```
  ```

  （文档里的命令要**照抄本机实际跑过的形状**：本计划的执行者跑过之后把真实输出贴进 §5.4 的验收小节。）

- [ ] **Step 2: 真跑一次 (a)（这是本计划唯一一条"对集群做操作"的验收，只读——两次 exec + flock，不写任何持久状态）**

  Run: `export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"; kubectl -n sandlock exec e2b-worker-0 -- flock /var/lib/e2b-sandboxes/.uid_pool.lock -c 'sleep 20' & sleep 3; kubectl -n sandlock exec e2b-worker-1 -- flock -n /var/lib/e2b-sandboxes/.uid_pool.lock -c true; echo "exit=$?"`

  Expected: 第二个 `flock -n` **非零退出**（`exit=1`）——这就是 (a) 门槛成立的现场证据。若为 0，**立刻停手**：这说明跨节点锁在当前部署上不成立，多副本形态本身有问题（回到 §13.3 的清单前置）。

- [ ] **Step 3: 提交**

  ```bash
  git add docs/production-deployment-requirements.md
  git commit -m "§5.4: 补上门槛的复核命令与现场证据"
  ```

---

## 验收判据（怎么算这条收口了）

1. `docs/production-deployment-requirements.md` §5.4 存在，且 (a)(b) 两段都是**门槛句式**（含"必须/前置条件"与"复核时机"），不是"建议"。
2. `docs/k8s-deployment.md` §10.5 不再有"收尾遗留"的悬置表述；F7 被明确记为早已完成（指向 §15），另两条指向 §5.4。
3. 可检索：`rg -n "nolock" docs/k8s-deployment.md docs/production-deployment-requirements.md` 命中新门槛文字；`rg -n "§5.4" docs/` 至少命中 §2.4.1、§10.5、§13.3、§5.4 自身。
4. 机器可判定：`tests/unit/test_worker_manifest_permissions.py` 全绿（(b) 的断言仍在）；(a) 的现场判据是"两个节点抢同一把锁，第二个必须失败"。
5. `docs/open-issues.md:28` 的"§10.5 遗留"行不再是"待决策"。

## 需人拍板 / 外部前提

1. **运维承诺**：贴 (a) 需要运维认下"将来换 NAS 不得用 v3/`nolock`"这条承诺（文档写了门槛，但换存储是人的决定）。
2. **托管集群的例外**：如果将来把同一套清单搬到托管集群（块存储/节点本地盘），(b) 不适用——是否在 §5.4(b) 里显式写出这个例外，由部署负责人定（本计划已按"写出来"处理）。
3. **§5.4 归属**：本计划把它加在 `docs/production-deployment-requirements.md` 的 §5（NFS 共享存储形态）之下；若更愿意放进 `docs/k8s-deployment.md`，只需改落点、不改文字。
