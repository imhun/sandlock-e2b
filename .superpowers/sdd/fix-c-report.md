# C1 真机验收缺陷：模板构建状态 404（共享卷记录的非原子写）

**2026-09-27，分支 `feat/c1-fix-c`（从 main `ff04e76` 起）。**

## 0. 结论摘要

| # | 问题 | 结论 |
|---|---|---|
| 1 | `deployment_smoke` 第一次跑 404 `Template build bld_… not found`（重跑即绿）的根因 | 共享卷上的记录文件是"截断 + 写"（`Path.write_text`）。poll 被 Service 轮到另一个副本，落在拥有者正在写的窗口里，`json.loads` 抛 `ValueError` 被吞 → 抛 `UnknownTemplateBuildError` → 404。**设计本身支持跨副本，缺陷只在写的原子性。** |
| 2 | 修法 | 新增 `gateway_common/paths.py::write_text_atomically` / `write_json_atomically`（同目录临时文件 + fsync + `os.replace`），把 7 处"会被另一个进程读到"的记录写全部换过去。 |
| 3 | 同一类、更严重的一处（worker 侧） | `envd_service/runtime/registry.py` 写 `_runtime/<id>/sandbox.json` 同样是截断写；另一个 worker 的 uid 分配器把"半截文件"读成"没有记录"⇒ 同一个 host uid 可能发给两个沙箱（E3.2 每沙箱 uid 隔离**静默失效**）。已一并修。 |
| 4 | 判据 | 容器内固定命令：改前 `184 passed / 0 failed`，改后 **`191 passed / 0 failed, 1530 deselected`**；两次**失败集合完全一致**（`diff` 无输出）。RED 证据见 §4.3。 |
| 5 | 未动的东西 | 控制面副本数（`replicas: 2` + 反亲和 + PDB）、`deploy/**` 一律未碰；集群未连。 |

## 1. 模板构建 404 的根因链条

1. `Template.build` 是**三个请求**（create / trigger / poll），Service 把它们散到两个副本上（F11 之后刻意多副本，`deploy/k8s/control-plane.yaml:33 replicas: 2`）。
2. `control_plane/registry/templates.py::create()` 把 build 记录写进共享卷 `_templates/<tpl_id>/builds/<bld_id>.json`（`_write_build`），`get_build()` 内存里没有就**从卷上读**。注释已经写明"trigger 可能落在另一个副本，只能从卷上找到"⇒ 跨副本是设计的一部分。
3. 但 `_write_build` 用 `path.write_text(json.dumps(build.to_storage_dict(), ...))`：先 `O_TRUNC` 把目标清成 0 字节，再分多次 `write` 写进去。payload 是 build 的 `logs`/`log_entries`，几十 KB 起。
4. poll 落在这个窗口里时，`get_build()` 的磁盘回退在 `json.loads` 上抛 `ValueError`，被 `except (OSError, ValueError, KeyError, TypeError): pass` 吞掉，然后抛 `UnknownTemplateBuildError` → 路由层回 **404 `Template build bld_… not found`**。
5. e2b SDK 不重试，直接把 404 变成 `BuildException`。重跑即绿（新的 build id，而且窗口是微秒级）。

同一形状的读者还有 `template.json`（`get_by_name` 会重扫磁盘）、`snapshot.json`（F11 文档明说"多副本下另一个副本会读记录"，`creating` 状态就靠它对齐）、`_meta/<vol>.json`（无 Redis 的 peer、worker 侧、启动 backfill）、`secret.json`（`_scan_disk`）——一并修掉，见 §3。

## 2. 同一类、更严重的一处：worker 的 uid 池（静默失效）

**症状**：两个 worker 共享同一份 base（k8s 就是 `e2b-worker-0/1` + 同一块 NAS）时，正在被重写的 `sandbox.json` 会对另一个 worker 的分配器短暂"消失"。

**根因**（链条）：

1. `envd_service/runtime/registry.py::register()` 写 `_runtime/<id>/sandbox.json` 用的是 `path.write_text(json.dumps(record.to_dict(), ...))`（截断 + 写）。
2. `envd_service/uid_pool.py::_recorded_uid()` 在 `json.JSONDecodeError` 时**直接 `return None`** —— "空/半截文件"与"根本没有记录"不可区分。
3. `_recorded_uids()` 由 `_recorded_uid` 的答案拼出"这个世界里已经分配出去的 uid 集合"（注释明写：a uid referenced by any record, on any worker sharing the workspace, is never handed out again）。
4. 于是 A 重写记录的窗口里，B 把这个 uid 当**空闲**发出去 ⇒ 两个沙箱拿到同一个 host uid（内核级文件隔离失效）。

