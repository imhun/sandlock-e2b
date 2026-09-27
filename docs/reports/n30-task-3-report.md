# N30 Task 3 报告：预算发放 / 归还的账本不变量

**Status**：完成。**Step 2 的结果是 PASS** —— 三条不变量在今天的实现上已经成立，
`control_plane/registry/manager.py` **一字未改**（`git diff --quiet control_plane/registry/manager.py`
通过，提交里也没有它）。用例作为回归钉子落地。

**Commit**：`43f9085` N30(3/6): 磁盘预留在账本上加不变量（Σ 活记录、只归还一次、超预算不归还）

**测试**：

- 简报 Step 4 的lane：`tmp/testenv/bin/python -m pytest tests/unit/test_pause_quota.py
  tests/unit/test_tenant_quota.py tests/unit/test_ttl.py tests/unit/test_redis_multireplica.py -q`
  → **59 passed**（改前同一 lane 是 46 passed，+13 条新用例）。
- 契约文件（Task 2 的钉子，未触碰）：`tests/contract/test_disk_budget_enforcement.py` → **10 passed**。
- 整个单元车道：`tests/unit -q` → **14 failed / 1329 passed / 11 skipped**。14 条失败全在
  **我没碰过的文件**：`test_priv_helpers.py` ×11、`test_real_root_gate.py` ×1、
  `test_xfs_quotactl_backend.py` ×2 —— 即 AGENTS 里说的那批已知 Linux-only 红（数量与基线一致）。

---

## 1. 用例清单：哪条断言证伪什么

### (i) `global_reserved()["disk"] == Σ(活记录 disk_size_mb)`

| 用例 | 位置 | 证伪的回归 |
|---|---|---|
| `test_disk_reservation_is_exactly_the_sum_of_live_records`（**照抄简报**） | `test_pause_quota.py` | 发放（hold/`_reserve`）或归还（`release_quota`）任一侧漏了 disk 维 |
| `test_the_tenant_disk_row_is_the_sum_of_its_live_records` | `test_tenant_quota.py` | 租户那一份账本（`_tenant_dims`）与全局脱钩 |

### (ii) `pause` / `delete` / TTL 过期三条路径各自只归还一次

| 用例 | 位置 | 证伪的回归 |
|---|---|---|
| `test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once`（**照抄简报**） | `test_pause_quota.py` | `release_quota` 的幂等闸门被拿掉（第二次返回 `True` 并再减一次） |
| `test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op` | 同上 | **真实路径** pause → delete 的二次归还；带第二个记录当"量具"，让 `max(0, …)` 的夹逼掩盖不住 |
| `test_delete_releases_the_disk_row_once` | 同上 | delete 之后再对同一个 record 对象调 `release_quota` 必须 `is False` |
| `test_ttl_expiry_releases_the_disk_row_once` | 同上 | `remove_expired` → `_release` 的归还；且不得碰到别人的行 |
| `test_shared_store_pause_then_delete_returns_the_disk_row_once` | 同上 | 共享存储后端上"闸门读的是 store 里那个 flag"（这里没有 clamp，二次归还会把行减成负数） |
| `test_pause_and_the_delete_after_it_return_the_tenant_disk_row_once` / `test_ttl_expiry_returns_the_tenant_disk_row_once` / `test_the_tenant_disk_row_returns_to_zero_in_the_shared_store` | `test_tenant_quota.py` | 租户账本的同一件事（内存 + 共享存储两个后端） |

### (iii) 归还后同一份预算能被下一次建箱拿走（且超预算不归还）

| 用例 | 位置 | 证伪的回归 |
|---|---|---|
| `test_an_over_budget_sandbox_keeps_its_reservation`（**照抄简报**） | `test_pause_quota.py` | 把"超限即冻结"那套语义又搬回来（跨越时顺手归还预留） |
| `test_a_released_disk_budget_is_bookable_by_the_next_create` | 同上 | 归还后预算拿不回来；**并且先证明这份预算是真的卡住过**（有界池下第二次建箱必须被以 disk 理由拒掉，`str(exc.value) == workspace_disk_refusal(64, 64)` 逐字匹配） |
| `test_shared_store_disk_row_follows_the_live_records_and_returns_once` | 同上 | 共享存储后端的 Σ 恒等式；pause/resume/delete 每一步都比一次 `账本 == Σ(活记录)` |

合计新增 13 条（`test_pause_quota.py` 9 条、`test_tenant_quota.py` 4 条）。简报里给出的三段代码逐字照抄
（只把 `registry` / `make_record` 改成 fixture 参数）。

---

## 2. 变异取证（6 次，原始输出）

每次都是：打补丁 → 跑 → 留原文 → 还原（`git diff --quiet control_plane/registry/manager.py` 复核通过）。
原始输出留在 `tmp/n30-task-3/mutation-*.txt`，下面逐字贴出。

### 变异 A：内存分支的归还漏掉 disk 维（简报 Step 3 缺口形状 1）

```diff
-                self._reserved_disk = max(
-                    0, self._reserved_disk - record.disk_size_mb
-                )
+                self._reserved_disk = max(0, self._reserved_disk)  # MUTATION A
```

`tmp/testenv/bin/python -m pytest tests/unit/test_pause_quota.py tests/unit/test_tenant_quota.py -q --tb=short`

```
............FF.FFFF.............                                         [100%]
=================================== FAILURES ===================================
___________ test_disk_reservation_is_exactly_the_sum_of_live_records ___________
tests/unit/test_pause_quota.py:256: in test_disk_reservation_is_exactly_the_sum_of_live_records
    assert registry.global_reserved()["disk"] == 64
E   assert 192 == 64
___ test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once ____
tests/unit/test_pause_quota.py:265: in test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once
    assert registry.global_reserved()["disk"] == 0
E   assert 64 == 0
___ test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op ___
tests/unit/test_pause_quota.py:289: in test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op
    assert registry.global_reserved()["disk"] == 128
E   assert 192 == 128
____________________ test_delete_releases_the_disk_row_once ____________________
tests/unit/test_pause_quota.py:300: in test_delete_releases_the_disk_row_once
    assert registry.global_reserved()["disk"] == 0
E   assert 64 == 0
__________________ test_ttl_expiry_releases_the_disk_row_once __________________
tests/unit/test_pause_quota.py:314: in test_ttl_expiry_releases_the_disk_row_once
    assert registry.global_reserved()["disk"] == 128
E   assert 192 == 128
__________ test_a_released_disk_budget_is_bookable_by_the_next_create __________
tests/unit/test_pause_quota.py:337: in test_a_released_disk_budget_is_bookable_by_the_next_create
    assert registry.global_reserved()["disk"] == 0
E   assert 64 == 0
=========================== short test summary info ============================
FAILED tests/unit/test_pause_quota.py::test_disk_reservation_is_exactly_the_sum_of_live_records
FAILED tests/unit/test_pause_quota.py::test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once
FAILED tests/unit/test_pause_quota.py::test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op
FAILED tests/unit/test_pause_quota.py::test_delete_releases_the_disk_row_once
FAILED tests/unit/test_pause_quota.py::test_ttl_expiry_releases_the_disk_row_once
FAILED tests/unit/test_pause_quota.py::test_a_released_disk_budget_is_bookable_by_the_next_create
6 failed, 26 passed in 0.74s
```

（租户那 4 条此时全绿 —— 租户行是另一段代码，见变异 A2。）

### 变异 A2：内存分支的**租户**归还漏掉 disk 维

```diff
                         for dim, value in self._tenant_dims(record).items():
+                            if dim == "disk":  # MUTATION A2
+                                continue
                             used[dim] = max(0, used[dim] - value)
```

```
............................FFF.                                         [100%]
=================================== FAILURES ===================================
___________ test_the_tenant_disk_row_is_the_sum_of_its_live_records ____________
tests/unit/test_tenant_quota.py:184: in test_the_tenant_disk_row_is_the_sum_of_its_live_records
    assert registry._tenant_reserved["t1"]["disk"] == 192
E   assert 2240 == 192
______ test_pause_and_the_delete_after_it_return_the_tenant_disk_row_once ______
tests/unit/test_tenant_quota.py:198: in test_pause_and_the_delete_after_it_return_the_tenant_disk_row_once
    assert registry._tenant_reserved["t1"]["disk"] == 192
E   assert 2240 == 192
_______________ test_ttl_expiry_returns_the_tenant_disk_row_once _______________
tests/unit/test_tenant_quota.py:219: in test_ttl_expiry_returns_the_tenant_disk_row_once
    assert registry._tenant_reserved["t1"]["disk"] == 0
E   assert 1088 == 0
=========================== short test summary info ============================
FAILED tests/unit/test_tenant_quota.py::test_the_tenant_disk_row_is_the_sum_of_its_live_records
FAILED tests/unit/test_tenant_quota.py::test_pause_and_the_delete_after_it_return_the_tenant_disk_row_once
FAILED tests/unit/test_tenant_quota.py::test_ttl_expiry_returns_the_tenant_disk_row_once
3 failed, 29 passed in 0.23s
```

### 变异 B：共享存储分支的归还漏掉 disk 维（全局 + 租户两条 release）

```diff
         if self._quota_store is not None:
-            self._quota_store.release("global", dims)
+            # MUTATION B: the shared-store release forgets the disk dim.
+            self._quota_store.release(
+                "global", {k: v for k, v in dims.items() if k != "disk"}
+            )
             tenant_limits = self._tenant_limits(record.tenant_id, is_admin=False)
             if tenant_limits is not None:
                 self._quota_store.release(
-                    f"tenant:{record.tenant_id}", self._tenant_dims(record)
+                    f"tenant:{record.tenant_id}",
+                    {k: v for k, v in self._tenant_dims(record).items() if k != "disk"},
                 )
```

