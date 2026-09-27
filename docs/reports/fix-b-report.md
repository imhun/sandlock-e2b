# C1 四条 Python 侧记账项（fix-b）报告

分支 `feat/c1-fix-b`（从 main `ff04e76` 起），一个 commit（SHA 见回复）。
未 push、未改分支、未碰集群（全程没有 `kubectl`，也没有 `KUBECONFIG`），
未碰 `deploy/**` 与 `control_plane/**`。

写集（按任务书，只有这四个文件被修改）：

- `envd_service/executors/sandlock.py`
- `envd_service/priv_helpers.py`
- `tests/unit/test_sandbox_secret_ownership.py`
- `tests/unit/test_priv_broker_protocol.py`

## 判据（实际输出）

```console
$ docker run --rm --security-opt seccomp=/Users/polus/project/ai/sandlock-e2b/deploy/seccomp/sandlock-worker.json \
    -v /Users/polus/project/ai/sandlock-e2b/tmp/wt-c1-fix-b:/w -w /w e2b-sandlock-test:latest \
    sh -c 'pytest tests/unit/test_sandbox_secret_ownership.py tests/unit/test_priv_broker_protocol.py tests/unit/test_priv_helpers.py -q -p no:cacheprovider'
test-runner: no /dev/loop-control: XFS gates unavailable
........................................................................ [ 86%]
...........                                                              [100%]
83 passed in 1.37s
```

（改前同一条命令为 `77 passed`；新增 6 个用例，全部落在两个测试文件里。）

## 逐条改动 + 撤销即红证据

证据的取法：把该条实现反向 patch 回旧写法，只跑相关用例，贴实际输出，再恢复。

### A1a「先全部解析、再统一落地」

改法：`_materialize_http_inject` 拆成两段——第一段只做值解析（`${…}`
替换、IAM JWT-SVID 铸造、env 兜底），产物进 `pending`；第二段才
mkdir/unlink/写盘/交主。解析失败因此发生在任何文件落地之前；写盘阶段的失败
仍是 `unlink` + 抛错，语义未变。

新用例 `test_a_later_entry_that_cannot_resolve_lands_nothing_on_disk`
（第 2 条的 `${e2b.identity.tokens.NOBODY}` 无 backing）。

撤销（恢复逐条发布）后的输出：

```console
>       assert host.events == []
E       AssertionError: assert [('unlink', '.../secrets/sbx_1/hdr_first.secret'), ...] == []
E         Left contains 3 more items, first extra item: ('unlink', '.../secrets/sbx_1/hdr_first.secret')
tests/unit/test_sandbox_secret_ownership.py:408: AssertionError
1 failed, 8 deselected
```

即旧写法下第 1 条已被 unlink→创建→交主（3 个事件），恢复修复后绿。

### A1b「`open()` → `chmod()` 之间的 umask 窗口」

改法：`fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)`
配 `os.fdopen(fd, "w", encoding="utf-8")`；删掉 `os.chmod(path, 0o600)`。
模式即创建参数，顺序（mode 先于交主）与既有注释一致，注释同步改成
「The mode lands **while the worker still owns the file**」。

新用例 `test_the_secret_is_created_at_0600_without_a_umask_window`：断言
`("open", path, 0o600) in events`、事件里没有 `chmod`、文件 mode 为 0600。
既有 5 个用例的期望事件序列同步从 `("chmod", path, 0o600)` 改为
`("open", path, 0o600)`——它们因此也成了「撤销即红」的守卫（旧写法多一个
chmod 事件，精确断言直接失败）。

撤销（恢复 `open()`+`chmod()`）后的输出：

```console
>       assert ("open", str(path), 0o600) in host.events
E       AssertionError: assert ('open', '.../hdr_api_example_com_x_api_key.secret', 384) in
        [('unlink', '...'), ('chmod', '...', 384), ('broker_chown', ...)]
tests/unit/test_sandbox_secret_ownership.py:433: AssertionError
1 failed, 8 deselected
```

### A1c「reclaim 的前提要显式检查并点名」

改法：`path.unlink(missing_ok=True)` 之前加一次父目录检查——
`os.stat(secret_dir)`，要求 `st_uid == os.geteuid()` 且
`not (st_mode & stat.S_ISVTX)`；不满足抛 `PrivHelperError`，消息点名
父目录路径、实际 owner uid、实际 mode，并问「who chowned it or set its
mode?」。（`import stat` 是本次唯一的模块级新增 import。）

两个新用例：

- `test_a_parent_directory_owned_by_someone_else_refuses_the_reclaim`
  （父目录被 chown 走）
- `test_a_sticky_parent_directory_refuses_the_reclaim`（父目录带 sticky 位）

撤销后的输出：

```console
FAILED tests/unit/test_sandbox_secret_ownership.py::test_a_parent_directory_owned_by_someone_else_refuses_the_reclaim
FAILED tests/unit/test_sandbox_secret_ownership.py::test_a_sticky_parent_directory_refuses_the_reclaim
E       Failed: DID NOT RAISE PrivHelperError
2 failed, 7 deselected
```

### A3「roots 归一时相对拼写按 worker 的 cwd 解析」

改法：`_require_broker_agreement` 在 `realpath` 比较**之前**要求两侧 root
都是绝对路径（`Path(value).is_absolute()`），任一侧有相对拼写就抛
`PrivHelperError`，消息点名是「the maintenance broker at <socket>」还是
「this worker」以及具体值。docstring 补了「realpath 只对绝对拼写有意义、
部署里全是绝对路径」的说明。绝对路径与符号链接用例行为不变。

两个新用例（daemon 回相对 root / 本地配置相对 root）。