**改法**：`register()` 的记录写换成 `write_json_atomically`（§3）。读者一侧（`uid_pool`）**未改**：它的"读不成就当没有"在原子写成立后是正确且便宜的语义，改它反而要处理"文件正在被替换"的伪错误。

**为什么这是静默失效而不是降级**：分配器不会报错，也不会退到"更弱但可见"的保护 —— 两个沙箱各自都"成功地"拿到了 uid，各自都认为隔离成立；只有攻击面变了（可以互读彼此的工作区）。没有任何一条日志、指标或返回值会指向它，唯一的观测方式是事后发现两个沙箱的 host uid 相同。这正是 `docs/production-deployment-standards` 点名的"不是降级，是静默失效"那一类。

## 3. 原子写 helper 与调用点

**位置**：`gateway_common/paths.py::write_text_atomically(path, text)`（`write_json_atomically(path, payload)` 是它的 JSON 包装）。放 `gateway_common/` 是因为控制面与 worker **都要 import** 它，而这里已经在给两边提供 `sandbox_record_path` / `validate_sandbox_id` 一类共享契约；`gateway_common/paths.py` 的模块 docstring 一并从"path safety"扩成"path safety + shared-record publishing"。

**语义**（docstring 里写全了"为什么"）：

* 临时文件与目标**同目录**（rename 必须落在同一文件系统才是原子的），名字 `.<原名>.<8字节随机>.tmp`；
* 权限用 `os.open(..., 0o666)` + 进程 umask，即**与 `write_text` 完全一致**（不是 `mkstemp` 的 0600）——文件的可读者不变，测试用同目录 `write_text` 出来的文件做逐位比对；
* 写完 `flush` + `fsync` 再 `os.replace`：共享 NFS 卷上"名字可见"必须晚于"字节落盘"，否则另一个节点可能打开新名字而读到还在飞的写；
* `os.replace` 之前的任何异常都会 `unlink` 临时文件并原样抛出 ⇒ **失败时目标保持旧内容、不留半截目标文件**（有专门用例，见 §4）。

**改掉的 7 处**（都是"另一个进程会读"的记录文件）：

| 文件 | 调用点 | 读者 |
|---|---|---|
| `control_plane/registry/templates.py` | `_write_build`（`_templates/<tpl>/builds/<bld>.json`） | 另一个副本的 status poll（本单的主缺陷） |
| `control_plane/registry/templates.py` | `_write_record`（`template.json`） | 另一个副本的 `get_by_name` 重扫 |
| `control_plane/registry/snapshots.py` | `_write_record`（`snapshot.json`） | 另一个副本（`creating` 状态靠它对齐，F11.3） |
| `control_plane/registry/volumes.py` | `_write_record`（`_meta/<vol>.json`） | 无 Redis 的 peer、worker 侧挂卷、启动 backfill |
| `control_plane/registry/volumes.py` | `_tombstone_path`（`_meta/<vol>.deleted`） | 只读存在性，但它是"删掉的卷不许复活"的唯一凭据 |
| `control_plane/registry/secrets.py` | `_persist_record`（`<sec>/secret.json`） | 另一个副本的 `_scan_disk` |
| `envd_service/runtime/registry.py` | `register()`（`_runtime/<id>/sandbox.json`） | 另一个 worker 的 uid 分配器（§2） |

## 4. 测试（TDD，7 条新用例，全部"撤销即红"）

### 4.1 新增用例

```
tests/unit/test_template_build.py::test_a_replica_reading_during_a_write_never_sees_half_a_record
tests/unit/test_template_build.py::test_a_poller_inside_the_write_window_reads_the_previous_build
tests/unit/test_template_build.py::test_a_template_record_is_published_in_one_step
tests/unit/test_template_build.py::test_a_publish_that_fails_leaves_the_previous_record_intact
tests/unit/test_snapshot_registry.py::test_a_snapshot_record_is_published_in_one_step
tests/unit/test_volume_registry.py::test_a_volume_record_is_published_in_one_step
tests/unit/test_uid_pool.py::test_a_uid_is_not_lost_while_the_registry_record_is_rewritten
```