```
...................F...........F                                         [100%]
=================================== FAILURES ===================================
_____ test_shared_store_disk_row_follows_the_live_records_and_returns_once _____
tests/unit/test_pause_quota.py:368: in test_shared_store_disk_row_follows_the_live_records_and_returns_once
    assert registry.global_reserved()["disk"] == 64
E   assert 192 == 64
_________ test_the_tenant_disk_row_returns_to_zero_in_the_shared_store _________
tests/unit/test_tenant_quota.py:229: in test_the_tenant_disk_row_returns_to_zero_in_the_shared_store
    assert registry._quota_store.get("tenant:t1")["disk"] == 64
E   assert 1088 == 64
=========================== short test summary info ============================
FAILED tests/unit/test_pause_quota.py::test_shared_store_disk_row_follows_the_live_records_and_returns_once
FAILED tests/unit/test_tenant_quota.py::test_the_tenant_disk_row_returns_to_zero_in_the_shared_store
2 failed, 30 passed in 0.23s
```

### 变异 C：拿掉 `release_quota` 的幂等闸门（"归还两次"）

```diff
-        if record.quota_released:
+        if False:  # MUTATION C: the idempotency gate is gone
             return False
```

```
...F.........F.FFF.F..........FF.                                        [100%]
=================================== FAILURES ===================================
_____________ test_delete_of_paused_sandbox_does_not_release_twice _____________
tests/unit/test_pause_quota.py:120: in test_delete_of_paused_sandbox_does_not_release_twice
    assert _pool(registry) == 512  # only sbx_b still holds reservation
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   assert 0 == 512
E    +  where 0 = _pool(<control_plane.registry.manager.SandboxRegistry object at 0x1084f2ea0>)
___ test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once ____
tests/unit/test_pause_quota.py:264: in test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once
    assert registry.release_quota(r) is False  # the second one is a no-op
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   AssertionError: assert True is False
E    +  where True = release_quota(SandboxRecord(template_id='base', sandbox_id='sbx_60aed3ff5a9bb82c', client_id='cli_011dc71fb362', tenant_id=None, env..., priority=5, quota_released=True, orphaned_at=None, workspace_disk_used_bytes=None, pause_reason=None, paused_at=None))
E    +    where release_quota = <control_plane.registry.manager.SandboxRegistry object at 0x108626470>.release_quota
___ test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op ___
tests/unit/test_pause_quota.py:292: in test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op
    assert registry.global_reserved()["disk"] == 128
E   assert 64 == 128
____________________ test_delete_releases_the_disk_row_once ____________________
tests/unit/test_pause_quota.py:303: in test_delete_releases_the_disk_row_once
    assert registry.release_quota(record) is False
E   AssertionError: assert True is False
E    +  where True = release_quota(SandboxRecord(template_id='base', sandbox_id='sbx_c205852de2a931c5', client_id='cli_1ad3289a8747', tenant_id=None, env..., priority=5, quota_released=True, orphaned_at=None, workspace_disk_used_bytes=None, pause_reason=None, paused_at=None))
E    +    where release_quota = <control_plane.registry.manager.SandboxRegistry object at 0x108626e00>.release_quota
__________________ test_ttl_expiry_releases_the_disk_row_once __________________
tests/unit/test_pause_quota.py:315: in test_ttl_expiry_releases_the_disk_row_once
    assert registry.release_quota(record) is False
E   AssertionError: assert True is False
E    +  where True = release_quota(SandboxRecord(template_id='base', sandbox_id='sbx_b16ceabfb6ced9ea', client_id='cli_502f48b840fb', tenant_id=None, env..., priority=5, quota_released=True, orphaned_at=None, workspace_disk_used_bytes=None, pause_reason=None, paused_at=None))
E    +    where release_quota = <control_plane.registry.manager.SandboxRegistry object at 0x1086267a0>.release_quota
________ test_shared_store_pause_then_delete_returns_the_disk_row_once _________
tests/unit/test_pause_quota.py:359: in test_shared_store_pause_then_delete_returns_the_disk_row_once
    assert registry.global_reserved()["disk"] == 128
E   assert 64 == 128
______ test_pause_and_the_delete_after_it_return_the_tenant_disk_row_once ______
tests/unit/test_tenant_quota.py:204: in test_pause_and_the_delete_after_it_return_the_tenant_disk_row_once
    assert registry._tenant_reserved["t1"]["disk"] == 128
E   assert 64 == 128
_______________ test_ttl_expiry_returns_the_tenant_disk_row_once _______________
tests/unit/test_tenant_quota.py:220: in test_ttl_expiry_returns_the_tenant_disk_row_once
    assert registry.release_quota(record) is False
E   AssertionError: assert True is False
E    +  where True = release_quota(SandboxRecord(template_id='base', sandbox_id='sbx_d355fdcf732d09c3', client_id='cli_37bbc68735ef', tenant_id='t1', env..., priority=5, quota_released=True, orphaned_at=None, workspace_disk_used_bytes=None, pause_reason=None, paused_at=None))
E    +    where release_quota = <control_plane.registry.manager.SandboxRegistry object at 0x108627460>.release_quota
=========================== short test summary info ============================
FAILED tests/unit/test_pause_quota.py::test_delete_of_paused_sandbox_does_not_release_twice
FAILED tests/unit/test_pause_quota.py::test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once
FAILED tests/unit/test_pause_quota.py::test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op
FAILED tests/unit/test_pause_quota.py::test_delete_releases_the_disk_row_once
FAILED tests/unit/test_pause_quota.py::test_ttl_expiry_releases_the_disk_row_once
FAILED tests/unit/test_pause_quota.py::test_shared_store_pause_then_delete_returns_the_disk_row_once
FAILED tests/unit/test_tenant_quota.py::test_pause_and_the_delete_after_it_return_the_tenant_disk_row_once
FAILED tests/unit/test_tenant_quota.py::test_ttl_expiry_returns_the_tenant_disk_row_once
8 failed, 25 passed in 0.32s
```

（注意：`test_shared_store_disk_row_follows_the_live_records_and_returns_once` 在 C 下**仍然是绿的**。

> **这句解释已在 2026-09-26 更正（评审收口，见 §9.3）**：原文写的是"store 里 resume 没有把 flag 写回
> False，delete 多减的 128 正好抵消 resume 加回的 128"——**这是错的**。`resume` → `hold_quota` 会置
> `record.quota_released = False`，`resume` 末尾的 `save` 把它写进 store，探针可见 resume 之后 store 里
> 就是 `quota_released=False`。它在 C 下仍绿的真实理由是：pause 还的是第一份持有、resume 又买回一份
> **新的**持有、delete 还的是那一份 —— 这条序列里根本不存在"同一份持有被还两次"。
> `86d4e92` 之后还有第二条：C 下 5 条 `shared_store` 用例全绿，因为 N41 的归还认领在 store 侧先回答了
> "这份预留已经在被归还"。所以"共享存储的幂等性由 `test_shared_store_pause_then_delete_returns_the_disk_row_once`
> 负责证伪"只在 `86d4e92` 之前成立；今天由内存侧那三条 + N41 的认领用例共同负责。）

### 变异 D：跨越超限时"顺手归还"预留（把 E9.2 的冻结语义搬回来）

```diff
             logger.warning(
                 "sandbox %s over its workspace budget (%d MiB used of %d MiB): "
                 "writes are blocked until it is back inside",
                 ...
             )
+            self.release_quota(record)  # MUTATION D
             overruns[record.sandbox_id] = (used_bytes, budget_bytes)
```

`tmp/testenv/bin/python -m pytest tests/unit/test_pause_quota.py tests/unit/test_tenant_quota.py tests/contract/test_disk_budget_enforcement.py -q --tb=short`

