# N41 残余闭合报告：归还账本行 + 打上已归还标记 = 共享存储侧的一次事务

**Status**：完成。`86d4e92`（原子认领）之后剩下的那个窗口（`save` 失败那一半）已按
"store 侧一次事务"闭合；`docs/open-issues.md` 的 N41 行与其"带触发"行按实测改写。

**Commit**：`f6d35d4` fix(registry): the quota return is one store transaction, claim included (N41 residual)

**改动文件**：

| 文件 | 改了什么 |
|---|---|
| `control_plane/registry/redis_backend.py` | 新增 `RedisQuotaStore.release_once`、`RELEASE_ONCE_MAX_ATTEMPTS`、`RedisRecordStore.record_key` |
| `control_plane/registry/manager.py` | `release_quota` 共享分支改走 `release_once`；`_claim_quota_release`（`SETNX`）**删除**；`_clear_quota_release` **保留**；常量注释与 docstring 重写 |
| `tests/unit/test_pause_quota.py` | N41 段注释重写 + 4 条新用例（30 passed） |
| `docs/open-issues.md` | N41 行改为"已收口"，"带触发"那条改为"已关" |

**证据目录**：`tmp/n41-residual/`（RED/GREEN/变异/全量名单原始输出、变异脚本 `mutations.py`）

---

## 1. 残余的机制（为什么 `86d4e92` 关不掉它）

```
pause(A):  (1) SETNX 认领 key                             ┐ 两次 store 写
           (2) 账本行 -128                                 │
           (3) save(record.quota_released=True)   ← 失败    ┘
```

(3) 失败 ⇒ **共享存储里的记录仍然是 `quota_released=False`**（它说"这份预留还占着"），
而 (1) 的认领键带 TTL（`QUOTA_RELEASE_CLAIM_TTL_S = 600`）。TTL 一过，同一份记录被
`delete`（或 TTL 清扫）读到 ⇒"还占着"⇒ 再归还一次 ⇒ 账本**少计**（超卖方向）。

关键是：**认领是"这次归还有没有发生过"的唯一副本**。记录本身那份标志要么没写、
要么是半小时前的旧值，而 paused 记录不会被 TTL 清扫（`remove_expired` 跳过 paused），
所以"记录旧了自然就好了"不成立 —— 这个窗口不会自愈。

## 2. 新形态：`release_once` 一次 WATCH/MULTI 写三样东西

```python
with self._client.pipeline() as pipe:
    for _ in range(RELEASE_ONCE_MAX_ATTEMPTS):          # 有界重试
        try:
            pipe.watch(*ledger_keys, marker_key, record_key)   # 账本 + 认领 + 记录
            if pipe.exists(marker_key):        return False    # ① 认领说"已归还"
            marked, value = self._released_record(pipe.get(record_key))
            if marked:                         return False    # ② 记录说"已归还"
            pipe.multi()
            pipe.set(marker_key, "1", ex=marker_ttl_s)          # 认领（带 TTL）
            pipe.set(record_key, value)                        # 记录上持久那份标志
            for (_, dims), key in zip(rows, ledger_keys):       # 每行 DECR（global + tenant）
                for dim, v in dims.items(): pipe.hincrby(key, dim, -v)
            pipe.execute(); return True
        except WatchError:
            continue
```

* 两个守卫**都在事务里读、都在事务里写**：认领（TTL）挡"读到旧副本的对手"，
  记录里那份 `quota_released`（**没有 TTL**）挡"认领过期之后的重放"。
  判据因此不再依赖认领的寿命 —— 这就是残余被闭合的地方。
* `WATCH` 冲突有界重试（64 次；仓库里 reserve / 限流器用同一套 WATCH/MULTI 模板，
  见 `redis_backend.py:reserve` 与 `control_plane/ratelimit.py:_allow_shared`）。
  耗尽后抛 `RuntimeError`（防御分支，`# pragma: no cover`）。
* 记录那份标志是**对 store 里已有的 JSON 做读-改-写**（只动 `quota_released` 一个键），
  不是把调用方对象整份覆盖回去：别的副本改的其它字段不会被旧副本盖掉。
  记录不存在/不可解析/不是 sandbox 记录（命名空间与卷注册表共用）⇒ 不写、交给认领守。

**为什么签名比简报的示例宽（`rows`，而不是 `scope, dims`）**：一次归还可能落在两个账本
（`global` 与 `tenant:<id>`）。分成两次调用要么需要两个认领键（出现"半归还"态），要么
第二次调用会看见第一次刚写的认领而整份拒绝（租户行永远还不掉）。所以 `rows` 是
`[(scope, dims), ...]`，两份行与两个守卫同属一个事务。单账本时就是一条。

