# Task 1 报告：面 A 原语（`as_uid`）+ 单测 + 独立 agent 镜像

worktree：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（`pwd` 与 `git rev-parse --show-toplevel`
均已核验为该路径），分支 `feat/c3-consolidation`，BASE = `be00f74`。
主机解释器：`/Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest`。

---

## 1. 交付了什么

### 1.1 `deploy/priv/as_uid.c`（新，唯一新增的生产 C 文件）

面 A 的**全部**特权能力就是一个二进制：给某个 pid 的 `uid_map`/`gid_map` 各写一行恒等映射
`X X 1`，成功时在 stdout 打印**恰好一行** `C3-ASUID-OK pid=N uid=X`。

- 用法 `as_uid --uid X --pid N`（同时支持 `--uid=X`），沿用 `priv_common.c` 的
  `priv_parse_uid` / `priv_validate_uid` 与 `priv_fail`/`priv_usage` 纪律（**复用，未重写**）：
  uid 必须落在 `E2B_UID_POOL_START..+SIZE` 内，uid 0 在共享解析那里就被拒。
- 四条越界规则，全部**先判后写**、逐条点名（`priv_fail` → 退出码 77，stderr 前缀
  `as_uid: refused: `）：
  1. uid 不在池内（`priv_validate_uid` 的既有文案）；
  2. 目标 map 非空（已写过）；
  3. 目标 map 是初始命名空间的全量区间（没 unshare）；
  4. 即将写下去的字节不是恒等 `X X 1`。
- `uid_map` 与 `gid_map` **都先读、都判完**才动笔，因此坏状态不会写出"半授予"的身份；
  只有 `gid_map` 写在 `uid_map` 之后失败时才会留下半授予，文案点名了这一点。
- 读/写分开两道墙、都 fail closed：读不到（pid 已不在）点名 errno，写不进点名 errno；
  读到的内容超过 4096 字节、字段不是十进制、字段越过 u32 上限、`count == 0`、
  组内多一个数 —— 一律归入"不是本程序能读的 map"，**绝不退化成"空 = 可以写"**。

### 1.2 `tests/unit/test_priv_as_uid.py`（新，9 个用例）

与 `tests/unit/test_priv_helpers.py` **同形**（同样的 helper + 精确断言风格），在**主机**上跑：
用 `cc -O2 -Wall -Wextra` 把**生产翻译单元**编出来（`AS_UID_NO_MAIN` 去掉 `main`，`DRIVER_C`
驱动它自己的纯入口），因此断言的是真实代码而不是规则副本；`build.stderr == ""` 就是
`deploy/priv/` 既有的 `-Wall -Wextra` 门槛。四条拒绝 + 成功行为都被逐字钉住（见 §3）。

### 1.3 `deploy/docker/Dockerfile.agent`（新，独立镜像）

两阶段：builder 用 `python:3.14-slim` + `gcc` 编 `as_uid.c`（+ `priv_common.c`）与 `maint.c`；
final 阶段只 `COPY --from` 两个二进制到 `/var/lib/e2b-priv/`，`0710 root:65534`、文件 `0750`、
`setcap`（在 final 阶段打，`COPY --from` 不携带 xattr），默认 `USER 65534:65534`。
**不复用 worker 镜像**（`Dockerfile.envd`），也没有 `envd_service`/`gateway_common`/egress/沙箱 wheel。

### 1.4 `tests/security/test_agent_image_privilege.py`（新，8 个用例）

容器车道：构建上面那个镜像，用 `getcap`/`stat`/`ls`/`id` 钉镜像内容，并在真 Linux 上做**一次真实授予**
与三条真 `uid_map` 拒绝（见 §4）。

---

## 2. 两条裁定与"测试环境"问题的落地方式

**D1（worker 镜像 pin 推迟到 Task 4）**：已按裁定执行 —— Task 1 只 pin **agent 镜像**
（目录里恰好 `as_uid` + `e2b-maint`、caps 与预期逐字相等）。容器车道文件的 module docstring 末段写了
一行："The other half of the plan's judgement #2 -- that the worker image has no `/var/lib/e2b-priv`
at all -- is C3 Task 4's deliverable"。**没有**对 worker 镜像下任何断言，也**没有**改
`deploy/docker/Dockerfile.envd`。

**D2（拒绝用例 ④ 的形态）**：选了"**编译期切开的生产纯函数**"，没有新增任何生产调用面：

- 生产二进制**自己算** `X X 1`，然后把这行字节送进 `as_uid_check_identity_line()` 校验，
  通过才写 —— 即 ④ 在**写路径上**，不是只在测试里；格式化 bug 会变成拒绝而不是非恒等授予。
