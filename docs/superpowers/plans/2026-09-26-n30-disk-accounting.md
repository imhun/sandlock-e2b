# N30 磁盘配额口径（存量 vs 峰值）收口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `diskMB` 一个**唯一口径**并在代码、文档、对外语义三处对齐：采用**存量（位置边界）**——"这棵沙箱树当前占的字节 + 每个目录自身的分配块"，删除即归还；**否决峰值口径**，并据此把"qcow2-over-NBD 镜像路线（L3）"从"待决策"改成"不做（带触发）"。

**Architecture:** 口径本身不用新机制：留存量的三层已在集群上实测（`open` 时按剩余额度发放上限、`unlink` 当场归还、per-exec `RLIMIT_FSIZE` 硬顶 + 建条目 `ENOSPC`）。本期要做的只是①把结论写死进四份文档、②把"超限**不冻结**、只拒写"从实现细节升格为**被单测钉住的对外契约**、③给发放/归还的账本加不变量用例、④修正两处已经过期的文档句（"k8s 上 `diskUsed` 恒为 0"）、⑤把条目（inode）维度**按已有旋钮**写进口径而**不新增机制**。

**Tech Stack:** Python（`control_plane/registry/manager.py` 的准入台账、`envd_service/runtime/dir_ledger.py` 的脏目录账本、`envd_service/priv_helpers.py::dir_size` 的整树测量）、XFS project quota（仅 compose/目标机）、k8s 形态的记账软闸门、pytest 单测 + 集群探针。

## Global Constraints

- 临时文件一律放本仓库 `tmp/`（AGENTS.md），不使用系统 `/tmp`、`$TMPDIR`。
- 测试断言必须精确匹配；禁用 `toContain` / `includes` / 部分匹配；禁止新增 skip 或用 ignore 掩盖失败。
- 改了部署清单先看差异：`DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl diff -f -`。
- 任何 kubectl 都必须显式带 `KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`（判据见 `docs/deploy-clusters.md` §2）。
- 本机测试命令是 `tmp/testenv/bin/python -m pytest`（`.venv` 缺 `fakeredis`）。
- **口径只有一句**：`diskMB` = 存量硬上限（当前占用 + 目录分配块），删除即时归还；任何文档、错误文案、指标说明都照抄这一句，不得出现"写多少算多少""删了不退"作为**选项**的表述。
- 不改功能换性能：本条**不引入**任何新的强制点、不做 qcow2/NBD/loop 镜像改造（L3 触发条件满足前不动）。
- `diskUsed` 的单位与口径与沙箱内 `du -sb --apparent-size` 必须能对齐（`tests/unit/test_dir_ledger.py` 已经是"随机突变序列 == 整树 walk"的钉子，不要绕过它）。

---

### Task 1: 把口径决定写进文档（零代码）

**Files:**
- Modify: `docs/sandbox-disk-quota.md:16-38`（§1.1 口径表）
- Modify: `docs/disk-quota-options.md:371-376`（§7 L3 段）、`:205`、`:349`（两处过期句）
- Modify: `docs/open-issues.md:27`（N30 行）
- Modify: `docs/task-backlog.md:129`（N30 行）

**Interfaces:**
- Consumes: 无
- Produces: 四份文档口径一致的结论段（可直接粘贴，见 Step 3）

- [ ] **Step 1: 先确认"现状与结论不符"的那两处（这是本任务要修的目标）**

  Run: `rg -n "仍然.*0|恒为 0" docs/disk-quota-options.md`

  Expected: 命中 `docs/disk-quota-options.md:205`（"k8s 上 `diskUsed` 恒为 0"）与 `:349`（"在 k8s 上**仍然是 0**"）。这两句已经过期——worker 的实测值现在**落在记录上**并对外暴露：

  Run: `rg -n "workspace_disk_used_bytes" control_plane/registry/manager.py | head`

  Expected: `manager.py:1746-1747`（每次心跳把实测字节写回记录）、`manager.py:245`/`264`（`sample_metric` 用它填 `diskUsed`）；对外出口是 `control_plane/api/sandboxes.py:2702`（`GET /sandboxes/{id}/metrics`）。