**`_claim_quota_release` 删除、`_clear_quota_release` 保留**：`SETNX` 那半的职责（读守卫 +
写守卫）已经并入 `release_once`，留着只会是第二套语义；而 `hold_quota` 成功之后要清认领
（否则下一轮归还被拒到 TTL），这条只有 `DEL`，保留原样。删掉的 `_claim_quota_release`
里"store 报错就照旧放行"的降级也一并消失 —— 下面是显式取舍。

## 3. store 报错时的显式取舍：**拒绝归还**（不吞错）

`release_once` 不捕获 store 异常（与 `try_claim`、限流器窗口的"照旧放行"**相反**）。
理由：

* 归还没落地 ⇒ 行一个字节没动。此时回答"已归还"= 让调用方以为容量回来了 ⇒ **少计** ⇒
  超卖共享工作区。这正是 N41 整个修复要躲的方向。
* 拒绝只是把预留**留在账上**（高计）：调用方拿到错误、重试一次就回来。高计只会浪费容量，
  不会让沙箱挤爆磁盘。
* 与今天的行为也一致：修前 `_claim_quota_release` 虽然吞错放行，但紧接着的
  `_quota_store.release()` 在 store 挂掉时照样抛 —— 也就是说"store 不可用 ⇒ pause/delete
  报错"本来就是既有可观测行为，本单没有把它变得更严。
* 用例 `test_a_store_that_cannot_record_the_return_keeps_the_row_booked` 把这条钉住：
  store 不能开事务时 `release_quota` 抛 `ConnectionError`，`record.quota_released` 仍是
  `False`、`global_reserved()["disk"]` 仍是 64。

## 4. RED（修前实现 + 最终用例原文）

`tmp/n41-residual/red-final.txt`（脚本：把 `control_plane/registry/{manager,redis_backend}.py`
换回 `f6d35d4^`，再跑用例；跑完即恢复）：

```
=========================== short test summary info ============================
FAILED tests/unit/test_pause_quota.py::test_a_replay_after_the_claim_ttl_cannot_return_the_row_twice
FAILED tests/unit/test_pause_quota.py::test_two_replicas_releasing_at_once_move_the_row_once
2 failed, 28 passed in 0.33s
```

两条的失败点（原文摘录）：

```
        assert registry.release_quota(record) is True
        assert registry.global_reserved()["disk"] == 0
>       assert registry._record_store.get("sbx_n41_ttl")["quota_released"] is True
E       assert False is True
tests/unit/test_pause_quota.py:582: AssertionError
```

```
        racer.arm(lambda: replica_b.release_quota(held_b))
        released = replica_a.release_quota(held_a)
        assert replica_a.global_reserved()["disk"] == 0
        assert replica_b.global_reserved()["disk"] == 0
>       assert released is False  # B got there first, so A stood down
E       assert True is False
tests/unit/test_pause_quota.py:671: AssertionError
```

**这两条红分别证伪什么**（都是"改哪一行会让它红"能回答的）：

1. `test_a_replay_after_the_claim_ttl_cannot_return_the_row_twice` —— **残余本身**。
   修前：归还只把 `quota_released` 写进调用方**内存对象**（store 里那份仍是 `False`），
   认领键删掉（= TTL 过期）后 `registry.delete(...)` 重新读 store 记录、判"还占着"、
   再 `HINCRBY -64`，账本变成 **`-64`**。修后：记录那份标志在同一事务里落地 ⇒ 重放读到的
   就是"已归还" ⇒ 不再动行。
2. `test_two_replicas_releasing_at_once_move_the_row_once` —— **并发只动一次行**。
   注入点：A 的 `EXEC` 之前让 B 的整次归还跑完（`_RaceClient` / `_RacePipeline`，只
   拦 `pipeline().execute()`）。修前 A 的认领是**更早的一次单独 round trip**，所以 A 是
   赢家、B 输掉 —— 行数碰巧是 0，红在"谁有权归还"这条断言上；修后 A 的读与写在同一
   事务里，读到 B 的写 ⇒ WATCH 冲突 ⇒ 重试 ⇒ 认领在 ⇒ A 站下（`False`），行只动一次。
   这条同时是变异 (a) 的判据。

## 5. GREEN（原文）

`tmp/n41-residual/green.txt`（`tmp/testenv/bin/python -m pytest tests/unit/test_pause_quota.py -q`）：

```
..............................                                           [100%]
30 passed in 0.32s
```

新增的四条（问题 3 要求的 RED 至少两条 + `hold_quota` 后再 pause + store 报错取舍）：