- `AS_UID_NO_MAIN` 只做一件事：把 `main` 切掉，让 `tests/unit` 能在主机上直接调用这四个纯入口
  （`as_uid_check_map` / `as_uid_check_identity_line` / `as_uid_identity_line` / `as_uid_ok_line`）。
  这与 `maint.c` 的 `#ifndef PRIV_MAX_WALK_OUTPUT`（A7 的编译期测试开关）同形，且**不在出厂二进制里**。
- 没有 `--map`、没有 `--proc-root`、没有任何"测试专用运行面"。

**测试环境**：主机（macOS）上跑不了 `/proc` 与 `CLONE_NEWUSER`，所以按"规则可离线、内核面在容器里"切两半：
四条规则用主机 `cc` 编生产 TU 来钉（`tests/unit`），文件 caps / 安装形态 / 真实授予用 Linux 容器钉
（`tests/security`）。这台主机有 Docker（`docker info` 可用），所以容器那一半**真的跑过**（§4）。

---

## 3. TDD 证据（RED → GREEN）

### RED（实现之前；把 `as_uid.c` 移开复现同一状态）

```
$ mv deploy/priv/as_uid.c tmp/c3/as_uid.c.hold
$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/unit/test_priv_as_uid.py -q -p no:cacheprovider
...
E       AssertionError: /Users/polus/project/ai/sandlock-e2b/tmp/wt-c3/deploy/priv/as_uid.c is missing
E       assert False
E        +  where False = is_file()
...
ERROR tests/unit/test_priv_as_uid.py::test_a_uid_outside_the_configured_pool_is_refused
ERROR tests/unit/test_priv_as_uid.py::test_the_pool_boundary_is_the_configured_one
ERROR tests/unit/test_priv_as_uid.py::test_an_unwritten_map_is_the_only_one_face_a_writes
ERROR tests/unit/test_priv_as_uid.py::test_a_target_whose_map_is_already_written_is_refused
ERROR tests/unit/test_priv_as_uid.py::test_a_target_that_has_not_unshared_is_refused
ERROR tests/unit/test_priv_as_uid.py::test_a_map_that_is_not_understood_is_never_treated_as_empty
ERROR tests/unit/test_priv_as_uid.py::test_a_non_identity_mapping_is_refused
ERROR tests/unit/test_priv_as_uid.py::test_the_granted_identity_is_the_one_the_worker_asked_for
ERROR tests/unit/test_priv_as_uid.py::test_the_line_this_program_writes_passes_its_own_check
9 errors in 0.15s
```

（同一次 RED 的另一形态：`build.stderr` 非空时也会红 —— 第一版实现把两个 `/proc` 静态函数留在
`AS_UID_NO_MAIN` 之外，`-Wunused-function` 直接把 `assert build.stderr == ""` 打红，随后把它们
移进 `#ifndef AS_UID_NO_MAIN`。）

### GREEN（实现之后）

```
$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/unit/test_priv_as_uid.py -q -p no:cacheprovider
.........                                                                [100%]
9 passed in 2.25s

$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest \
      tests/unit/test_priv_as_uid.py tests/security/test_agent_image_privilege.py -q -p no:cacheprovider
.................                                                        [100%]
17 passed in 10.75s
```

### 逐条拒绝用例（测试名 → 钉住的**逐字**文案）

| # | 用例 | 断言（精确相等） |
|---|---|---|
| ① | `test_a_uid_outside_the_configured_pool_is_refused` / `test_the_pool_boundary_is_the_configured_one` | `uid 9999 is outside the privileged helper uid pool 10000..10999`；`uid 11000 is ...`；`uid/gid must be positive (got 0)`；池改成 20000..20009 时 `uid 10000 is outside ... 20000..20009` |
| ② | `test_a_target_whose_map_is_already_written_is_refused` | `uid_map for pid 4242 already carries a mapping ('10007 10007 1'): a user namespace's map is written exactly once, and face A never rewrites one`（`gid_map` 同形、单独点名） |
| ③ | `test_a_target_that_has_not_unshared_is_refused` | `uid_map for pid 4242 is the initial namespace's full range: this pid has not unshared a user namespace, so there is no new identity to grant` |
| ④ | `test_a_non_identity_mapping_is_refused` | `the mapping '0 10000 1' is not the identity 'X X 1' face A writes`；`'10000 10001 1'` 同；`'10000 10000 2'` → `must map exactly one id`；两 extent → `must map exactly one id`；`'0 0 1'` → `names uid 0: face A hands a pooled uid, never root's` |
| + | `test_a_map_that_is_not_understood_is_never_treated_as_empty` | `'0 0'` / `'not a map'` / `'10000 10000 4294967296'` / `'-1 0 1'` 全部 → `is not a map this program can read (...)` |
| + | `test_an_unwritten_map_is_the_only_one_face_a_writes` | 空 `uid_map`/`gid_map` → `OK` |
| + | `test_the_granted_identity_is_the_one_the_worker_asked_for` | `10000 10000 1\n`、`10999 10999 1\n`、`C3-ASUID-OK pid=4242 uid=10000\n` |
| + | `test_the_line_this_program_writes_passes_its_own_check` | 生产格式化的每一行都能通过生产校验（两者同源，不能漂移） |

