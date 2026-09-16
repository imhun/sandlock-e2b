# 构建 / 测试 / 部署踩坑清单（防复发）

这里只记**已经真实踩过、并且看起来像"代码坏了"但其实不是**的坑。每条都是
「症状 → 原因 → 做法」。新增一条的判据：它至少骗过一次人（或花掉一轮排查）。

---

## A. 构建（fork wheel / 镜像）

**A1. lane 里看不到 fork 的新特性。**
症状：`sandlock-supervise: policy rejected: policy contains unknown field(s): X`，
或 `Sandbox.__init__() got an unexpected keyword argument 'X'`。
原因：测试镜像是 compose build 出来的，`wheels/fork/` 变了**不会**自动重建。
做法：`./deploy/scripts/build-sandlock-wheels.sh` →
`docker compose -f deploy/compose/docker-compose.test.yml build test-runner`。

**A2. `docker run -v $PWD:/w -w /w … cargo` 报 `failed to create directory /w/target: Not a directory`。**
原因：`third_party/sandlock/target` 是指向容器内路径 `/src/target-linux` 的符号链接。
做法：`-e CARGO_TARGET_DIR=/tmp/ct`。

**A3. macOS 本机 `cargo check --target x86_64-unknown-linux-gnu` 报 `can't find crate for core`，
而 `rustup target add` 却说 "up to date"。**
原因：活动工具链的 sysroot 里没有该 target 的 std（`rustup target list --installed` 的条目可能属于另一个工具链）。
做法：编译检查用 Linux dev 镜像：
`docker run --rm -e CARGO_TARGET_DIR=/tmp/ct -v "$PWD/third_party/sandlock:/w" -w /w sandlock-dev-f17:latest cargo check -p sandlock-core`。

**A4. 新增 fork 策略字段要改全，漏一处报错各不同。**
清单：builder 字段 + 方法 + `Default`、`Sandbox` 的 `From<&Builder>`、`validate()`、`unsupported` 列表、
`POLICY_FIELDS`（**按字典序**）、示例 policy JSON、supervise 解析 + 回读校验、FFI 函数 + 头、
python `_sdk.py`（`_b_*` + `_HANDLED_FIELDS` + 应用点）、E2B `route_b.py::_POLICY_FIELDS`、执行器 kwargs。
对应报错：`POLICY_FIELDS must be sorted` / `example policy must cover every manifest field` /
`policy contains unknown field(s)` / `AttributeError: 'SimpleNamespace' object has no attribute …`（测试桩）。

**A5. 新增开关在 lane 里"没生效"。**
原因：`test-prod-shaped.sh` 只透传它显式认识的宿主机环境变量。
做法：照 `MIRRORS_ENV` / `MEMORY_ENV` / `PIDNS_ENV` 的写法加一段，并且**未设时必须保持未设**
（否则门禁跑的是代码默认形态，而不是线上形态）。

---

## B. 跑测试 / lane

**B1. dev 容器里一切 namespace 测试都红：`unshare(CLONE_NEWUSER): Operation not permitted`。**
原因：`sandlock-dev-*` 用 Docker 默认 seccomp 档，`unshare` 被按 `CAP_SYS_ADMIN` 门控——而那正是
线上 profile 放行的 syscall。
做法：`--security-opt seccomp=$REPO/deploy/seccomp/sandlock-worker.json`（跑的就是线上档）。

**B2. `test_uid_isolation::` 报 `pidfd_getfd: Operation not permitted`。**
原因：Docker 默认 cap 集不含 `CAP_SYS_PTRACE`（写子进程 uid_map 需要对它 ptrace 权限）。
做法：`--cap-add SYS_PTRACE`（lane phase 1 同样这么做）。

**B3. 怀疑自己的改动弄坏了 fork。**
做法：先用干净 worktree 对照，再决定是否归因于本次改动：
`git -C third_party/sandlock worktree add /tmp/sl-head HEAD` → 在 `/tmp/sl-head` 跑同一条测试 →
`git -C third_party/sandlock worktree remove /tmp/sl-head --force`。

**B4. 给 lane 传测试文件路径并不会缩小范围。**
原因：脚本内部是 `pytest tests … "$@"`，文件路径只是追加。用 `-k` 过滤。

**B5. phase 1 红了，phase 2（非 root 形态）根本没跑。**
原因：脚本 `set -e`，phase 1 非零直接退出。看输出里有没有 `==> phase 2 …` 再下结论。

**B6. 冷 lane 首次建箱 `428 warm_required`。**
原因：每条 lane 都是新容器、`E2B_TEST_TMP_ROOT` 容器原生，镜像缓存不持久。
做法：按 428 的提示带 `X-Sandbox-Id` 走幂等建箱（`tests/contract/test_nonroot_route_b.py` 的新用例是范例），
或重跑一次（第二次已预热）。

**B7. 内存/形态契约不要写死。**
上限来自 `E2B_DEFAULT_MEMORY_MB`，尺寸口径在 `tests/_memory_budget.py`；写死 1024 的门禁在
线上 512 形态下是假绿。