- [ ] **Step 2: 写出结论草稿（Step 3 就贴这段，一字不改地落到四处）**

  > **口径（2026-09-26 定，N30）：`diskMB` = 存量硬上限。** 它指的是"**这棵沙箱树当前占用的字节**（文件按 `st_size`、目录按分配块 `st_blocks×512`）**加上**镜像形态下不计入的公共 rootfs"，删除文件/目录即时归还；上限由三处一起保证：中介在 `open` 时按剩余额度发放单文件上限、`unlink`/删除当场归还、`O_CREAT`/`mkdir`/`symlink`/`link` 在超预算时返回 `ENOSPC`，另有 per-exec `RLIMIT_FSIZE` 作为内核级兜底。**明确否决"峰值口径"（写多少算多少、删了不退）**：它的唯一卖点是"内核级 ENOSPC、不经过中介"，而残留缺口只是"一条命令里的循环写者"（今天由上面那条内核兜底覆盖，集群实测超支 0–1 MiB）；代价却是不可逆的——本集群的 NFS **不支持打洞**（`fallocate -p` unsupported、`fstrim` 报 973 MiB 而 `du` 不降），镜像占用 = **高水位**，删文件不退还空间，于是"写满一次"会把沙箱**永久**降级成只读，唯一的恢复路径是销毁重建；此外还要付 privileged/`SYS_ADMIN` + `/dev/loop` 或 NBD + 宿主 `modprobe nbd` + 每节点守护进程 + "工作区不再是目录"（GC/快照/模板/迁移/CP 读树全部跟着改）。**因此 qcow2-over-NBD（L3）不作为计划项**：它只在"必须在写路径上做到字节级 ENOSPC、且愿意接受'删除不退'与工作区语义变更"时才回来。**触发条件**（满足任一即回到这条）：① 出现真实使用者要求写路径字节级 ENOSPC；② 存储换成支持打洞或支持目录配额的文件系统（那时先重新评估目录配额与条目配额，再谈镜像）。

- [ ] **Step 3: 写入四处**

  1. `docs/sandbox-disk-quota.md` §1.1 的对比表**加一行**：

     ```
     | 峰值口径（已否决，2026-09-26 N30） | 写多少算多少，删除不退 | 镜像/块设备路线的候选口径；本集群 NFS 不支持打洞 ⇒ 删除不退，写满即永久只读。结论与理由见 docs/disk-quota-options.md §7 |
     ```

  2. `docs/disk-quota-options.md` §7 的 `### L3` 段（`371-376`）末尾加一句"**不作为计划项**（N30 已定存量口径）"，并**改掉两处过期句**：`:205` 与 `:349` 都改成"该数已由 worker 的实测值落库（`record.workspace_disk_used_bytes`）并从 `GET /sandboxes/{id}/metrics` 的 `diskUsed` 暴露；口径 = 存量（含目录分配块）"。
  3. `docs/open-issues.md:27`（N30 行）：状态从 **待决策** 改成 **已定（存量口径）**，下一步写成"L3 不做（带触发）；口径落地记录见本计划"。
  4. `docs/task-backlog.md:129`（N30 行）：末尾附上 Step 2 那段结论的**第一句 + 触发条件**（不要把整段抄进去，backlog 是索引不是论证）。

- [ ] **Step 4: 校验口径在文档里只有一种说法**

  Run: `rg -n "峰值" docs/*.md`

  Expected: 只命中三处——`docs/sandbox-disk-quota.md` 新增的那一行、`docs/disk-quota-options.md` L3 的新增句、`docs/task-backlog.md` 的触发条件；**不再有**"待决策/待定/要不要"这类悬置表述。

- [ ] **Step 5: 提交**

  ```bash
  git add docs/sandbox-disk-quota.md docs/disk-quota-options.md docs/open-issues.md docs/task-backlog.md
  git commit -m "N30(1/6): 配额口径定为存量，L3 转为不做（带触发），并修掉两处过期的 diskUsed 描述"
  ```

---

### Task 2: 把"超限不冻结、只拒写"钉成对外契约

**Files:**
- Modify: `control_plane/registry/manager.py:1702-1765`（`enforce_disk_budget` 的 docstring 已写明语义，补一行指向契约用例）
- Modify: `control_plane/registry/manager.py:239-266`（`sample_metric` 的 `diskUsed`/`diskTotal` 注释写成口径句）
- Modify: `tests/unit/test_sandbox_disk_enforcement.py`
- Modify: `tests/contract/test_disk_budget_enforcement.py`