```
..............F..................F........F                              [100%]
=================================== FAILURES ===================================
______________ test_an_over_budget_sandbox_keeps_its_reservation _______________
tests/unit/test_pause_quota.py:273: in test_an_over_budget_sandbox_keeps_its_reservation
    assert registry.global_reserved()["disk"] == 64
E   assert 0 == 64
------------------------------ Captured log call -------------------------------
WARNING  control_plane.registry.manager:manager.py:1775 sandbox sbx_273ad2bbe5d0ae25 over its workspace budget (128 MiB used of 64 MiB): writes are blocked until it is back inside
____________ test_over_budget_report_blocks_writes_and_never_pauses ____________
tests/contract/test_disk_budget_enforcement.py:94: in test_over_budget_report_blocks_writes_and_never_pauses
    assert registry.global_reserved()["disk"] == record.disk_size_mb
E   AssertionError: assert 0 == 1024
E    +  where 1024 = SandboxRecord(template_id='base', sandbox_id='sbx_c378ddd269cf0cdf', ..., quota_released=True, ..., workspace_disk_used_bytes=1073741825).disk_size_mb
------------------------------ Captured log setup ------------------------------
WARNING  control_plane.registry.secrets:secrets.py:183 E2B_SECRET_MASTER_KEY is not configured: secrets are stored without at-rest encryption and are not persisted to Redis (degraded mode; configure the key in production)
WARNING  envd_service.app:app.py:266 E2B_PRIV_HELPERS=auto on a non-root worker, but /var/lib/e2b-priv/e2b-slot-spawn is missing: this worker keeps the in-process (E5.1) shape; ship the file-capability brokers to get per-sandbox host uids and route-B slots
WARNING  envd_service.app:app.py:303 E2B_PER_SANDBOX_UID is enabled but the worker is not running as root; per-sandbox host uids are disabled (non-root workers use the fixed identity + Landlock model, E5.1)
------------------------------ Captured log call -------------------------------
WARNING  control_plane.registry.manager:manager.py:1775 sandbox sbx_c378ddd269cf0cdf over its workspace budget (1024 MiB used of 1024 MiB): writes are blocked until it is back inside
WARNING  control_plane.api.internal:internal.py:143 sandbox sbx_c378ddd269cf0cdf on node node_disk is over its workspace budget (1024 MiB used of 1024 MiB): writes are blocked until it is back inside
_________ test_enforce_disk_budget_does_not_pause_or_release_anything __________
tests/contract/test_disk_budget_enforcement.py:317: in test_enforce_disk_budget_does_not_pause_or_release_anything
    assert registry.global_reserved() == before
E   AssertionError: assert {'memory': 0,...processes': 0} == {'memory': 10...ocesses': 256}
E     
E     Differing items:
E     {'cpu': 0} != {'cpu': 100}
E     {'memory': 0} != {'memory': 1024}
E     {'disk': 0} != {'disk': 64}
E     {'processes': 0} != {'processes': 256}
E     Use -v to get more diff
------------------------------ Captured log setup ------------------------------
WARNING  control_plane.registry.secrets:secrets.py:183 E2B_SECRET_MASTER_KEY is not configured: secrets are stored without at-rest encryption and are not persisted to Redis (degraded mode; configure the key in production)
WARNING  envd_service.app:app.py:266 E2B_PRIV_HELPERS=auto on a non-root worker, but /var/lib/e2b-priv/e2b-slot-spawn is missing: this worker keeps the in-process (E5.1) shape; ship the file-capability brokers to get per-sandbox host uids and route-B slots
WARNING  envd_service.app:app.py:303 E2B_PER_SANDBOX_UID is enabled but the worker is not running as root; per-sandbox host uids are disabled (non-root workers use the fixed identity + Landlock model, E5.1)
------------------------------ Captured log call -------------------------------
WARNING  control_plane.registry.manager:manager.py:1775 sandbox sbx_budget_contract over its workspace budget (999 MiB used of 64 MiB): writes are blocked until it is back inside
=========================== short test summary info ============================
FAILED tests/unit/test_pause_quota.py::test_an_over_budget_sandbox_keeps_its_reservation
FAILED tests/contract/test_disk_budget_enforcement.py::test_over_budget_report_blocks_writes_and_never_pauses
FAILED tests/contract/test_disk_budget_enforcement.py::test_enforce_disk_budget_does_not_pause_or_release_anything
3 failed, 40 passed in 0.93s
```

**这条变异最值得记**：契约用例 `test_over_budget_report_blocks_writes_and_never_pauses` 是死在
**账本那一行**（`:94`）的，它在它前面的 `assert after.state == "running"` **已经通过了** ——
也就是说"超限不冻结"的 state 断言对这种"归还预留"的回归是**瞎的**，抓到它的是账本断言。
这正是 Task 3 存在的理由。

### 变异 E：`hold_quota` 忘掉 disk 维（"拿不回来"那一半）

```diff
-            self._reserved_disk += dims["disk"]
+            self._reserved_disk += 0  # MUTATION E
```

```
............FFFF.F...............                                        [100%]
=================================== FAILURES ===================================
___________ test_disk_reservation_is_exactly_the_sum_of_live_records ___________
tests/unit/test_pause_quota.py:254: in test_disk_reservation_is_exactly_the_sum_of_live_records
    assert registry.global_reserved()["disk"] == 192
E   assert 0 == 192
___ test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once ____
tests/unit/test_pause_quota.py:267: in test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once
    assert registry.global_reserved()["disk"] == 64
E   assert 0 == 64
______________ test_an_over_budget_sandbox_keeps_its_reservation _______________
tests/unit/test_pause_quota.py:273: in test_an_over_budget_sandbox_keeps_its_reservation
    assert registry.global_reserved()["disk"] == 64
E   assert 0 == 64
------------------------------ Captured log call -------------------------------
WARNING  control_plane.registry.manager:manager.py:1775 sandbox sbx_ebff5ac17b01c6a4 over its workspace budget (128 MiB used of 64 MiB): writes are blocked until it is back inside
___ test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op ___
tests/unit/test_pause_quota.py:289: in test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op
    assert registry.global_reserved()["disk"] == 128
E   assert 0 == 128
__________________ test_ttl_expiry_releases_the_disk_row_once __________________
tests/unit/test_pause_quota.py:314: in test_ttl_expiry_releases_the_disk_row_once
    assert registry.global_reserved()["disk"] == 128
E   assert 0 == 128
=========================== short test summary info ============================
FAILED tests/unit/test_pause_quota.py::test_disk_reservation_is_exactly_the_sum_of_live_records
FAILED tests/unit/test_pause_quota.py::test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once
FAILED tests/unit/test_pause_quota.py::test_an_over_budget_sandbox_keeps_its_reservation
FAILED tests/unit/test_pause_quota.py::test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op
FAILED tests/unit/test_pause_quota.py::test_ttl_expiry_releases_the_disk_row_once
5 failed, 28 passed in 0.41s
```

这一条同时证明 **helper 不会替实现掩盖 hold 侧的洞**：`make_record` 的"售出"本身就是走
`release_quota` + `hold_quota` 写的，hold 一坏，账本立刻是 0。

---

## 3. fixture 落点决定与理由：`tests/unit/conftest.py`（新建）

三个候选都考虑过，选"单元车道的 conftest"，也就是"提升到公共 conftest"里**范围最小**的那一层：

1. **不放在仓库根的 `tests/conftest.py`**。那个文件是服务器 harness（`workspace` / `make_apps` /
   live servers / docker 容器，1400+ 行），每个车道都会加载它。Task 2 已经在
   `tests/contract/test_disk_budget_enforcement.py` 里自建了一个**配置不同**的 `registry`
   （`make_apps` + `default_disk_mb=64` 的 contract app）；根 conftest 再放一个同名的、语义不同的
   `registry`，会让 60 多个契约文件在"想借一个 registry"时拿到一个和它们自己的 app 无关的对象，
   是纯粹的意外来源。
2. **两个用例文件里各建一份**：这正是评审说的"会漂移的副本"。这两个文件问的是同一个对象的同一个
   性质（账本行 == 活记录的数字），一份定义两个用户是这里的最少重复解。
3. **`tests/unit/conftest.py` 已经有两个同名 fixture 的先例**（`test_oci_registry.py`、
   `test_image_cache_sharing.py` 里的 `registry` 是假 OCI registry）。pytest 里文件级定义会遮蔽
   conftest 定义，那两个文件一个字节都不用动；不主动请求 `registry` 的用例完全不受影响（整条单元
   车道跑过：14 failed 全是我没碰的文件，见文首）。

`registry` fixture 的语义是**故意"没有约束"的**：每个池都是 `0`（manager 里 `0` = 无上限），
每记录的默认值取真实值（memory 512 / cpu 100 / processes 64）。这样它提供的任何东西都不能决定一个
断言的走向；**需要"拒"的场景自己建有界的 registry 并写清楚**（只有
`test_a_released_disk_budget_is_bookable_by_the_next_create` 这么做，并把 `max_total_disk_mb=64`、
`default_disk_mb=64` 摆在脸上）。

`make_record` 放在同一个 conftest，做成**工厂 fixture**（函数 fixture 的返回值），调用写法与简报里
的 `make_record(registry, disk_size_mb=64)` 一字不差。它的"售出"实现是本次唯一需要解释的设计：
`registry.create()` 只收 `settings.default_disk_mb`，而同一个账本里要放 64 和 128 两个数，所以
helper 先 create，再 `release_quota(record)` → 改 `record.disk_size_mb` → `hold_quota(record)`
→ `save()`：

* 走的是 pause/resume 用的**同一对公共 API**，不是直接去改 `_reserved_disk` 计数器的"测试后门"，
  所以 helper 造不出"admission 不可能造出的账本状态"；
* 两个后端都成立（fakeredis 的那几条 store 用例也用它）；
* hold 侧一旦回归，账本立刻对不上（变异 E），helper 掩盖不了。

---

## 4. `release_quota` 到底幂不幂等（读代码的结论）

**结论：幂等，而且是"第二条闸门 + 一处单写"的形态；本任务不需要修它。**

`control_plane/registry/manager.py:888-932` 的骨架：

```python
        if record.quota_released:
            return False                      # 闸门：第二次是 no-op，且"什么都没做"
        dims = self._global_dims(record)      # 四个维，含 "disk": record.disk_size_mb
        if self._quota_store is not None:
            self._quota_store.release("global", dims)
            ...                               # 租户行同理
        else:
            with self._lock:
                ... self._reserved_disk = max(0, self._reserved_disk - record.disk_size_mb) ...
        record.quota_released = True          # 只有真正归还过才置位
        for callback in callbacks: ...        # 唤醒 E9.4 等待者只在真归还时发生
        return True
```

判断依据：

1. 闸门在**两个后端之前**，返回值有语义（`True` = 这次真的归还了，`False` = 这次什么都没做），
   调用方（`pause` / `resume` 冲突回滚 / `_release`）都只看返回值就能判"要不要 save"。
2. 内存分支的 `max(0, …)` 是**第二张网而不是替代品**：它只能让某一维不为负，挡不住"吃别人的行"。
   变异 C 就是证据——闸门一拿掉，`pause → delete` 会把**另一个沙箱的 128** 吃掉（`128 → 64`）。
3. `pause` / `delete` / TTL 三条路径确实同源：`pause()` 调 `release_quota` 后 `save`；
   `delete()`/`remove_expired()` 都走 `_release()`，`_release` 先移记录再 `release_quota`
   （顺序是对的：记录先没了，没有再读到一个"看起来还活着"的副本的窗口）。
   共享存储下 `delete`/`remove_expired` 都是**重新从 store 读记录**再走 `_release`，所以闸门读到的
   是 store 里那份 flag，跨副本也成立（`test_shared_store_pause_then_delete_returns_the_disk_row_once`）。