* **并发**（第 1 条）：写者线程循环 40 轮"发 build（每轮 +2 KB 日志，payload 早已超过单次 write）+ 发 record"，读者线程同时反复 `json.loads(path.read_text())` 两个文件。断言：**观测到的每一份都是完整文档**（`failures == []`），且 80 次更新**每一次都经过同目录 rename**（`publish_spy.calls` 精确 80 条、dst 集合精确、src.parent == dst.parent）。
* **原子性/因果**（第 2、5、6、7 条）：不是"抢窗口"，而是**站在窗口里**——`publish_spy.hold_next_publish()` 让写者在"新字节已落到旁路文件、尚未 rename"这一刻停住，读者此时读**已发布的路径**，必须拿到**上一份完整记录**（build 状态仍是 `building`、snapshot 仍是 `creating`、volume 仍是旧 payload、`uid_pool._recorded_uid` 仍指向旧 uid）；随后释放，再断言读到新内容。
* **失败面**（第 4 条）：把 `os.replace` 换成抛 `OSError`，`registry.save(record)` 必须抛出、`template.json` 必须仍是旧内容、目录里**不许留下 `*.tmp`**。
* **后果钉死**（第 7 条后半）：直接写一份半截 `sandbox.json`，断言 `_recorded_uid(...) is None` 且 `_recorded_uids(...) == set()` —— 即"半截 = 没有 = uid 空闲"，把 §2 的静默失效机制写成可读的断言，而不是只在注释里。

### 4.2 确定性是怎么做到的

`tests/unit/conftest.py` 新 fixture `publish_spy`（连带 `_PublishSpy`）包装 `os.replace`，它同时提供两件东西：

1. `hold_next_publish()` 上下文 —— 用两个 `threading.Event` 把写者冻在 `os.replace` **之前**，于是"半截目标文件"这一刻可以被读者**确定性地**观察到，不靠 `sleep`、不靠 payload 大小碰运气；`with` 退出（含断言失败）一定释放写者，线程是 daemon，红的时候不会挂住 pytest。
2. `await_publish(entered, subject)` —— 断言写者**确实走到了这一步**。这是 RED 的确定性来源：`Path.write_text` 根本不会调用 `os.replace`，所以旧实现下窗口永远不会出现，用例以一条解释性信息失败（"this write never reached a rename at all"），而不是时快时慢。

undo 探针同样确定：把 7 处调用点还原成 `write_text`（`git stash push -- control_plane gateway_common envd_service`），7 条全红；`git stash pop` 后全绿。

### 4.3 实际输出

判据命令（容器内，工作树直挂）：

```
docker run --rm --security-opt seccomp=/Users/polus/project/ai/sandlock-e2b/deploy/seccomp/sandlock-worker.json \
  -v /Users/polus/project/ai/sandlock-e2b/tmp/wt-c1-fix-c:/w -w /w e2b-sandlock-test:latest \
  sh -c 'pytest tests/unit -q -p no:cacheprovider -k "template or registry or build"'
```

* **改前**（`ff04e76`，未加用例）：`184 passed, 1530 deselected in 10.93s`
* **只加用例、未修实现（RED）**：`7 failed, 184 passed, 1530 deselected in 33.08s`，失败名单就是 §4.1 的 7 条（逐条 `FAILED` 见下）
* **修完（GREEN）**：`191 passed, 1530 deselected in 9.10s`

RED 的失败名单（实际输出）：

```
FAILED tests/unit/test_snapshot_registry.py::test_a_snapshot_record_is_published_in_one_step
FAILED tests/unit/test_template_build.py::test_a_publish_that_fails_leaves_the_previous_record_intact
FAILED tests/unit/test_template_build.py::test_a_replica_reading_during_a_write_never_sees_half_a_record
FAILED tests/unit/test_template_build.py::test_a_poller_inside_the_write_window_reads_the_previous_build
FAILED tests/unit/test_template_build.py::test_a_template_record_is_published_in_one_step
FAILED tests/unit/test_uid_pool.py::test_a_uid_is_not_lost_while_the_registry_record_is_rewritten
FAILED tests/unit/test_volume_registry.py::test_a_volume_record_is_published_in_one_step
```

**改前/改后失败集合一致**：另外跑了整条 `tests/unit`（同一容器、不加 `-k`）作对照，逐条对 `FAILED` 行做 `diff`：

```
改前（HEAD 的 pristine 拷贝）: 20 failed, 1678 passed, 16 skipped in 75.46s
改后（本单工作树）          : 20 failed, 1685 passed, 16 skipped in 79.20s
$ diff tmp/fail-before.txt tmp/fail-after.txt && echo IDENTICAL
IDENTICAL failure sets (      20 lines)
```