**Interfaces:**
- Consumes: Task 1 的口径
- Produces: 一条被单测钉住的不变量——**超预算的沙箱记录状态仍是 `running`**，且 `sample_metric()["diskUsed"]` 等于 worker 上报的实测字节

- [ ] **Step 1: 写会失败的测试**

  在 `tests/contract/test_disk_budget_enforcement.py` 新增：

  ```python
  def test_over_budget_records_the_measurement_and_keeps_the_sandbox_running(...):
      # 建一个 disk_size_mb=64 的记录，喂一份 128 MiB 的实测报告
      over = registry.enforce_disk_budget({record.sandbox_id: 128 * 1024 * 1024})
      assert [r.sandbox_id for r in over] == [record.sandbox_id]
      assert registry.get(record.sandbox_id).state == "running"       # 精确相等
      assert registry.get(record.sandbox_id).workspace_disk_used_bytes == 128 * 1024 * 1024
      assert registry.get(record.sandbox_id).sample_metric()["diskUsed"] == 128 * 1024 * 1024
      assert registry.get(record.sandbox_id).sample_metric()["diskTotal"] == 64 * 1024 * 1024

  def test_an_in_budget_report_is_recorded_without_being_reported_as_a_crossing(...):
      # 同一份报告在预算内时返回空列表，但数字照样落库（这是舰队账本）
      assert registry.enforce_disk_budget({record.sandbox_id: 1_000_000}) == []
      assert registry.get(record.sandbox_id).workspace_disk_used_bytes == 1_000_000
  ```

  再补一条**反向**钉子（防止有人后来"顺手"把冻结加回来）：

  ```python
  def test_enforce_disk_budget_does_not_pause_or_release_anything(...):
      before = registry.global_reserved()
      registry.enforce_disk_budget({record.sandbox_id: 999 * 1024 * 1024})
      assert registry.global_reserved() == before
      assert registry.get(record.sandbox_id).quota_released is False
  ```

- [ ] **Step 2: 跑它，确认现状与契约相符**

  Run: `tmp/testenv/bin/python -m pytest tests/contract/test_disk_budget_enforcement.py -q`

  Expected: PASS。**注意这里的预期是"通过"而不是"失败"**——`enforce_disk_budget` 的实现与 docstring（`manager.py:1713-1725`：*"Over budget is not a pause"*）今天已经一致；本步要确认的是"这条语义确实被实现"，把它从注释变成用例。如果没有通过，先查是不是把 `pause` 加回来了——那是本任务最该抓的回归。

- [ ] **Step 3: 把口径写进代码注释与对外文案**

  1. `manager.py:239-266`（`sample_metric`）：在 `"diskUsed"` 那一行上方加一行注释——"`diskUsed` = 该沙箱树当前占用的字节（含每个目录的分配块），删除即减少；`diskTotal` = 卖出的 `diskMB` 上限。口径见 `docs/sandbox-disk-quota.md` §1.1（N30）。"
  2. `manager.py:1702-1710`（`enforce_disk_budget` docstring）末尾加一行："口径与不变量由 `tests/contract/test_disk_budget_enforcement.py::test_over_budget_records_the_measurement_and_keeps_the_sandbox_running` 钉住。"
  3. 检查拒绝/超限的**用户可见文案**有没有"暂停"的说法：

     Run: `rg -n "disk|磁盘" control_plane/registry/manager.py | rg -n "pause|暂停"`

     Expected: 只命中解释"为什么不暂停"的注释；若命中任何"将被暂停"的对外字符串，改成"写入会被拒绝，直到回到预算内（删除即时释放）"。

- [ ] **Step 4: 跑测试，确认通过**

  Run: `tmp/testenv/bin/python -m pytest tests/contract/test_disk_budget_enforcement.py tests/unit/test_sandbox_disk_enforcement.py tests/unit/test_disk_overrun_visibility.py tests/unit/test_pause_quota.py -q`

  Expected: PASS（新增 3 条 + 既有点数不变）。