4. **一处不对称，登记不改**：`release_quota` 在共享存储后端只动账本行 + 只改**内存**里的
   `quota_released`，不落盘（落盘一直是调用方的活）。产品里三条路径都在其后 save 或删记录，所以现状
   自洽；但"直接调 `release_quota` 而不 save"（简报那条用例的写法）在 store 后端会留下一个窗口：
   另一个副本从 store 读到的记录仍显示"活着"，而账本已经归还。我第一版
   `test_shared_store_disk_row_is_the_live_records_sum_and_returns_once` 就死在这里
   （`assert 64 == 192`），于是把 store 的那两条改成走真实路径。下面第 6 节把它作为**发现 1** 报上来。

---

## 5. 文件清单

| 文件 | 变化 |
|---|---|
| `tests/unit/conftest.py` | **新增**（97 行）：`ledger_settings()`、`registry` fixture、`make_record` 工厂 fixture |
| `tests/unit/test_pause_quota.py` | +161 行：新增 `-- N30 --` 段，9 条用例；只多一个 import（`workspace_disk_refusal`） |
| `tests/unit/test_tenant_quota.py` | +84 行：新增 `-- N30 --` 段，4 条用例；多两个 import（`timedelta`、`utcnow`） |
| `control_plane/registry/manager.py` | **未改**（`git diff --quiet` 通过；简报 Step 5 里的 `git add` 对这个文件是空的，所以没有 stage） |
| `tests/contract/test_disk_budget_enforcement.py` | **未触碰**（Task 2 的三条契约原样） |

提交只含上面三个测试文件（`git show --stat 43f9085`）：
`342 insertions(+)`，没有删改任何既有断言。

证据与中间产物（都在项目内 `tmp/`，未提交）：`tmp/n30-task-3/mutation-{A,A2,B,C,D,E}-*.txt`、
`tmp/n30-task-3/step4-lane.txt`、`tmp/n30-task-3/unit-lane-after.txt`、
`tmp/n30-task-3/probe-cross-replica-pause-delete.{py,txt}`。

---

## 6. 自审发现

1. **共享存储下"归还账本"与"落盘 flag"不是一步**（发现，未修，见下节担忧 1）。
   `release_quota` 只动 store 的账本行，flag 要等调用方 `save`；产品内三条路径都补了这一步，
   所以三条不变量成立。但在多副本里 `pause` 的两步之间，另一个副本的 `delete` 会对同一份预留
   再归还一次。我用 `tmp/n30-task-3/probe-cross-replica-pause-delete.py`（公共 API、两个副本共享一个
   fakeredis）复现了它，原始输出：

   ```
   ledger after create        : 128
   ledger after A's release   : 0
   ledger after B's delete    : -128 (0 is the truth)
   ```

   这不是本任务三条路径里的任何一条（三条路径都是单进程顺序语义），修它需要 store 侧的
   "只归还一次"原子守卫（或 CAS），属于设计判断，所以我**登记而不动实现**。
2. **不变量 (iii) 在契约层已经有钉子**：Task 2 的
   `test_enforce_disk_budget_does_not_pause_or_release_anything` 已经断言 `global_reserved() == before`
   + `quota_released is False`。我仍然按简报把 (iii) 的账本断言留在单元文件里（它在那里才和另外两条
   不变量并排），并确认两者都被变异 D 打红——不是重复的装饰，是同一个性质在两层的两句话。
3. **内存 clamp 会掩盖"归还两次"的一半**：只有一条记录在账本上时，`max(0, …)` 让二次归还在数字上
   看不出（0 还是 0）。所以三条路径用例都带第二个记录当量具，并且额外断言 `release_quota(...) is False`。
4. **租户行只在租户配了限制时才被 store 分支动**（`_tenant_limits(...) is not None`），所以租户的
   store 用例给了 `max_total_disk_mb`；无限制租户在 store 后端没有行 —— 既有行为，未改。
5. **`make_record` 的"售出"假设 `create()` 一定book得住**（全无界池）。若将来池改成默认有界，helper
   的 `assert ... is True` 会当场炸在"helper 自己的前提"上，而不是悄悄产出一个账本对不上的状态。
6. 简报照抄的三段里 `test_disk_reservation_is_exactly_the_sum_of_live_records` 的 `a` 只用于占账本
   （`192` 这个数就是 64+128）；为了"逐字照抄"保留了未使用的变量名，没有改写成 `_a`。
7. `Σ(活记录)` 这半边在内存用例里用的是字面数字（简报原文），只有 store 用例用了从
   `registry.list()` 现算的求和函数。将来若再加一个"占账本的维度"，字面数字那几条需要跟着人改 ——
   可接受的取舍（新维度本来就应该有人重新想一遍这些数字），但如果评审更想要"算出来"的形态，
   改起来是纯测试改动。

---

## 7. 担忧

1. **多副本的 `pause`/`delete` 窗口**（上面发现 1）：`-128 MiB` 的负预留真的能出现。触发条件窄
   （A 释放账本后、save 前，B 正好 delete 同一个沙箱），但它是"账本不变量"在分布式下的破口，而且
   store 分支**没有 clamp**，负数是可见的。建议：把"这次归还归我"做成 store 侧的原子claim
   （例如 `HSETNX {ns}:quota-released:{id}` 之类的 gate，或把 pause 的两步并成一个 Lua），
   这是 E9.4/多副本那批工作的自然延伸，需要人拍板，我没有在本任务里做。
2. **同一 worktree 里有另一个会话在改**：我跑单元车道时先看到 15 failed / 1328 passed，重跑是
   14 failed / 1329 passed（多出来的是 `tests/unit/test_worker_manifest_permissions.py::test_compose_prod_worker_env_carries_the_fleets_route_b_root`，
   与我没关系；期间还落了一个提交 `28c7f70`）。所以"基线 14 failed"这个数字在有并发编辑时不太稳，
   14 这个数我是复核过的（失败清单全在 `test_priv_helpers`/`test_real_root_gate`/`test_xfs_quotactl_backend`）。
   我的提交只包含我的三个文件，别人的未提交改动原样留在工作区。
3. **本任务只跑了 `tests/unit` + `tests/contract/test_disk_budget_enforcement.py`**，没有跑整个 `tests/`
   （契约/安全车道要 docker 与服务 harness，并且会被上面那个并发会话搅浑）。回归面判断依据是：
   manager.py 零改动 + 这三份测试文件的改动是纯新增。
4. 简报里"三条各自只归还一次"我拆成了 9 条用例（含三个后端/路径的镜像）；如果评审希望
   "一条用例一条不变量"的形态（用 parametrize 收成一个），结构可以合并，语义不变。

---

# 8. N41 追加：归还认领（多副本下同一份预留只归还一次）

**Status**：已修。`pause` 的两步之间（账本行已动、`quota_released` 还没落盘）另一个副本 `delete`
同一沙箱，不再能把同一份预留还第二次。探针从 `-128` 回到 `0`，且"真正移动了账本的归还"恰好一次。
**Commit**：`86d4e92` fix(N41): 归还预留在共享存储上是一次原子认领，多副本不再把同一份预留还两次

判据对照：

| 判据 | 结果 |
|---|---|
| RED：探针复现 `ledger after B's delete : -128` | ✅ `tmp/n30-task-3/probe-cross-replica-pause-delete.RED.txt`（修前原文） |
| GREEN：同一探针 ⇒ 账本回到 0 | ✅ `.GREEN.txt`：`ledger after B's delete : 0`，并多印一行 `returns that moved the rows: {'A': 1, 'B': 0}` |
| GREEN：只归还一次 | ✅ 同一行的计数（修前那次带计数的 RED 是 `{'A': 1, 'B': 1}`，见 `.RED-unwired.txt`）+ 新用例 `release_quota(...) is True/False` |
| 单副本（无 Redis）路径逐条不变 | ✅ 见 8.5：代码 diff 只落在 store 分支；21 个既有用例名逐字不变、逐个 PASSED |
| `tests/unit` failed 名单仍是那 14 条 | ✅ 名单 `diff` 为空（14/14 同名）；参考数 14 failed / 1333 passed / 11 skipped |

## 8.1 守卫的键名与语义

```
e2b:quota:released:<sandbox_id>:<client_id>        # 值 "1"，TTL = QUOTA_RELEASE_CLAIM_TTL_S (600 s)
```

* 沿用现有命名空间约定（`self._ns` = `e2b`，与 `e2b:record:` / `e2b:quota:global` / `e2b:snapshot:copy:` 同族），
  语义段 `quota:released:` 就是它存的那件事："**这份预留已经被（或正在被）归还**"。
* **语义单位是"一次预留"，不是"一个沙箱 id"**。`client_id` 是 `create()` 每次铸一次的
  （`gateway_common.ids.client_id`），pause/resume 不改它、store 往返也带着它，所以
  `sandbox_id + client_id` 精确命名"这一次创建所买下的那份预留"。理由见 8.3 的最后一条。
* 生命周期（全部走 `SETNX`/`DEL` 两个调用，没有读-改-写）：

  | 时机 | 动作 | 谁调 |
  |---|---|---|
  | 归还**赢**了认领 | `SETNX key 1 EX 600` → 才去动账本行 | `release_quota`（store 分支） |
  | 归还**输**了认领 | 什么都不做，返回 `False` | 同上（`if not self._claim_quota_release(record): return False`） |
  | 预留被**重新订回**（resume） | `DEL key` → 才置 `record.quota_released = False` | `hold_quota`（store 分支，且在全局与租户两次 reserve 都成功之后） |
  | 沙箱被删除 / TTL 过期 | **不动这个键** | —— |