**B8. macOS 本机跑 `pytest tests/unit` 可能收集失败**（缺 `mcp` / `fakeredis` 等只在测试镜像里的依赖）。
碰到这些模块的测试要在 lane 里跑。

**B9. 验证"线上形态"必须带形态开关**：`E2B_DEFAULT_MEMORY_MB=512`、`E2B_TEST_NET_ISOLATION=1`、
`E2B_PID_NS=1`（视所要验证的形态），并配合 `PROD_DROP_CAPS=SYS_ADMIN`。

---

## C. 目标机部署 / 远程操作

**C1. `/opt/sandlock/.env` 不能 `source`。**
原因：里面是 compose 变量文件，含 JSON 值（如 `E2B_TEMPLATE_IMAGES={"py311": …}`），会被 shell 当命令执行。
做法：`sed -n "s/^KEY=//p" /opt/sandlock/.env | tail -1` 逐键取值。

**C2. 别走多层引号。**
`run_target "docker exec … python3 -c \"…\""` 很快变成引号地狱（这次踩过：`\\n` 被当字面量、
`Host:` 头未设导致 421）。做法：本地写脚本 → `upload_file` → 远端 `bash` / `venv/bin/python` 执行。

**C3. `run-target.exp` 会把输出里的 "Permission denied" 当成 SSH 鉴权失败。**
症状：命令明明跑完并打印了结果，末尾却出现 `AUTH FAILED` 且 exit 2。
做法：探针别打印该字符串（例如把 traceback 收敛成 `type(exc).__name__`），或重跑一次确认。

**C4. 换 base image digest / 清缓存 = 冷缓存：升级后首批建箱可能 `428 warm_required`。**
做法：等预热或重跑冒烟；别把它当新镜像的缺陷。

**C5. 沙箱命令通道默认只允许 1 条并发。**
症状：`command queue timed out after 30s`（后台 holder + 前台命令）。
做法：一条命令自报状态（在同一个进程里做完并打印），或临时调
`E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX`。

**C6. 目标机脚本要用 bash 跑**（zsh 下 `BASH_SOURCE` 失效，`deploy/scripts/lib/helpers.sh` 依赖它）。

**C7. 灰度/开关的"真源"在清单里。**
netns 开关与 `E2B_NET_BIND_INJECT` 都在 `deploy/stack/docker-compose.prod.yml`；
`upgrade.sh` 每轮都会把清单带过去，所以**改开关要改清单**，只改目标机 `.env` 会被下一轮覆盖。
改完清单别忘同步 `deploy/stack/.env.example`（否则重建部署会静默回退到旧口径）。

---

## D. 探针方法论（沙箱内观测）

**D1. 沙箱内 `/proc` 是虚拟化的**：`ps` 看不到进程、`/proc/net/tcp`/`/proc/self/ns/net` 可能 EPERM。
替代：`socket.if_nameindex()` 看网卡（判别 netns）、读 `/proc/meminfo` 看内存账本、
从 **worker 容器** 的 `/proc/net/tcp` 看监听端口。

**D2. 判断"谁吃了内存"就读箱内 `/proc/meminfo` 的 `MemFree`**（`= 上限 − ledger`），
不要用 RSS/`statm` 反推：sandlock 记的是**匿名映射预留**（线程的 glibc arena 一次 64 MiB，
不 touch 也算）。单位成本可用"同一条命令前后各读一次账本"测出来（见 §2.4.9）。

**D3. 沙箱内普通命令连自己的 loopback 会被策略拒绝（`ConnectionRefused`）**——
这不是 netns 的问题，别拿它当指标（两种形态都一样）。

**D4. MCP `/mcp` 在 create 返回后 2–5 s 才就绪**，期间 500（代理未捕获 ConnectError）。
脚本要轮询到 200 再断言。

**D5. 定位"路径慢还是处理慢"用三段拆分**：客户端与测试自己的服务端各打墙钟时间戳（同一宿主时钟），
去程 / 服务端自身 / 回程一目了然（`tmp/mcp-3way.py` 的思路）。这次就是靠它把 390 ms 归到传输层。

**D6. 探针与清理**：脚本放项目 `tmp/`；跑完 kill 掉所有沙箱、删掉目标机上的探针文件
（`tmp/cleanup*.sh` 有模板），别给下一轮留冷缓存陷阱和残留箱。

---

## E. 工作习惯

**E1. `rg -r` 是"替换"不是"递归"。** `rg -rn "pat"` 会把匹配替换成 `n` 打印，看起来像输出被吞字；
要行号用 `rg -n`。

**E2. `exec_command` 里 `nohup … &` 会随会话结束被杀。** 长任务前台跑，或写成脚本 + `setsid`；
别指望后台任务能在下一次工具调用里继续。

**E3. 长任务输出重定向到 `tmp/*.log` 再 `rg` 关键行**，只 `tail` 会漏掉中段的失败细节
（这次踩过：失败详情在中段，`tail` 只看到结尾的 warnings）。