- [ ] **Step 5: 提交**

  ```bash
  git add control_plane/registry/manager.py tests/contract/test_disk_budget_enforcement.py tests/unit/test_sandbox_disk_enforcement.py
  git commit -m "N30(2/6): 把「超限只拒写、不冻结」钉成契约，并把 diskUsed 口径写进注释"
  ```

---

### Task 3: 预算发放 / 归还的账本不变量

**Files:**
- Modify: `tests/unit/test_pause_quota.py`
- Modify: `tests/unit/test_tenant_quota.py`
- Modify: `control_plane/registry/manager.py:665-712`（`_quota_allows_locked`）、`:773-800`（`global_reserved`）、`:825-877`（`_tenant_quota_allows_locked`）、`:878-923`（`release_quota`）、`:924-982`（`hold_quota`）

**Interfaces:**
- Consumes: Task 2 的契约
- Produces: 三条不变量用例——(i) `global_reserved()["disk"] == Σ(活记录 disk_size_mb)`；(ii) `pause`/`delete`/TTL 过期 三条路径各自**只**归还一次（`release_quota` 幂等）；(iii) 归还后同一份预算能被下一次建箱拿走

- [ ] **Step 1: 写会失败的测试**

  ```python
  def test_disk_reservation_is_exactly_the_sum_of_live_records(registry):
      a = make_record(registry, disk_size_mb=64)
      b = make_record(registry, disk_size_mb=128)
      assert registry.global_reserved()["disk"] == 192
      registry.release_quota(b)
      assert registry.global_reserved()["disk"] == 64

  def test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once(registry):
      r = make_record(registry, disk_size_mb=64)
      assert registry.release_quota(r) is True      # pause / delete / expiry 走同一个函数
      assert registry.release_quota(r) is False     # 第二次是 no-op
      assert registry.global_reserved()["disk"] == 0
      assert registry.hold_quota(r) is True         # resume 拿得回来
      assert registry.global_reserved()["disk"] == 64
  ```

  第三条（这条最容易漏，写死）：**超预算不归还**——

  ```python
  def test_an_over_budget_sandbox_keeps_its_reservation(registry):
      r = make_record(registry, disk_size_mb=64)
      registry.enforce_disk_budget({r.sandbox_id: 128 * 1024 * 1024})
      assert registry.global_reserved()["disk"] == 64
  ```

- [ ] **Step 2: 跑它，确认失败或暴露真缺口**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pause_quota.py tests/unit/test_tenant_quota.py -q`

  Expected: 若 `global_reserved()["disk"]` 的口径是"预留"而记录里已被 `release`，第一条会 FAIL；否则 PASS。**两种结果都要如实记录**：FAIL 说明存在"记录活着但预留已归还"的真实不一致（那时先修实现，不修测试）；PASS 说明不变量成立，把这三条作为回归钉子留下。

- [ ] **Step 3: 最小实现（只在 Step 2 暴露缺口时需要）**

  缺口形状只有两种，修法都最小：

  1. **归还漏了 disk 维**：`release_quota`（`:878-923`）的 `dims = self._global_dims(record)` 必须包含 `"disk": record.disk_size_mb`（与 `_quota_denied_message` 的维度名一致，`manager.py:657-664` 那张映射表就是唯一真相）；`_quota_store is None` 的分支同步减 `_reserved_disk`。
  2. **双份归还**：任何新路径都必须过 `release_quota` 的 `record.quota_released` 幂等闸门，**不要**在别处直接改 `_reserved_disk`。

- [ ] **Step 4: 跑测试，确认通过**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pause_quota.py tests/unit/test_tenant_quota.py tests/unit/test_ttl.py tests/unit/test_redis_multireplica.py -q`

  Expected: PASS（`test_redis_multireplica.py` 一起跑是因为台账有两个后端：进程内与共享存储，两条路径的归还语义必须一致）。

- [ ] **Step 5: 提交**

  ```bash
  git add tests/unit/test_pause_quota.py tests/unit/test_tenant_quota.py control_plane/registry/manager.py
  git commit -m "N30(3/6): 磁盘预留在账本上加不变量（Σ 活记录、只归还一次、超预算不归还）"
  ```

---

### Task 4: 逐字节契约 + 条目（inode）维度的口径落定