* 键名与取值的实现：`SandboxRegistry._quota_release_key` / `_claim_quota_release` /
  `_clear_quota_release`（`control_plane/registry/manager.py`，形态照
  `SnapshotRegistry.try_acquire_copy`，含"`Store 出问题就照旧放行`"的降级取舍）。
* TTL 放在模块常量 `QUOTA_RELEASE_CLAIM_TTL_S = 600`，常量注释写清了取舍：要比它守的那两步
  （微秒级）长几个数量级 —— 因为输的那一方可能是"早就读到未归还副本"的另一个副本 ——
  但不能是永远。

## 8.2 为什么它能关掉那个窗口

缺陷的形状是**两次 store 写之间有一个可读到的中间态**：

```
pause(A) : (1) 账本行 -128         (2) save(record.quota_released=True)
                └─ 窗口 ────────────┘
                       delete(B)：读 store 里的记录 ⇒ 仍是 False ⇒ 以为"还持有" ⇒ 再还一次 ⇒ -128
```

认领把"谁有权动账本"变成一次原子写：**(1) 之前**先 `SETNX`，赢家才走 (1) 与 (2)，输家直接 `False`
返回、账本不动。于是无论 B 读到的是哪个旧副本，它都动不了行 —— 窗口关掉不是因为"两步变成了原子"
（它们没有），而是因为**动账本这件事本身在跨副本语义上只可能发生一次**。

三个刻意的选择：

1. **先认领、后归还**（而不是先归还、后认领）。若赢家在两行之间挂掉：预留仍然**被记着**
   （账本高估 → 只会少卖、不会超卖），等 TTL 后可以再来；反过来则会重复归还（低计 → 超卖）。
   取舍方向与 N41 的危害方向一致。
2. **删除时绝不清除这个键**（评审批示点）。竞态里最后写的是**输家**，如果它在 delete 里清键，
   窗口立刻复开：第三方还能再还一次。这条被用例
   `test_the_return_claim_outlives_the_delete_it_protects_and_has_a_ttl` 钉住（变异 G3 让它红）。
3. **键里带 `client_id`**。TTL 是分钟级，而 id 可以随时被重新使用（测试天天这么干，客户端也能传
   `X-Sandbox-Id`）：只按 id 存会让"同 id 的新记录"第一次归还被误判成重复归还、把行卡到 TTL。
   用例 `test_a_reused_sandbox_id_is_not_blocked_by_the_previous_episodes_claim` 钉这一条。

顺带把"不许只调换两步顺序"也说清楚：反序（先 save 再归还）里，B 若在 A 的两步**之前**读到的仍是旧副本，
它照样会再还一次（A 的归还随后落地 = 第二次），窗口依旧是窗口 —— 只是把"谁读到旧副本"挪了一段。

## 8.3 RED 与 GREEN 的原始输出

### RED（修前基线，公共 API 探针）

`tmp/n30-task-3/probe-cross-replica-pause-delete.RED.txt`（脚本：`tmp/n30-task-3/probe-cross-replica-pause-delete.py`，
两个副本共享一个 fakeredis，全部走公共 API）：

```
ledger after create        : 128
ledger after A's release   : 0
ledger after B's delete    : -128 (0 is the truth)
```

同一条探针在**只加了常量、还没接上认领**时（`tmp/n30-task-3/n41-red-tests.txt`，用例级 RED；带计数的版本见
`probe-cross-replica-pause-delete.RED-unwired.txt`）：

```
.....................F..F                                                [100%]
=================================== FAILURES ===================================
______ test_a_delete_inside_the_pauses_window_cannot_return_the_row_twice ______
tests/unit/test_pause_quota.py:434: in test_a_delete_inside_the_pauses_window_cannot_return_the_row_twice
    assert replica_a.global_reserved()["disk"] == 0
E   assert -128 == 0
_____ test_the_return_claim_outlives_the_delete_it_protects_and_has_a_ttl ______
tests/unit/test_pause_quota.py:487: in test_the_return_claim_outlives_the_delete_it_protects_and_has_a_ttl
    key = registry._quota_release_key(record)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   AttributeError: 'SandboxRegistry' object has no attribute '_quota_release_key'
=========================== short test summary info ============================
FAILED tests/unit/test_pause_quota.py::test_a_delete_inside_the_pauses_window_cannot_return_the_row_twice
FAILED tests/unit/test_pause_quota.py::test_the_return_claim_outlives_the_delete_it_protects_and_has_a_ttl
2 failed, 23 passed in 0.26s
```

### GREEN（修后，同一条探针）

`tmp/n30-task-3/probe-cross-replica-pause-delete.GREEN.txt`：

```
ledger after create        : 128
ledger after A's release   : 0
ledger after B's delete    : 0 (0 is the truth)
returns that moved the rows: {'A': 1, 'B': 0}
```

### 变异取证（守卫自己也要能被证伪）

| 变异 | 做法 | 结果（原始输出文件） |
|---|---|---|
| G1 | `if not self._claim_quota_release(record)` → `if False:`（认领不看） | 探针回到 `-128`、计数 `{'A': 1, 'B': 1}`；2 条用例红：`.RED-unwired.txt` / `n41-mutation-G1-unwired-claim.txt` |
| G2 | `hold_quota` 里不 `DEL`（认领活过预留） | **7 条**红，含两条 N30 的 store 用例 —— 欠计的 bug 换成正号：行再也回不来（`n41-mutation-G2-claim-not-cleared.txt`） |
| G3 | 在 `_release`（delete/TTL）里清认领 | `test_the_return_claim_outlives_the_delete_it_protects_and_has_a_ttl` 红：`assert None == b'1'`，报错文本里正好印出键名 `e2b:quota:released:sbx_n41_claim:cli_a64a47fd7847`（`n41-mutation-G3-claim-cleared-on-delete.txt`） |

G2 那段原文（证明"守卫的另一半"也被钉住）：

```
...................FFFFFF...........F                                    [100%]
=================================== FAILURES ===================================
________ test_shared_store_pause_then_delete_returns_the_disk_row_once _________
tests/unit/test_pause_quota.py:357: in test_shared_store_pause_then_delete_returns_the_disk_row_once
    assert registry.global_reserved()["disk"] == 128
E   assert 192 == 128
_____ test_shared_store_disk_row_follows_the_live_records_and_returns_once _____
tests/unit/test_pause_quota.py:390: in test_shared_store_disk_row_follows_the_live_records_and_returns_once
    assert registry.global_reserved()["disk"] == 64
E   assert 192 == 64
...
7 failed, 30 passed in 0.33s
```

## 8.4 新用例（4 条，`tests/unit/test_pause_quota.py::-- N41 --`）

| 用例 | 钉住的性质 |
|---|---|
| `test_a_delete_inside_the_pauses_window_cannot_return_the_row_twice` | N41 本身：B 在 A 的两步之间 delete，账本必须停在 0（修前 `-128`） |
| `test_the_return_claim_is_dropped_when_the_reservation_comes_back` | 认领必须随预留一起回来：pause→resume→delete 仍是 0→128→0（变异 G2 让它红） |
| `test_a_reused_sandbox_id_is_not_blocked_by_the_previous_episodes_claim` | 认领认的是"这一次预留"：同 id 新记录照样能被归还（只按 id 存会卡住） |
| `test_the_return_claim_outlives_the_delete_it_protects_and_has_a_ttl` | 键不被 delete 清掉、`TTL == QUOTA_RELEASE_CLAIM_TTL_S`、`0 < TTL <= 3600`，且第三次归还尝试仍 `is False` |

## 8.5 单副本（无 Redis）路径不变的证据

1. **代码**：`git diff 86d4e92` 里，`release_quota` 的既有提前返回闸门（`if record.quota_released: return False`）
   与内存 `else:` 分支**一行未动**；新增的那一行 `if not self._claim_quota_release(record): return False`
   只在 `if self._quota_store is not None:` 里面。`hold_quota` 同理（`_clear_quota_release` 只在 store 分支，
   且在两次 reserve 都成功之后）。两个助手都有 `if self._redis is None: return True/return` 的短路，
   且**没有任何调用点**在无 store 时够到它们 —— 单进程路径新增 0 次 Redis 往返。
2. **用例名逐条不变**：`git show HEAD~1:tests/unit/test_pause_quota.py` 与工作区的 `def test_*` 名字集合
   diff 只有 4 行新增（21 → 25），既有 21 条一个不少、顺序不变
   （`tmp/n30-task-3/n41-pause-quota-names-{before,after}.txt`）。
3. **逐条 PASSED**：`pytest tests/unit/test_pause_quota.py -v` 25/25 PASSED，其中 21 条是既有用例
   （`n41-pause-quota-verbose.txt`）——包括所有无 Redis 的内存账本用例
   （`test_pause_frees_capacity_for_a_new_sandbox` … `test_ttl_reaps_running_but_parks_paused`）。
4. **更宽的 lane**：
   - 简报 Step 4 的 4 文件 lane：**63 passed**（N30 时 59，+4 条 N41）；
   - 加上 `tests/contract/test_disk_budget_enforcement.py`：**73 passed**；
   - 配额相关契约 lane（`test_pause_resume_quota` / `test_eviction_api` / `test_create_queue_api` /
     `test_pause_write_gating` / `test_tenant_isolation`）：**39 passed**；
   - `tests/contract/test_redis_multireplica_e2e.py`（真两副本共用一个共享台账）：**7 passed**；
   - `tests/unit` 全量：**14 failed / 1333 passed / 11 skipped**，`FAILED` 名单与改前
     `diff` 为空（14/14 同名，`unit-failed-{before,after}.txt`）。