容器车道的三条真机拒绝（§4）另把 ①②③ 钉在**真实 `/proc`** 上，文案与上表逐字一致。

---

## 4. 真 Linux 容器证据（getcap 类断言 + 一次真实授予）

```
$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/security/test_agent_image_privilege.py -q -p no:cacheprovider
........                                                                 [100%]
8 passed in 7.80s
```

同一批断言的手工转录（`tmp/c3/c3-face-a-evidence.sh`，输出存 `tmp/c3/c3-face-a-evidence.log`；
worker 形状容器 `--user 65534:65534 --cap-drop ALL`，agent 形状容器 `--cap-add SETUID --cap-add SETGID
--pid=container:<worker>`，即 `hostPID` 的效果）：

```
== worker-shaped container: uid 65534, no capabilities, unshared child ==
C3-PLAIN pid=1
C3-TARGET pid=6

== the image: caps, modes, owners ==
/var/lib/e2b-priv/as_uid cap_setgid,cap_setuid=ep
/var/lib/e2b-priv/e2b-maint cap_chown,cap_dac_override=ep
/var/lib/e2b-priv 0 65534 710
/var/lib/e2b-priv/as_uid 0 65534 750
/var/lib/e2b-priv/e2b-maint 0 65534 750
as_uid
e2b-maint

== the grant: as_uid --uid 10007 --pid 6 (target was pids 1/6) ==
C3-ASUID-OK pid=6 uid=10007
exit=0

== worker side: the slot's own polling setresuid ==
C3-SETRESUID-OK uid=10007 euid=10007

== the outside view: host uid of /proc/6 ==
10007 65534

== refusal 2 (already written) ==
as_uid: refused: uid_map for pid 6 already carries a mapping ('10007 10007 1'): a user namespace's map is written exactly once, and face A never rewrites one
exit=77

== refusal 1 (uid outside the pool) ==
as_uid: refused: uid 9999 is outside the privileged helper uid pool 10000..10999
exit=77

== refusal 3 (pid never unshared: the plain parent, pid 1) ==
as_uid: refused: uid_map for pid 1 is the initial namespace's full range: this pid has not unshared a user namespace, so there is no new identity to grant
exit=77
```

**这张表里最关键的两行**：`C3-ASUID-OK`（一个 65534 + 只有两个 file caps 的进程，
给自己**既不是父进程、也不同命名空间**的 pid 写成了映射）与 `10007 65534`
（**命名空间外**看 `/proc/<pid>` 的属主就是宿主 uid 10007 —— 命名空间内的记账伪造不出这一行）。
gid 仍是 65534 是因为这条链路按设计**只授予 uid**，与 §14.2.7 探针测到的 `10000:65534` 完全一致。

`getcap` 回的是 libcap 的规范形 `cap_setgid,cap_setuid=ep`（`+` 是 `setcap` 的**输入**运算符），
所以两条拼写都被钉住：`...=ep` 来自真机 `getcap`，`cap_setuid,cap_setgid+ep` 来自 Dockerfile 的
`setcap` 实参（文本 pin，不需要 docker 也会跑）。

---

## 5. 改动的文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `deploy/priv/as_uid.c` | 新增（生产） | 面 A 原语；纯规则 + `main`；`AS_UID_NO_MAIN` 只切 `main` |
| `deploy/docker/Dockerfile.agent` | 新增 | 独立 agent 镜像（两阶段，只装两个特权二进制） |
| `tests/unit/test_priv_as_uid.py` | 新增 | 四条规则 + 成功行为，主机可跑 |
| `tests/security/test_agent_image_privilege.py` | 新增 | 镜像内容 pin + 真机授予 + 三条真机拒绝 |

未改动任何既有文件（`git status` 里 `.superpowers/sdd/progress.md` 的改动是控制器写进度时留下的，
本任务未碰、也**不**提交）。报告本身在 `.superpowers/` 下（已被 `.gitignore` 忽略）。

**提交**：`f3f93f2` `feat(c3): 面 A 原语 as_uid + 独立 agent 镜像（Task 1）`
（`git show --stat` → 4 files changed, 1493 insertions(+)；提交后重跑两个文件 → `17 passed`）。

---

## 6. 自审发现