**Files:**
- Modify: `tests/unit/test_dir_ledger.py`
- Modify: `tests/unit/test_priv_helpers.py`
- Modify: `docs/sandbox-disk-quota.md`（§1.1 表格下方补"第二维：条目数"一段）
- Modify: `deploy/k8s/worker.yaml:485`（`E2B_DISK_MAX_ENTRIES` 的注释指向口径；**值不改**）

**Interfaces:**
- Consumes: `envd_service/runtime/dir_ledger.SubtreeScan.files_by_dir`（`dir_ledger.py:61-81`）、`envd_service/priv_helpers.py::dir_size`（`:787-830`）、`envd_service/runtime/brief_stat.entry_size`
- Produces: (i) 一条"账本与 `os.stat` 在同一文件上返回同一个 `st_size`"的钉子；(ii) 口径文档里明确"条目维度的机制已经存在（`E2B_DISK_MAX_ENTRIES`），本期**不新增**第二种旋钮"

- [ ] **Step 1: 写会失败的测试**

  ```python
  def test_the_ledger_and_os_stat_agree_on_the_same_file(tmp_path):
      p = tmp_path / "a.bin"
      p.write_bytes(b"x" * 12345)
      scan = dir_ledger.scan_subtree(str(tmp_path))
      assert scan.bytes_by_dir[str(tmp_path)] == os.stat(p).st_size

  def test_directory_cost_matches_the_allocated_blocks_not_st_size(tmp_path):
      from envd_service.runtime.brief_stat import directory_cost
      assert directory_cost(str(tmp_path)) == os.stat(tmp_path).st_blocks * 512
  ```

  第三条（把"条目维度已存在"钉在代码上，而不是只写在文档里）：

  ```python
  def test_entry_counts_are_already_part_of_the_scan():
      scan = dir_ledger.scan_subtree(str(tmp_path))
      assert scan.file_count == sum(scan.files_by_dir.values())
  ```

- [ ] **Step 2: 跑它，确认失败**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_dir_ledger.py -q -k "agree_on_the_same_file or allocated_blocks or entry_counts"`

  Expected: 至少 `test_the_ledger_and_os_stat_agree_on_the_same_file` FAIL（`bytes_by_dir` 这个键名/语义要靠读 `dir_ledger.py:61-81` 订正成真实字段名——**这一条就是"先确认现状"**）。字段名以代码为准，不要为了让测试好看而改实现。

- [ ] **Step 3: 最小实现**

  1. 按 `dir_ledger.SubtreeScan`（`dir_ledger.py:61-81`）的真实字段把测试改到位（`files_by_dir` 与它派生的 `file_count`）。
  2. `docs/sandbox-disk-quota.md` §1.1 表格下方补一段：

     > **第二维：条目数（inode）。** 字节账本看不见"空文件"——2000 个空文件在字节口径下增量是 0（N31 实测），而风险是真金白银的 inode/元数据耗尽与整树 walk 成本。机制**已经存在**：`dir_ledger` 边算字节边数名字，worker 每轮下发上限，中介在计数到顶时对 `O_CREAT`/`mkdir`/`symlink`/`link` 返回 `ENOSPC`；旋钮是 `E2B_DISK_MAX_ENTRIES`（代码默认 `0` = 关；k8s 取 `500000` 作失控兜底，见 `deploy/k8s/worker.yaml`）。**本期不新增第二种旋钮**，只把它写进口径：`diskMB` 管字节，条目上限管数量，两者都按"删除即归还"。
  3. `deploy/k8s/worker.yaml:485` 的注释末尾加一句"（口径见 `docs/sandbox-disk-quota.md` §1.1 的'第二维'；N30 定不新增旋钮）"。

- [ ] **Step 4: 跑测试，确认通过**

  Run: `tmp/testenv/bin/python -m pytest tests/unit/test_dir_ledger.py tests/unit/test_priv_helpers.py tests/unit/test_platform_disk.py -q`

  Expected: PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add tests/unit/test_dir_ledger.py tests/unit/test_priv_helpers.py docs/sandbox-disk-quota.md deploy/k8s/worker.yaml
  git commit -m "N30(4/6): 账本逐字节契约与条目维度口径（复用已有 E2B_DISK_MAX_ENTRIES）"
  ```