5. **为什么 N41 的用例落在 registry 层而不是 API 层**：窗口在一个 API 调用（`pause`）内部的两次 store 写之间，
   从 API 外面无法确定性地落进去（等 pause 返回时 flag 已经落盘，B 的 delete 会被既有的 flag 闸门挡住，
   走的就不是认领这条路了）。要稳定命中的话，只能在 registry 层显式构造"归还已发生、记录还没保存"这个中间态
   —— 这也是 Task 2 把契约钉在 registry 层的同一个理由。

## 8.6 文件清单（N41）

| 文件 | 变化 |
|---|---|
| `control_plane/registry/manager.py` | +98 行：`QUOTA_RELEASE_CLAIM_TTL_S = 600`；`_quota_release_key` / `_claim_quota_release` / `_clear_quota_release`；`release_quota` 加认领（含 docstring 说明跨副本那一半）、`hold_quota` 成功后清认领。**内存分支与提前返回闸门未动** |
| `tests/unit/test_pause_quota.py` | +102 行：`-- N41 --` 段 4 条用例 + `_replicas()` 助手 + `QUOTA_RELEASE_CLAIM_TTL_S` import |
| `docs/open-issues.md` | **未改**（你的台账；N41 那行现在仍写"待做"，见担忧 4） |

产物（`tmp/`，未提交）：`probe-cross-replica-pause-delete.{py,RED,RED-unwired,GREEN}.txt`、
`n41-red-tests.txt`、`n41-mutation-G{1,2,3}-*.txt`、`n41-pause-quota-verbose.txt`、
`n41-pause-quota-names-{before,after}.txt`、`n41-step4-lane.txt`、`n41-unit-lane-after.txt`、
`unit-failed-{before,after}.txt`。

## 8.7 担忧

1. **"第二步写失败"的那一半窗口，认领关不掉**（范围外，建议单开一条）。
   守卫保证的是"账本的这一份预留**至多**被归还一次"，它不能保证 `pause` 的第二步
   （`save(record)`）一定写成功：若 `release_quota` 成功了而 `save` 抛错（Redis 抖动），store 里的记录
   仍显示"持有"、账本已经归还 —— 这一次是**账本少计**（Σ 活记录 > 账本），也就是超卖的方向；
   而重试会被认领挡住（TTL 内），所以它会持续到 TTL。要真正关掉它需要 store 侧把"归还账本 + 置 flag"
   做成一次事务（或把 flag 并进账本键），不是认领能覆盖的。注意它与 N41 的量级差别：
   N41 不需要任何错误、纯靠两次正常操作交叠就能复现；这一条要一次写失败。
   **要不要开 N42，请你拍板** —— 我没有扩大范围。
2. **记录会被"复活"**：B 的 delete 先把 store 记录删掉，A 随后的 `save` 又把它写回去（paused、无预留）。
   账本不变量仍然成立（那条记录的 flag 是 True，不占预留），但会留下一条 paused 记录，等它自己再被 delete/TTL 收走。
   这是 `pause` 两步结构本身的既有形状（守卫没有改变它，也没有让它更坏），登记备查。
3. **认领的降级取舍**：`_claim_quota_release` 在 store 抛错时选择"照旧归还"（沿用 `try_acquire_copy` 的
   可用性取舍）。也就是说在一个持续报错的 store 上，N41 的窗口会回来（换来的是不把全fleet的预留卡到 TTL）。
   这是有意的，但如果评审认为"宁可卡住也不能超卖"，把 `except` 那一支改成"拒绝归还"是一行的事。
4. **N41 台账行没动**：`docs/open-issues.md` 是你维护的，我按最小改动没去改它的状态
   （仍写"待做"）。评审确认这次修复后，那一行可以改成"已修（2026-09-26，`86d4e92`）"并把
   8.3 的两段原始输出挂上去；要我做，下一轮说一声。
5. `QUOTA_RELEASE_CLAIM_TTL_S = 600` 是全篇唯一的魔法数：现在它对被守护的两步有几个数量级的余量；
   若将来 `pause` 的两步之间插进更慢的写（或有人给 `pause` 加回调），这个值要跟着一起看。

---

# 9. 评审收口（追加，2026-09-26）：共享存储 × TTL 那一格 + 四条 Minor + 变异 C 的解释更正

**Status**：完成。`control_plane/registry/manager.py` **一字未改**（`git diff --quiet control_plane/registry/manager.py`
通过），全部改动落在 `tests/unit/test_pause_quota.py`：+1 条用例（补上矩阵缺的第 6 格）+ 四条 Minor。
**Commit**：`9610f27` `test(N30/3): 补上共享存储 × TTL 过期那一格，并收掉评审的四条 Minor`

判据：

| 车道 | 结果 |
|---|---|
| 简报 4 文件 lane（`test_pause_quota` / `test_tenant_quota` / `test_ttl` / `test_redis_multireplica`） | **64 passed**（改前 63，+1） |
| 契约文件（Task 2 的钉子，未触碰）`tests/contract/test_disk_budget_enforcement.py` | **10 passed** |
| `tests/unit -q` 全量 | **14 failed / 1336 passed / 11 skipped**，FAILED 名单与基线逐条同名（`test_priv_helpers.py` ×11、`test_real_root_gate.py` ×1、`test_xfs_quotactl_backend.py` ×2） |

原始产物都在 `tmp/n30-task-3-followup/`（未提交）。

---

## 9.1 Important：共享存储 × TTL 过期（新增 1 条）

`tests/unit/test_pause_quota.py::test_shared_store_ttl_expiry_returns_the_disk_row_once`
（放在 N30 段末尾、两条 store 用例之后，约 8 行 + docstring）：把 `test_shared_store_pause_then_delete_...`
的 store 形状复制过来，`redis_client=fakeredis.FakeRedis()`、**第二条记录当量具**（64 + 128）、
`end_at` 拨到过去**再 `registry.save(record)`**（store 清扫枚举的是 store，不是内存字典）、`remove_expired()`；
断言 `expired` 名单、账本 `== 128`、`release_quota(expired[0]) is False`、账本仍 `== 128`、还活着那条的
`quota_released is False`。

### RED（变异：清扫不走 `release_quota`）

变异形状 = 评审说的那个"很自然的重构"：`remove_expired` 的 store 分支**从 store 批量删记录，再把行各自减回去**，
不经过 `release_quota(record)`（所以那条记录的 `quota_released` 永远不会被置位、N41 的认领也不会被取）。
整条 lane 的原始输出（`tmp/n30-task-3-followup/RED-sweep-mutation-lane.txt`）：

```
.....................F..........................................         [100%]
=================================== FAILURES ===================================
____________ test_shared_store_ttl_expiry_returns_the_disk_row_once ____________
tests/unit/test_pause_quota.py:440: in test_shared_store_ttl_expiry_returns_the_disk_row_once
    assert registry.release_quota(expired[0]) is False
E   AssertionError: assert True is False
E    +  where True = release_quota(SandboxRecord(..., sandbox_id='sbx_e42f8c225831493b', ..., quota_released=True, ...))
E    +    where release_quota = <control_plane.registry.manager.SandboxRegistry object at 0x111178050>.release_quota
=========================== short test summary info ============================
FAILED tests/unit/test_pause_quota.py::test_shared_store_ttl_expiry_returns_the_disk_row_once
1 failed, 63 passed in 7.07s
```

**只有这一条红，其余 63 条全绿** —— 这就是缺口当初的形状（delete 用例全绿、TTL 用例在内存后端也绿）。

同一次变异下把危害量出来的原始输出（探针 `tmp/.../probe-sweep-mutation.py`，脚本用公共 API）：

```
ledger before sweep      : 192
ledger after sweep       : 128
swept record quota_released: False
release_quota(swept)     : True
ledger after that call   : 64
```

即：清扫自己把行减回去之后，**记录并不知道自己已经被归还**；紧接着的第二次归还把共享存储的行从 128 减到
64（store 侧没有 clamp），方向是**少计/超卖**——与 N41 同族，但入口换成 TTL 清扫。

### GREEN（还原实现后）

`git diff --quiet control_plane/registry/manager.py` 通过，同一条 lane：

```
................................................................         [100%]
64 passed in 6.53s
```

（`tmp/n30-task-3-followup/GREEN-lane-after-important.txt`）

### 这条断言由谁回答（`probe-store-ttl-flag.py`）

```
ledger after sweep        : 128
swept.quota_released      : True          <- 清扫确实走了 release_quota（本用例要钉的那一半）
claim key present in store: b'1'          <- N41 的认领也在（同一份预留的两道守卫）
release_quota(swept)      : False
ledger after that call    : 128
```

用例钉的是行为"这次归还只发生一次"；清扫回到 `release_quota` 后，flag（和认领）里至少一道会拦下第二次。

---

## 9.2 四条 Minor

### (1) `test_an_over_budget_sandbox_keeps_its_reservation`：给"超限被识别"补精确断言

改前（三行都是"断言不变"形状）：

```python
    r = make_record(registry, disk_size_mb=64)
    registry.enforce_disk_budget({r.sandbox_id: 128 * 1024 * 1024})
    assert registry.global_reserved()["disk"] == 64
```

改后：

```python
    over_budget = registry.enforce_disk_budget({r.sandbox_id: 128 * 1024 * 1024})
    # The premise has to be self-evident: the crossing was *seen* as one.
    assert [x.sandbox_id for x in over_budget] == [r.sandbox_id]
    assert registry.global_reserved()["disk"] == 64
```

证据（变异：`enforce_disk_budget` 永不识别跨越，即那个 `for` 体一开头就 `continue`）：

| | 命令 | 结果 |
|---|---|---|
| 改前 | `pytest tests/unit/test_pause_quota.py -q -k an_over_budget` | **1 passed**（`RED-minor1-before.txt`）—— 评审说的"改成 no-op 它照样绿"复现 |
| 改后 | 同上 | **1 failed**（`RED-minor1-after.txt`）：