| 用例 | 钉住的行为 | 修前 |
|---|---|---|
| `test_a_replay_after_the_claim_ttl_cannot_return_the_row_twice` | TTL 过期后重放不得再归还 | **红**（`-64`） |
| `test_two_replicas_releasing_at_once_move_the_row_once` | 两副本同时归只动一次行（WATCH） | **红**（A 没站下） |
| `test_a_resumed_reservation_can_be_returned_by_a_later_pause` | `hold_quota` 后再 `pause` 能正常归还（新一轮 episode） | 绿（回归钉子） |
| `test_a_store_that_cannot_record_the_return_keeps_the_row_booked` | store 不可用 ⇒ 拒绝归还、预留仍在账上 | 绿（新语义钉子） |

## 6. 变异：4 个，各自红一次

脚本 `tmp/n41-residual/mutations.py`（对实现做一处精确文本替换，跑指定用例，跑完立刻按
原字节恢复），原始输出 `tmp/n41-residual/mutation-*.txt`。

| # | 变异 | 结果 | 红的用例 | 关键断言原文 |
|---|---|---|---|---|
| (a) | 读守卫挪出事务（即**去掉 `WATCH`**，只留"读一下 + MULTI 写回"） | RED | `test_two_replicas_releasing_at_once_move_the_row_once` | `assert replica_a.global_reserved()["disk"] == 0` / `E assert -128 == 0` |
| (b) | 事务里**只写认领键 + DECR**（不写记录那份标志）= 修前的形态 | RED | `test_a_replay_after_the_claim_ttl_cannot_return_the_row_twice` | `assert registry._record_store.get("sbx_n41_ttl")["quota_released"] is True` / `E assert False is True` |
| (c) | **不查认领键**（只看记录；`delete` 先删记录再归还那条就漏） | RED | `test_the_return_claim_outlives_the_delete_it_protects_and_has_a_ttl` | `assert replica_b.release_quota(stale) is False` / `E AssertionError: assert True is False` |
| (d) | 认领 TTL 缩到 **1 s** | RED | 同上（TTL 断言） | `assert client_b.ttl(key) == QUOTA_RELEASE_CLAIM_TTL_S` / `E assert 1 == 600` |

读法：

* (a) 说明 ② 那条用例真的在测"读和写是一个事务"，不是"顺序调用两个 API"。
* (b) 说明 ① 那条用例真的在测"记录上那份持久标志"，即残余本身；(b) 就是修前的实现。
* (c) 说明认领**没有**变成死代码：`delete` 路径先 `_record_store.delete()` 再
  `release_quota`，此时记录键已不在，只剩认领能挡住"拿着旧记录对象的对手"。
* (d) 说明认领的 TTL 仍是被钉住的参数（它的语义见 §8 的残余边界）。

## 7. 全量单测：failed 名单逐条同名（要求 5）

```
$ tmp/testenv/bin/python -m pytest tests/unit -q          # 基线（改动前）
14 failed, 1531 passed, 11 skipped, 2 warnings in 133.42s
$ tmp/testenv/bin/python -m pytest tests/unit -q          # 改完
14 failed, 1535 passed, 11 skipped, 2 warnings in 114.98s
$ diff baseline-failed.txt after2-failed.txt && echo IDENTICAL
IDENTICAL failed list
```

14 条与仓库已知的那批完全相同（`test_priv_helpers.py` ×11、`test_real_root_gate.py` ×1、
`test_xfs_quotactl_backend.py` ×2，全部是 Linux/root-only），逐条名单见
`tmp/n41-residual/{baseline-failed.txt,after2-failed.txt}`。passed 1531 → 1535 = 新增 4 条
（`tests/unit/test_pause_quota.py` 26 → 30）。

## 8. 代码 diff 摘要 / 单副本路径

* `release_quota` 的共享分支：

```
-        if not self._claim_quota_release(record):
-            return False
-        self._quota_store.release("global", dims)
+        rows = [("global", dims)]
         tenant_limits = self._tenant_limits(record.tenant_id, is_admin=False)
         if tenant_limits is not None:
-            self._quota_store.release(f"tenant:{record.tenant_id}", self._tenant_dims(record))
+            rows.append((f"tenant:{record.tenant_id}", self._tenant_dims(record)))
+        if not self._quota_store.release_once(
+            rows, self._quota_release_key(record),
+            marker_ttl_s=QUOTA_RELEASE_CLAIM_TTL_S,
+            record_key=self._record_store.record_key(record.sandbox_id),
+        ):
+            return False
```

* **`else:` 单副本分支逐字节未动**（`git show f6d35d4 -- control_plane/registry/manager.py`
  的 hunk 全部落在 `if self._quota_store is not None:` 之内；`git diff` 里那几行是上下文行，
  没有 `+`/`-`）。`self._quota_store is None` 时既不构造 `release_once`、也没有任何新调用 ⇒
  **无 store 时 0 次新增 Redis 往返**；`_claim_quota_release` 的删除只影响有 store 的分支。