---

### Task 5: quota-agent / EDQUOT 路径的口径对齐（compose/XFS 线）

**Files:**
- Modify: `tests/contract/test_xfs_project_quota.py`
- Modify: `tests/unit/test_quota_agent_server.py`
- Modify: `docs/production-deployment-requirements.md:368-396`（§2.4.4，k8s 不上 agent 的口径）

**Interfaces:**
- Consumes: `envd_service/xfs_quota.py:539`（`limit -p bhard=<disk_mb>M`）、`deploy/quota_agent/app.py:128`（`disk_mb=body.limit_mb`）
- Produces: 一条契约用例明确"服务器端 EDQUOT 在 NFS 客户端表现为 **ENOSPC**"，并在文档里写清"k8s 形态没有 agent ⇒ 口径仍成立，只是强制点在别处"

- [ ] **Step 1: 写会失败的测试**

  ```python
  # tests/contract/test_xfs_project_quota.py
  def test_the_quota_row_is_a_position_boundary_not_a_write_budget(fake_xfs_quota):
      # 建箱 -> bhard == disk_mb（存量上限）；删除 -> bsoft/bhard 归零（归还）
      envd_service.xfs_quota.create_project(..., disk_mb=64)
      assert fake_xfs_quota.last_limit == "limit -p bhard=64M 1004"
      envd_service.xfs_quota.release_project(...)
      assert fake_xfs_quota.last_limit == "limit -p bsoft=0 bhard=0 1004"
  ```

  ```python
  # tests/contract/test_xfs_project_quota.py（NFS 客户端的答案）
  def test_the_client_sees_enospc_not_edquot(...):
      # 服务器端超限返回 EDQUOT，NFS 客户端表现为 ENOSPC(errno 28)
      with pytest.raises(OSError) as exc:
          write_past_the_limit(...)
      assert exc.value.errno == 28
  ```

  第二条的既有依据在 `docs/production-deployment-requirements.md` §5.2 第 2 条（实测结论），测试要按**现有夹具**的形状落地；若本机没有能复现 NFS 的夹具，就把它落成 `tests/unit/test_quota_agent_server.py` 里的**文案/映射**断言（agent 把 errno 原样透传、不把它翻译成 EDQUOT）——**不要**新建一个永远 skip 的用例。

- [ ] **Step 2: 跑它，确认失败或确认现状**

  Run: `tmp/testenv/bin/python -m pytest tests/contract/test_xfs_project_quota.py tests/unit/test_quota_agent_server.py -q`

  Expected: 新增的两条里至少一条 FAIL（字段名/夹具差异），把真实形状读出来（`xfs_quota.py:539`、`:1002` 就是 bhard 与清零两句）。

- [ ] **Step 3: 最小实现**

  1. 按真实实现订正测试（`bhard=<disk_mb>M`；清零是 `limit -p bsoft=0 bhard=0`，见 `xfs_quota.py:946`/`:1002`）。
  2. `docs/production-deployment-requirements.md` §2.4.4 末尾补一句："N30 的口径（存量、删除即归还）在**没有** agent 的 k8s 形态同样成立——强制点从中介的活账本 + per-exec `RLIMIT_FSIZE` 提供；agent 路径只是把同一口径交给内核（`bhard`），两者的**语义必须一致**（`envd_service/xfs_quota.py` 与 `deploy/quota_agent/app.py` 各只有一处 `disk_mb` 入参）。"
  3. 不要为此新增任何部署清单。