1. **①②③④ 全部先判后写，且都点名**：`main` 里 1 → 4 → (2+3) → 写，顺序与文件头注释一致；
   四条规则的返回值/文案都被测试逐字钉住，不存在"只有测试知道的规则"。
2. **TOCTOU 不靠自检**：读 map 只是**前置检查**，真正的保证是内核对"一个命名空间只写一次"的强制
   （第二个写者在内核里拿 `EPERM`，本程序把它当拒绝）。文件头已点名这条区别。
3. **`AS_UID_NO_MAIN` 不含任何生产行为**：切掉的只有 `main`；`/proc` 读写在 `#ifndef` 内部
   （第一版把它们放在外面，`-Wunused-function` 立刻把 `stderr == ""` 打红，这才修对）。
4. **`mapped != uid` 自检**：校验器返回的 uid 必须等于已校验的 uid，否则拒绝 ——
   防止"校验一行、写另一行"这类改动。
5. **半授予被点名**：`uid_map` 写成功而 `gid_map` 失败时，文案明确要求放弃该 pid，不许换 uid 重试
   （写过的 map 是永久的）。
6. **`ls` 用 `--user 0`**：`0710 root:65534` 对组只有 `x`，用容器默认用户 `ls` 会 EACCES。
   pin 的是**镜像内容**而不是"worker 能看见什么"，所以用 root 列目录是对的（第一次跑就撞上了这个
   `EACCES`，不是猜的）。
7. **`getcap` 文本已核对**：真机输出 `=ep` 且按位序排列，与 brief 里的 `+ep` 拼写不同 ——
   两者都 pin（一处是 image 事实，一处是 Dockerfile 实参），不拿一个去"近似"另一个。
8. **行宽/风格**：新 Python 文件 ≤88 列，C ≤82 列；`cc -O2 -Wall -Wextra` 零 stderr
   （单元车道与 Dockerfile 构建阶段都据此把关）。
9. **测试无 skip、无 `in`/`startswith` 式部分匹配**：容器车道的 docker 门是 `skipif`
   （沿用 `tests/security/test_worker_nonroot.py` 的既有惯例，且这台主机上真的跑了，不是靠 skip 过关）；
   其余断言全部 `==` / 列表整体相等。
10. **回归**：`tests/unit` 全量在主机上跑出 49 failed —— 已用 `HEAD`（`be00f74`）的干净 worktree
    复现**同一批**失败（`test_priv_helpers.py` 11 个是 macOS 上 `os.chown(..., 0, ...)` 的 `EPERM`，
    `test_real_root_gate.py` 1 个是 macOS 无 `libc.so.6`，`test_xfs_quotactl_backend.py` 2 个同理，
    其余同类），与本任务无关；本任务新增的两个文件 17 个用例全绿，`tests/unit/test_priv_helpers.py`
    的失败集合与基线**逐条相同**。

---

## 7. 关注点与缺口

1. **agent 镜像还没进发布流**：`deploy/scripts/build-images.sh` / `build-and-push.sh` 未加这个
   Dockerfile，也还没有 `deploy/k8s/c3-agent.yaml`（Task 3 的 deliverable）。Task 1 的"建镜像"
   由容器车道真的构建并验过，但**没有任何部署会去拉它** —— 这是刻意的，本任务不引入部署面。
2. **worker 镜像里仍有 `/var/lib/e2b-priv/`**：按 D1，这条 pin 与删除动作都在 Task 4。
3. **本任务不验证"跨 pod 反查宿主 pid"**：`--pid` 是调用方给的（容器 pid）；`NSpid` + cgroup 的
   反查与 cgroup 归属是 Task 3 的判据。本报告里的 `--pid=container:` 只是把"agent 看得见 worker 的 pid"
   这件事做成真的，不构成对 Task 3 反查逻辑的验证。
4. **容器车道的 emulation**：本机 docker 拉的是 `linux/arm64/v8`、宿主是 `linux/amd64/v3`
   （OrbStack 模拟）。file caps / userns 语义与架构无关，但"真机 k0s（arm64）"上的复验仍属 Task 3。
5. **没有 unit 层的 `--pid` 解析用例**：`--pid` 的"正十进制、>0"是 usage 级错误（退出码 2），
   目前只在手工转录里出现过（`--uid 10000` 缺 `--pid` → usage）。若要更严，可在驱动里补一条
   `parse-pid` 子命令；我判断它属于"参数形状"而不是四条拒绝规则，故未加。
6. **`as_uid` 不做 gid 身份**：本任务按计划只授予 uid 映射（写 `X X 1`），不 `setresgid`；
   与 §14.2.7 实测的 `10000:65534` 一致，沙箱进程的 gid 归属不在本任务范围。