```
E   AssertionError: assert [] == ['sbx_d07d068de0adfe62']
E     Right contains one more item: 'sbx_d07d068de0adfe62'
```

### (2) `test_a_released_disk_budget_is_bookable_by_the_next_create`：期望文案字面化

先 `rg`：

```
$ rg -n "shared workspace disk budget exhausted" tests/ docs/ control_plane/
tests/unit/test_sandbox_registry.py:218,260                 <- 单元车道，1024/1536 那对
tests/contract/test_redis_multireplica_e2e.py:229-233       <- 契约车道，1024/1536 那对（字面锚点）
docs/k8s-deployment.md:1251                                  <- 线上实测记录
control_plane/registry/manager.py:63-71                      <- 实现本身
```

契约车道**已有**字面锚点（所以按评审的说法"有锚点就不必换"），但那条锚钉的是另一组数值（1024/1536），
而这句的数值只有本用例覆盖、且比对取自实现符号 ⇒ 一并字面化，让这条用例自己也能红：

改前：`assert str(exc.value) == workspace_disk_refusal(64, 64)`
改后：

```python
    assert (
        str(exc.value)
        == "shared workspace disk budget exhausted: 64 MiB reserved of 64 MiB"
    )
```

（同时把已无用的 `workspace_disk_refusal` 从 import 里删掉。）

证据（变异：把 `workspace_disk_refusal` 的 `exhausted` 改成 `used up`）：

| | 命令 | 结果 |
|---|---|---|
| 改前 | `pytest ... -q -k released_disk_budget` | **1 passed**（`RED-minor2-before.txt`）—— 文案漂了，符号式比对发现不了 |
| 改后 | 同上 | **1 failed**（`RED-minor2-after.txt`）：

```
E   - shared workspace disk budget exhausted: 64 MiB reserved of 64 MiB
E   + shared workspace disk budget used up: 64 MiB reserved of 64 MiB
```

### (3) `test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once`：注明"every path"指什么

改前：函数体只有两次 `release_quota` + 一次 `hold_quota`，名字却像在逐条走路径。
改动**只加 docstring**（名字保留：它是简报里逐字照抄的那三条之一，改名会丢掉这句可追溯性）：

```python
    """The gate every freeing path shares, called directly.

    "Every path" means pause, delete and TTL expiry, and what they share is
    ``release_quota``: this case pins the gate itself (first call ``True``,
    second ``False``, ``hold_quota`` books it back), while the cases below
    drive each of those paths for real, on both ledgers.
    """
```

### (4) `test_disk_reservation_is_exactly_the_sum_of_live_records`：`a` 未使用

改前 `a = make_record(registry, disk_size_mb=64)`，改后 `_a = make_record(registry, disk_size_mb=64)`
（选改名而不是补断言：它是简报里那句 `192 = 64 + 128` 的占位，补一条 `a.disk_size_mb == 64` 只是把
参数原样读回来，不能证伪任何东西）。

(3)(4) 都是纯结构改动、不改变行为，改后 lane 仍 64 passed。

---

## 9.3 变异 C 疑点：结论

**结论：是报告那句解释错了，不是"未被登记的 flag 落地不对称"。**（只改这一句的解释 + 本节结论，
`manager.py` 未动。）

### 查证的代码路径

1. `SandboxRegistry.resume()`（`manager.py:1109`）：`acquired = self.hold_quota(record)` → `record.resume(timeout)`
   → **`self.save(record)`**。
2. `hold_quota()` 的 store 分支（`manager.py:1025-1090`）：两次 `reserve` 都成功之后
   `self._clear_quota_release(record)` + **`record.quota_released = False`**，然后返回 True。
3. `save()`（`manager.py:1716`）：`self._record_store.put(record.sandbox_id, record.to_storage_dict(), ttl=None)`。
   ⇒ **flag 确实落进 store**，`manager.py:1907` 的 `list()` 从 store 读到的就是 `quota_released=False`。

探针（变异 C 打开；两个 registry 共享一个 fakeredis，逐步打印 store 里的 `(quota_released, disk_mb)`；
原始输出 `tmp/n30-task-3-followup/probe-mutation-c-flag.txt`）：

```
after two books                    ledger= 192 ... store={'sbx_bdb43...': (False, 64), 'sbx_aec212...': (False, 128)}
after pause(b)                     ledger=  64 ... store={'sbx_bdb43...': (False, 64), 'sbx_aec212...': (True, 128)}
after resume(b)                    ledger= 192 ... store={'sbx_bdb43...': (False, 64), 'sbx_aec212...': (False, 128)}
after delete(b)                    ledger=  64 ... store={'sbx_bdb43...': (False, 64)}
```

resume 之后 store 里就是 `(False, 128)`，所以 `global_reserved() == live_disk_mb() == 192` 在**未变异**的
实现上本来就成立 —— `test_shared_store_disk_row_follows_the_live_records_and_returns_once` 全绿与
"59 passed"没有矛盾，报告原文那句自相矛盾的解释是错的。

### 那它在 C 下为什么还绿（复跑确认：`1 passed`，`mutation-C-store-cases.txt`）

不是"flag 没落地、数字凑巧抵消"，而是**这条序列里不存在第二次归还**：`pause` 还掉的是第一份持有，
`resume` 又买回一份**新的**持有，`delete` 还的是这一份 —— 三个动作各自恰好归还一次，拿掉幂等闸门也不会多减。
闸门要挡的是"同一份持有被还两次"（`pause` 之后不 `resume` 直接 `delete`），那由内存侧的三条用例负责：
`test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op`、`test_delete_releases_the_disk_row_once`、
`test_ttl_expiry_releases_the_disk_row_once` —— 它们在 C 下全红（`mutation-C-pause-quota-tenant.txt`，7 failed）。

### 顺带量出来的两条新事实（N41 之后）

1. C 下 5 条 `shared_store` 用例**全部绿**（含 `test_shared_store_pause_then_delete_returns_the_disk_row_once`）：
   N41 的归还认领在 store 侧先回答了"这份预留已经在被归还"，flag 闸门与认领是两道守卫、同一条行为。
   所以 §2 末尾"共享存储的幂等性由 `test_shared_store_pause_then_delete_...` 负责证伪"只在 `86d4e92`
   之前成立（§2 已就地更正并指到这里）。
2. C 下 store 侧的失败名单从 8 条降到 7 条，减少的那条正是上一条里的 store pause→delete —— 不是它变弱，
   是它多了一道守卫。

---

## 9.4 文件清单

| 文件 | 变化 |
|---|---|
| `tests/unit/test_pause_quota.py` | +1 条用例（store×TTL，34 行含 docstring）+ 四条 Minor（3 行断言/字面化、1 个 docstring、1 处改名、1 行 import 清理）。**唯一被提交的文件** |
| `.superpowers/sdd/n30-task-3-report.md` | 本追加 + §2 那句解释的就地更正（该目录被 `.gitignore` 忽略，未提交） |
| `control_plane/registry/manager.py` | **未改**（所有变异都已还原，`git diff --quiet` 通过） |

产物（`tmp/n30-task-3-followup/`，未提交）：`RED-sweep-mutation-lane.txt`、`GREEN-lane-after-important.txt`、
`probe-sweep-mutation.{py,txt}`、`probe-store-ttl-flag.{py,txt}`、`probe-mutation-c-flag.{py,txt}`、
`mutation-C-pause-quota-tenant.txt`、`mutation-C-store-cases.txt`、
`RED-minor1-{before,after}.txt`、`RED-minor2-{before,after}.txt`、`unit-lane-after.txt`、
`test_pause_quota.{before,after}.py`、`manager.py.orig`。

---

## 9.5 担忧

1. **租户账本的那一格还空着**：本轮补的是全局账本 store×TTL。`test_tenant_quota.py` 里 store 侧只有
   `test_the_tenant_disk_row_returns_to_zero_in_the_shared_store`（delete），租户 × store × TTL 与 pause
   仍然只有内存版。同一个"清扫不走 `release_quota`"的变异对 `_tenant_dims` 那一半同样无声
   （§9.1 的变异里我把租户 release 也照抄了，仍然没有用例红）。要补是 10 行左右，需要评审点头再动。
2. **`release_quota(expired[0]) is False` 在今天有两道守卫**：flag 与 N41 的认领。单独拿掉 flag 闸门（变异 C）
   时这条新用例**不会红**（认领兜住），单独拿掉认领（G1）时它也不会红。这是有意的纵深，但意味着"store 上
   flag 闸门本身"没有再被单独钉住；若将来认领被重构掉，这条用例会跟着变松。要更硬可以让它同时断言
   `expired[0].quota_released is True`（那是清扫返回对象的身份），我没有扩大改动范围。
3. **工作区里有别人的未提交改动**：`envd_service/executors/sandlock.py`、`tests/unit/test_pure_rootfs_shape.py`
   （本轮的 `1336 passed` 比上一轮报告里的 `1333` 多 3，其中 1 条是我的新用例）。我没有碰它们，也没有把它们
   加进提交；失败名单仍与基线逐条同名。
4. 本任务只跑了 `tests/unit` 全量 + `tests/contract/test_disk_budget_enforcement.py`（10 passed）。没有跑
   `tests/contract/test_redis_multireplica_e2e.py` 等需要真 redis 的契约车道 —— 改动是纯测试新增，
   `manager.py` 零改动，所以回归面判断依据同上一轮。

---

# 10. 二号评审收口（追加，2026-09-26）：租户账本的 `store × pause` / `store × TTL` 两格

**Status**：完成。`manager.py` 仍**一字未改**；两格新用例只落在 `tests/unit/test_tenant_quota.py`。
**Commit**：`1a1dcb3` `test(N30/3): 租户账本补齐共享存储 × pause / TTL 两格`