撤销（去掉绝对性守卫）后的输出：

```console
FAILED tests/unit/test_priv_broker_protocol.py::test_a_relative_root_from_the_daemon_is_refused_before_it_is_compared
FAILED tests/unit/test_priv_broker_protocol.py::test_a_relative_local_root_is_refused_before_it_is_compared
E       AssertionError: assert 'the maintenance...of the socket' == 'this worker ...t absolutely)'
2 failed, 29 deselected
```

（撤销后落到旧的 roots 不匹配消息上。）

### A7「注释对齐」

`BROKER_MAX_WALK_RESPONSE_BYTES` 的注释改成两侧同源推导：daemon 侧 walk
上限 64 MiB 未转义（点名的常量是 `deploy/priv/maint.c` 的
`PRIV_MAX_WALK_OUTPUT`；其余 verb 仍归 `PRIV_MAX_OUTPUT`），×6 逃逸
⇒ 线路最多 384 MiB ⇒ worker 侧 512 MiB（约 1.3x）**永远不会先拒掉一个
daemon 认为是合法的答案**，只会拦住「冒充者灌流」。常量值本身未改。

纯注释项，没有可红的测试；撤销即红在这里不适用（其余四项都有上面的实测
输出）。已验证注释内容与本文件代码一致，另见「疑虑 1」。

## 全量回归对比（排除环境性失败）

两边都在同一镜像、同一条命令下跑 `tests/unit`：

| worktree | 结果 |
|---|---|
| `tmp/wt-c1-fix-b`（本分支） | `20 failed, 1684 passed, 16 skipped` |
| `tmp/wt-c1-base`（`ff04e76`，`git worktree` 拉的干净基线） | `20 failed, 1678 passed, 16 skipped` |

失败集合逐条相同（`test_control_plane_network_local.py` 3 条、
`test_deploy_env_examples_are_ignored.py` 5 条、
`test_docs_only_point_at_repo_artifacts.py` 1 条、
`test_migrate_state_base_script.py` 3 条、`test_migration_volume_quota.py` 2 条、
`test_quota_agent_client.py` 1 条、`test_xfs_project_quota_agent.py` 5 条），
基线同样失败，与本改动无关（含环境/顺序相关的 `E2B_PER_SANDBOX_UID`
警告泄漏与 gitignore 相关用例）。passed 差额 +6 正是新增用例数。

另跑 `tests/security/test_egress_proxy.py tests/security/test_fork_network_features.py`：
`5 passed, 2 skipped, 1 error` —— 该 error 是环境性的
（`test_wildcard_allowout_unprivileged_dns_gateway` 需要 `NET_ADMIN`，
容器未给该 capability），2 个 skip 是无 docker daemon，均非本改动引入。

## 契约面

`exec` transport 路径与协议字段逐字未变：`_run` / `_run_exec` /
`_run_socket`、argv 构造、`{"v": 1, ...}` 请求字段都没动；
`_require_broker_agreement` 只是比较前多了一道守卫。没有新增 skip/xfail。

## 疑虑

1. **A7 点名的常量在本分支还不存在**：`grep PRIV_MAX_WALK_OUTPUT
   deploy/priv/maint.c` 在本 worktree 无命中（只有
   `PRIV_MAX_OUTPUT = 256 MiB`）。按任务书它由 C 侧 agent 落地，两条分支
   合并后才与注释一致；若 C 侧最终用了别的名字或数值（不是 64 MiB），
   这段注释需要跟着改。这也解释了为什么这条「撤销即红」只能给注释 diff，
   给不出测试。
2. A7 的「永不先拒合法答案」结论依赖两点同时成立：walk 的未转义上限确为
   64 MiB，且 escape 上界仍是 `BROKER_ESCAPE_BLOWUP = 6`。
3. A1c 的属主判定是与**本进程 `os.geteuid()`** 比较。单测里非 root worker
   是模拟的，所以 `_Host` 对「父目录的属主」也做了模拟（只改写目录、
   不改写普通文件，以免掩盖文件属主断言）；生产路径不做任何模拟。
4. 按纪律没有跑任何 `tests/contract/**` 与集群相关用例；因此本次没有在
   真实 k0s 集群上复验 C1 行为。

## 附录：评审 Minor（混合 env/文件条目的输出顺序）

评审指出 A1a 的两段式重构顺手改了输出顺序（env 条目在第一段就 `out.append`）
⇒ `[env, file, env, file]` 变成 `[env, env, file, file]`。

改法：第一段对 env 条目也 `pending.append((entry, None))`（`None` = 「值已定稿、
不需要落盘」），第二段按输入顺序统一 append，`value is None` 时只补位置。类型标注
改为 `list[tuple[dict, str | None]]`，函数 docstring 补一句「返回条目保持输入顺序」。

新用例 `test_env_and_file_entries_keep_their_input_order`（env → file → env →
file）：断言 `out` 的 `name` 顺序与完整字典（含 `secret`）逐条等于输入顺序，
并断言两次落盘事件也按顺序。

撤销（env 条目回到第一段 append）后：

```console
>       assert [entry["name"] for entry in out] == [
            "hdr_0_env", "hdr_1_file", "hdr_2_env", "hdr_3_file",
        ]
E       AssertionError: assert ['hdr_0_env', ... 'hdr_3_file'] == [...]
E         At index 1 diff: 'hdr_2_env' != 'hdr_1_file'
tests/unit/test_sandbox_secret_ownership.py:552: AssertionError
1 failed, 9 deselected
```

恢复后判据命令 `84 passed`；`tests/unit` 全量 `20 failed, 1685 passed`
（仍是同一批基线失败，passed +1 即本次新增用例）。