- [ ] **Step 4: 跑测试，确认通过**

  Run: `tmp/testenv/bin/python -m pytest tests/contract/test_xfs_project_quota.py tests/unit/test_quota_agent_server.py tests/unit/test_quota_maintenance.py -q`

  Expected: PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add tests/contract/test_xfs_project_quota.py tests/unit/test_quota_agent_server.py docs/production-deployment-requirements.md
  git commit -m "N30(5/6): quota-agent 路径与存量口径对齐，明确 EDQUOT→ENOSPC 的客户端形状"
  ```

---

### Task 6: 对外 `diskMB` 语义的集群现场验收

**Files:**
- Create: `tmp/k0s/probe_disk_metric_agreement.py`
- Modify: `docs/k8s-deployment.md:2003-2033`（§22.5.12，把"超支可见"那半从"仍未决"改成实测结论）

**Interfaces:**
- Consumes: Task 1–5 的落地版本
- Produces: 一份"沙箱内自量 == 平台上报"的逐字节证据 + "写满后删掉即可继续写"的行为证据

- [ ] **Step 1: 写探针，并先在当前版本上跑一次拿到对照**

  形状（照 `tmp/k0s/probe_dir_ledger.py` 那套"沙箱内自己量 == 平台上报"的既有套路）：

  ```
  1) 建 diskMB=64 的沙箱
  2) 沙箱内 df/du -sb --apparent-size /home/user 与 /workspace 各取一次
  3) dd if=/dev/zero of=big bs=1M count=128 -> 必须失败（EFBIG 或 ENOSPC），记 errno
  4) 删掉 big -> 立刻再写 8 MiB -> 必须成功
  5) GET /sandboxes/{id}/metrics 的 diskUsed 与第 2 步的 du 数字逐字节相等
  ```

  Run: `E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000 tmp/testenv/bin/python tmp/k0s/probe_disk_metric_agreement.py`

  Expected（对照组）：第 3 步失败、第 4 步成功、第 5 步两边数字相等（这条今天应当已经成立——`docs/k8s-deployment.md` §22.5.9/§22.5.10 与 §22.5.12 记的实测就是它）。若不成立，先把差异记下来：那是**口径没对齐**的真缺口，比文档措辞重要。

- [ ] **Step 2: 把结论写进 §22.5.12**

  `docs/k8s-deployment.md:2003-2033` 里"超支可见（`diskUsed` 恒为 0）"的那一段改成：口径 = 存量（含目录分配块）、数据来源 = worker 心跳实测 + `record.workspace_disk_used_bytes`、出口 = `GET /sandboxes/{id}/metrics`；并给出本次探针的日期与数字。

- [ ] **Step 3: 三份文档一致性复查**

  Run: `rg -n "diskUsed" docs/*.md`

  Expected: 没有任何一处仍写"恒为 0 / 仍然是 0 / 还没接到 API"。

- [ ] **Step 4: 提交**

  ```bash
  git add docs/k8s-deployment.md
  git commit -m "N30(6/6): 集群实测 diskUsed 与沙箱内自量逐字节相等"
  ```

---

## 验收判据（怎么算这条收口了）

1. **口径唯一**：`rg -n "峰值" docs/*.md` 只剩结论行（新增的口径说明 + 触发条件），没有"待决策"。
2. **对外契约被钉住**：`enforce_disk_budget` 超限后记录仍是 `running`、`quota_released` 仍为 `False`、实测数字照样落库——三条断言在同一次会话里全绿（Task 2 的 3 条）。
3. **账本不变量**：`global_reserved()["disk"] == Σ(活记录 disk_size_mb)`；pause/delete/expiry 三条路径各只归还一次。
4. **数字能对账**：集群上 `GET /sandboxes/{id}/metrics` 的 `diskUsed` 与沙箱内 `du -sb --apparent-size` 逐字节相等；`dd` 超限失败、删除后立刻可写。
5. **L3 有明确去向**：文档与 backlog 都写成"不做（带触发）"，触发条件是**可判定的两件事**（真实使用者要求字节级 ENOSPC；换到支持打洞/目录配额的存储）。
6. 两处过期句（`docs/disk-quota-options.md:205`、`:349`）已被改正。

## 需人拍板 / 外部前提

1. **确认存量口径成立、并接受 L3 不做**（本计划的全部推理都挂在这一条上；若改判峰值口径，Task 1 与 Task 5 的文本要重写，且要另开一份 qcow2/NBD 的实施计划）。
2. **条目（inode）维度是否升格为对外配额项**：机制已在（`E2B_DISK_MAX_ENTRIES`），本期只写口径、不新增旋钮；若要变成"每沙箱可配置的条目上限"，那是一条独立的 API 变更。
3. **NAS 规格确认（通用型 vs 极速型）**：只影响"将来是否重新评估目录配额"（`docs/disk-quota-options.md` §3 的 500 目录/GiB、整数、RAM 凭据），与本次口径决定无关。