这 20 条是**容器环境的既有红**（`test_deploy_env_examples_are_ignored`、`test_docs_only_point_at_repo_artifacts`、`test_migrate_state_base_script`、`test_migration_volume_quota`、`test_control_plane_network_local`、`test_xfs_project_quota_agent`、`test_quota_agent_client` —— 都依赖容器里看不到的 git 元数据 / XFS 门禁），与本单改动无关，数量与名单两次一致；`+7 passed` 就是本单新增的 7 条。原始输出留在 `tmp/unit-before.txt`、`tmp/unit-after.txt`。

## 5. 扫过、但**没有**改的同类调用点（列出来而不是盲改）

`rg -n 'write_text\(json\.dumps|json\.dump\(' envd_service/`、`rg -n 'write_text\(|write_bytes\(' envd_service/ control_plane/`，以及 `rg -n 'write_text\(json\.dumps' control_plane/` 的全部命中逐个判过：

| 位置 | 判断 |
|---|---|
| `envd_service/runtime/checkpoint_store.py:252` | 写的是 `<...>/last-restore.json`，**已经是** `tmp.write_text(...)` + `os.replace`（同目录）。同一类，已安全，不改。 |
| `envd_service/uid_pool.py:459` | 预留标记 `_write_reservation`，同样已是 `tmp` + `os.replace`。不改。 |
| `envd_service/runtime/image_resolver.py:1554` | `_write_shared_file` 已按"旁路 + rename"写共享缓存；`:551` 的 `.complete` 是**只读存在性**的发布标记。不改。 |
| `envd_service/agent.py:1087` | 隔离区的 `<id>.reason`：给人看的单行说明，best-effort，没有解析者。不改。 |
| `envd_service/agent.py:3526` | `_migrate/<id>.tar.gz` 是**同进程**接着就解包的暂存文件，用完即删，不是被别处读的记录。不改。 |
| `envd_service/executors/sandlock.py:242` | 写 `/proc/self/*_map`，不是文件记录。不适用。 |
| `control_plane/api/sandboxes.py:2046` | `tar_path.write_bytes(...)` 是同一请求内的导出/导入暂存文件。不改。 |
| `control_plane/api/templates.py:158`、`:216` | `~/.docker/config.json`（buildctl 凭据）与构建上下文里的 `Dockerfile`：确实是**另一个进程**（buildctl 子进程）读，但它们是**本 pod 自己的**中间产物、不在共享卷上、也不在本单的写集（`control_plane/api/**`）内，**未改**。若将来发现 buildctl 读到半截凭据的实例，再单独处理。 |
| `envd_service/runtime/context.py:661` | `<workspace>/etc/mcp-gateway/.token`：SDK 走 files API 读它，可能读到半截 token（表现为一次 401，可见且有界）。它在**沙箱自己的树里**、且不属于"记录文件"，写集也没给。**列出待裁定**，本轮未动。 |
| `control_plane/registry/manager.py` | 沙箱记录走 Redis（`RedisRecordStore`），本文件没有任何磁盘写。不适用。 |

## 6. 残余与疑虑

1. **崩溃时的残留**：`os.replace` 之前进程被 kill，会留下一个 `.<原名>.<hex>.tmp` 隐藏文件。它不落入任何扫描的 glob（`template.json`/`snapshot.json`/`secret.json` 都是精确文件名，`_meta/*.json` 要求 `.json` 结尾，envd 侧按精确路径读），所以**语义上无害**，只是垃圾；本轮不加清理任务（会引入新的扫描面）。
2. **成本**：每次发布多一次 `fsync` + 一次 rename。build 的日志行路径（`save_build`）在一场构建里是几十次量级，相对 buildkit 构建本身可忽略；如果将来有 profile 显示它成了热点，可以按"日志行不 fsync、状态迁移 fsync"分级 —— 本轮不做，先要正确性。
3. **跨节点原子性**：同目录 rename 的原子性依赖共享卷（NFSv4.0）的语义，`docs/control-plane-multi-replica.md` §F5 已把这条列成"任何依赖共享文件锁/原语的协调"的天花板；本单的写法只依赖"rename 原子"，比文件锁弱，是这份卷能提供的。
4. **`_recorded_uid` 的语义没动**：它现在安全的前提是"写者原子"；若将来有人再在共享卷上加一条非原子的记录写，同一个坑会回来 —— helper 的 docstring 就是给那一刻看的。
5. 控制面副本数、反亲和、PDB、`deploy/**` 全部未动（本单只在容器内跑测试，未连集群）。