* 认领键名与 TTL 值**没变**（`{ns}:quota:released:{sandbox_id}:{client_id}`、
  `QUOTA_RELEASE_CLAIM_TTL_S = 600`）⇒ 既有 4 条 N41 用例逐条仍绿；常量注释重写为
  "TTL 现在管的是恢复时间，不是重放判据"。

## 9. 残留边界与担忧（诚实清单）

1. **认领 TTL 到期 + 只剩一个内存旧对象**（**仍未关**，同向 = 少计/超卖）：
   对手必须①握着 600 s 之前读到的 `SandboxRecord` 对象、②那时记录已被 `delete` 从 store
   删掉（记录键不在了，持久标志无从查）、③直接对那个对象调 `release_quota`。走公共路径
   的调用方不经过它：`delete()`/`remove_expired()` 都**重新从 store 读**记录，读到的就是
   "已归还"，两次守卫都拒。要真正消掉它得让认领**永不过期**，代价是每个 episode 一个
   永久键（沙箱创建频率 = 键增长速度），以及 resume 侧清认领失败时预留变成永久泄漏；
   本单选了"有界恢复时间"这一侧，并把这条边界写进了 N41 行的"残余/未关"。
2. **镜像方向的窗口（高计，安全方向，未关）**：`resume` 先 `hold_quota`（补账本行 + 清认领）
   再 `save`。若那次 `save` 失败，store 里的记录仍说"已归还"而账本行已经补上 ⇒ 之后
   `delete` 重新读到"已归还" ⇒ 拒绝归还 ⇒ 行**滞留在账上**（高计，只会少卖）。
   这与本单修的窗口是同构的，但方向安全，且它属于 `hold_quota` 的事务化
   （需要把"补账本 + 清记录标志 + 删认领"也做成一次事务）；本单按"最小改动、只闭合 N41
   的少计方向"没有动它。留作登记，触发条件与 N41 原触发一致（真出现超卖/容量异常滞留）。
3. **`_clear_quota_release` 仍吞错**（`DEL` 失败只记 warning）：预留会滞留到认领 TTL，
   方向同样是高计。修前同形，本单未改。
4. **记录写的事务性边界**：`release_once` 只改 store 里那份 JSON 的 `quota_released`，
   其它字段仍由调用方的 `save` 落盘。因此 pause 的两步依然存在（事务 + save），
   但**决定重放安全的那一步已经在事务里** —— 这正是本单的判据。
5. 变异 (d)（TTL → 1 s）只能靠 TTL 常量断言变红，不是行为性红：修后认领 TTL 的行为后果
   需要"持有旧对象 > TTL"才显现（见第 1 条），单测里无法在 1 s 内稳定构造。这一点写进
   报告以免读者以为 (d) 证明了别的东西。
6. **WATCH 冲突重试有界 ⇒ 新增了一条 `RuntimeError` 出口**（`RELEASE_ONCE_MAX_ATTEMPTS = 64`）。
   要撞上需要"同一份记录/账本键在别人手里的读-写窗口内被改 64 次"，而车队只有两个副本、
   一次归还的窗口是亚毫秒级 —— 实际概率极低；真撞上时的行为等同"拒绝归还"（预留留在账上，
   调用方可重试），方向安全。修前没有这个出口（代价是没有事务）。
7. 附带跑了一次 `tests/contract -q` 作为额外保险：**1 failed / 338 passed / 49 skipped**，
   唯一那条 `test_a_refused_tree_is_parked_and_the_row_it_pinned_is_released[lsattr]` 是
   **与本单无关的既有抖动**（失败点在 `envd_service.agent._disk_loop`：用例把
   `agent_mod.time` 换成 `SimpleNamespace` 后，后台磁盘轮询那一拍撞上 `time.monotonic`
   缺失，多出一行 `disk loop tick failed` 日志，被 `_agent_lines(caplog)` 的精确比对抓住；
   单独跑该用例 **2 passed**）。本单要求的是 `tests/unit` 的 failed 名单（§7），contract
   车道不参与判据。

---

**要求逐条对照**：① 单副本分支逐字不变、无 store 时 0 次新增往返（§8）；② store 报错取舍
显式取"拒绝归还"并写进 docstring + §3 + 用例；③ RED 两条（§4）+ `hold_quota` 后再 pause
（§5）；④ 4 个变异各自红一次、原文在 §6；⑤ 全量 failed 名单逐条同名（§7）；⑥ `docs/open-issues.md`
N41 行改写、带触发那条改为"已关"；⑦ 本报告。