判据：

| 车道 | 结果 |
|---|---|
| 4 文件 lane（`test_pause_quota` / `test_tenant_quota` / `test_ttl` / `test_redis_multireplica`） | **66 passed**（上一轮 64，+2） |
| 加上契约 `test_disk_budget_enforcement.py` | **76 passed** |
| `tests/unit -q` 全量 | 14 条已知 Linux-only 红**逐条同名**，另有 1 条**别人正在写的新用例**（见担忧 1） |

---

## 10.1 两格新用例与量具怎么放的

| 用例 | 路径 × 后端 | 量具 |
|---|---|---|
| `test_pause_and_the_delete_after_it_return_the_tenant_store_row_once` | 租户 × `pause`（+ pause 后的 delete 必须是无操作）× fakeredis | 第二条记录 64 MiB（同租户同尺寸），账本 128 → pause 后 64 → parked 的 delete 后仍 64 → 另一条 delete 后 0 |
| `test_ttl_expiry_returns_the_tenant_store_row_once` | 租户 × `remove_expired()` × fakeredis | 同上：128 → sweep 后 64 → `release_quota(expired[0]) is False` → 仍 64，且另一条 `quota_released is False` |

量具的放法（评审点名的那个前提）：**账本上必须活着两条记录**。只有被清扫/被暂停的那一条时，
"归还一次"与"归还两次"在数字上都落在 0（内存后端还被 `max(0, …)` 夹住），所以这里第二条记录
（`other`，64 MiB）一直在账上，正确的中间态是 **64**、多减一次会读成 **0**。两条记录都用
`default_disk_mb=64` 造，这样 `make_record` 的"售出"走的是 `create` 的 reserve、**不经过
`release_quota`** —— 下面变异的正是 `release_quota` 的租户分支，如果 setup 走那条路，用例会在
setup 的断言上红（第一版就是 `assert 2240 == 192`、`assert 1088 == 64` 这种形状），而不是红在
被测路径上；把 setup 挪开之后，红点正好落在"pause/sweep 之后账本该是多少"那一行。

## 10.2 RED / GREEN 原始输出

### RED（变异打在 `_tenant_dims` 的归还侧：租户行忘了 disk 维）

```diff
             if tenant_limits is not None:
                 self._quota_store.release(
-                    f"tenant:{record.tenant_id}", self._tenant_dims(record)
+                    f"tenant:{record.tenant_id}",
+                    # MUTATION (temporary): the tenant release forgets "disk".
+                    {k: v for k, v in self._tenant_dims(record).items() if k != "disk"},
                 )
```

`tmp/n30-task-3-followup/RED-tenant-dims-mutation-lane.txt`（4 文件 lane）：

```
_____ test_pause_and_the_delete_after_it_return_the_tenant_store_row_once ______
tests/unit/test_tenant_quota.py:261: in test_pause_and_the_delete_after_it_return_the_tenant_store_row_once
    assert registry._quota_store.get("tenant:t1")["disk"] == 64
E   assert 128 == 64
______________ test_ttl_expiry_returns_the_tenant_store_row_once _______________
tests/unit/test_tenant_quota.py:300: in test_ttl_expiry_returns_the_tenant_store_row_once
    assert registry._quota_store.get("tenant:t1")["disk"] == 64
E   assert 128 == 64
FAILED tests/unit/test_tenant_quota.py::test_the_tenant_disk_row_returns_to_zero_in_the_shared_store
FAILED tests/unit/test_tenant_quota.py::test_pause_and_the_delete_after_it_return_the_tenant_store_row_once
FAILED tests/unit/test_tenant_quota.py::test_ttl_expiry_returns_the_tenant_store_row_once
3 failed, 63 passed in 6.64s
```

三条都是租户 × store 的用例（两格新的 + 上一轮那格 delete），红点全落在"账本该是多少"那一行；
`test_pause_quota.py` 的全局用例与内存版租户用例全绿 —— 这个变异只动租户行，正是这两格补上的那半边。

### RED（变异二：租户行**多减一次**，验证量具没有把伤害藏起来）

```diff
+                    {
+                        **self._tenant_dims(record),
+                        "disk": self._tenant_dims(record)["disk"] * 2,   # MUTATION 2
+                    },
```

`tmp/n30-task-3-followup/RED-tenant-double-release-mutation.txt`：

```
tests/unit/test_tenant_quota.py:261: ... assert registry._quota_store.get("tenant:t1")["disk"] == 64
E   assert 0 == 64
tests/unit/test_tenant_quota.py:300: ... assert registry._quota_store.get("tenant:t1")["disk"] == 64
E   assert 0 == 64
3 failed, 11 passed in 0.23s
```

两格都读出 **0**（而不是被夹到的 0 掩盖：只有一条记录时正确值也是 0，这里正确值是 64）。

### GREEN（还原实现后）

`git diff --quiet control_plane/registry/manager.py` 通过；4 文件 lane `66 passed in 6.90s`，
加契约文件 `76 passed in 7.39s`（`tmp/.../GREEN-lane-after-tenant-cells.txt`）。

### 第三份证据：上一轮那个清扫变异，现在连租户 TTL 那一格一起红

同一个"清扫直接从 store 批量删记录、再把行各自减回去"的变异（§9.1）现在红两条 —— 全局那格（上一轮就有）
与**租户那格（本轮新增）**，`tmp/n30-task-3-followup/RED-tenant-sweep-mutation.txt`：

```
tests/unit/test_pause_quota.py:440: AssertionError: assert True is False
tests/unit/test_tenant_quota.py:301: AssertionError: assert True is False
FAILED tests/unit/test_pause_quota.py::test_shared_store_ttl_expiry_returns_the_disk_row_once
FAILED tests/unit/test_tenant_quota.py::test_ttl_expiry_returns_the_tenant_store_row_once
2 failed, 64 passed in 6.53s
```

## 10.3 "两道守卫"那一句写在哪

按你的选择（不动产品、不为了测试强度去改实现），这句写在**报告**里，两处：

1. 本节：`test_shared_store_ttl_expiry_returns_the_disk_row_once` 里那条
   `release_quota(expired[0]) is False`，**它的证伪力依赖两道守卫都在** —— 一条是记录自己的
   `quota_released`（清扫走 `release_quota` 就会置位），一条是 N41 在 store 里的归还认领
   （`e2b:quota:released:<id>:<client_id>`）。**单独移走任一道，这条断言都不会红**：只拿掉闸门（§2 变异 C）
   时认领兜住（实测这条用例仍 `1 passed`），只拿掉认领（变异 G1）时闸门兜住。要让它恢复"单点可证伪"，
   变异必须**同时**动两道；本报告的选择是保持断言为行为断言（"这次归还只发生一次"），并在此写明前提。
   同一个形状在本轮新增的租户 TTL 格上成立（`RED-tenant-sweep-mutation.txt` 里那两条是从**两道都没了**
   的这个变异里红出来的）。
2. §9.5 担忧 2 同一条观察（先写在上一轮的报告里，本轮把"依赖两道都在"这句点明）。

## 10.4 文件清单

| 文件 | 变化 |
|---|---|
| `tests/unit/test_tenant_quota.py` | +2 条用例（68 行含 docstring）。**唯一被提交的文件** |
| `control_plane/registry/manager.py` | **未改**（两次变异都已还原，`git diff --quiet` 通过） |
| `.superpowers/sdd/n30-task-3-report.md` | 本追加（该目录被 gitignore，未提交） |

产物（`tmp/n30-task-3-followup/`）：`RED-tenant-dims-mutation-lane.txt`、
`RED-tenant-double-release-mutation.txt`、`RED-tenant-sweep-mutation.txt`、
`GREEN-lane-after-tenant-cells.txt`、`unit-lane-after-tenant-cells.txt`、
`test_tenant_quota.{before,after}.py`、`manager.py.orig`。

## 10.5 担忧

1. **全量车道这次是 15 failed**，多出来的一条是**另一个会话正在写的用例**：
   `tests/unit/test_worker_manifest_permissions.py::test_multinode_example_runs_the_fleet_netns_shape`
   （N36 的 multinode compose 那一处）。`git status` 里那个文件是 `M`（**不是我改的**，我的 diff 只碰
   `test_tenant_quota.py`），它断言 `deploy/compose/docker-compose.multinode.yml` 里带
   `E2B_ENABLE_NET_ISOLATION: "true"` 而现在还没有 —— 典型的"先写测试、compose 还没改"的中间态
   （上一轮报告里那条同文件的 `test_compose_prod_worker_env_carries_the_fleets_route_b_root` 现在已绿）。
   把那 14 条已知红逐条比对过：名单一模一样（`test_priv_helpers` ×11、`test_real_root_gate` ×1、
   `test_xfs_quotactl_backend` ×2），只多了这一条。
2. **两格新用例的 `other` 与被测记录同尺寸（都是 64 MiB）**：这是为了让 `make_record` 不去走
   `release_quota`（见 10.1）。代价是"归还时减错成另一条记录的尺寸"这一种错误在这里看不出来 ——
   需要的话把 setup 换成"直接 `_create` 一条 128 MiB"，但那样 setup 又会经过被变异的那条路。
3. 租户账本现在补齐了 `store × {pause, delete, TTL}`；矩阵里仍空的只剩**共享存储 × hold/resume 的租户行**，
   不过那一半由 `test_the_tenant_disk_row_returns_to_zero_in_the_shared_store` 与 §9 的全局 pause/resume/delete
   那条间接覆盖（resume 的租户 reserve 只在 `hold_quota` 里，且被全局 reserve 成功所门控）。
4. 本任务仍只跑 `tests/unit` 全量 + `tests/contract/test_disk_budget_enforcement.py`（10 passed）。
