# 构建 / 测试 / 部署踩坑清单（防复发）

这里只记**已经真实踩过、并且看起来像"代码坏了"但其实不是**的坑。每条都是
「症状 → 原因 → 做法」。新增一条的判据：它至少骗过一次人（或花掉一轮排查）。

> **引用约定（2026-09-27 更新）**：正文里的 `tmp/**` 都在仓库 `.gitignore` 里（**不是仓库路径**）。
> 可重跑脚本已迁到 [`deploy/scripts/acceptance/`](../deploy/scripts/acceptance/)（原名不变）；
> `tmp/**.log` 一律是**原始日志**（会被清、可重跑，脚本见 `deploy/scripts/acceptance/`）。

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

**A6. fork 新增字段后 lane 全绿，但 fork 自己的门禁编译不过。**
症状：`cargo test -p sandlock-core --lib` 报 `error[E0063]: missing field X in initializer of …`
（2026-09-16 实测：`net_bind_inject` 加了字段、改了生产侧初始化，漏了 `seccomp/dispatch.rs` 与
`resource.rs` 里两个 `#[cfg(test)]` 的 `NotifPolicy` 字面量）。
原因：E2B lane 与 fork 门禁**编译的目标不一样** —— lane 走 `--test integration` + wheel 里的
`sandlock-supervise`，`--lib` 的测试字面量它根本不碰；`fe492be` 的 bind-injection 证据正是来自
这两条路径，所以断在 tip 上没人看见。
做法：fork 改动按完整门禁验（`docker run --privileged -v "$PWD/third_party/sandlock":/src -w /src
sandlock-dev:latest sh scripts/test-all.sh`；该镜像 entrypoint 自动降到 uid 65534，根相位另跑
`--oci-root` / `--supervise-root` / `--mediation-2uid`）。新增字段时把测试字面量一起搜：
`rg -n "NotifPolicy \{" crates/`。

**又一个形状（2026-09-22，N25/C 的 `max_file_size`）**：同一个错误可以躲过 `--lib`、只被
**release 构建门 + oci 相位**抓住。症状不是 `--lib` 红，而是 `cli_build: suite FAILED`，日志里是
`error[E0063]: missing field 'max_file_size' in initializer of sandlock_core::init::Req`
（`--oci-root` 相位另报同族的 5 处测试字面量）。原因：`Req::RunExec` 的构造点分布在
`sandlock-oci`（1 处生产 + 4 处 `#[cfg(test)]`），而默认相位的 `--lib`/`--test integration` 都不
编译那个 crate。做法：**新增字段时按"谁构造这个结构体"全局搜**（`rg -n "Req::RunExec \{" crates/`），
而不是只搜 fork 自己那两棵 crate —— 上一条做法里那句 `rg` 要按字段所在的结构体改，不是照抄。

**A7. sandlock 构建的唯一口径：在 `third_party/sandlock` 里跑，仓库根不再产出任何 target。**
（2026-09-30 立此条：当天从根目录 + fork 里一共清掉 **86 GB** 构建产物，并把两个把产物写到根目录
的接线改掉了。没有这条记录，下一个人只能从零把下面这些重新推一遍。）

| 用途 | 入口 | 工具 / 镜像 | 产物落点 |
|---|---|---|---|
| 发布 wheel（镜像装的就是它） | `./deploy/scripts/build-sandlock-wheels.sh` → fork 的 `python/build-wheels.sh` | `docker buildx` builder **`multiarch`**；`third_party/sandlock/python/wheel-builder/Dockerfile`；BASE_IMAGE **`quay.io/pypa/manylinux_2_34_x86_64`**（宿主 x86_64）/ **`..._aarch64`**（宿主 arm64），`--platform` 必须等于宿主架构；镜像内：rustup（`RUSTUP_DIST_SERVER=rsproxy.cn`）+ cp314 + `setuptools-rust` + `ziglang`（配合 `python/wheel-builder/zigcc` 与 `cargo-config.toml` 的 per-target linker）+ `auditwheel` | **`wheels/fork/`**（E2B 侧，`build-and-push.sh` 缺它就直接退出）：`*.whl`、`supervise/{x86_64,aarch64}/sandlock-supervise`、`restore-stub/*/restore-stub`、`SHA256SUMS.supervise`；staging context 在 `third_party/sandlock/tmp/wheel-context` |
| fork 门禁（全套相位） | `deploy/scripts/fork-gate.sh`（`--one 'test_chroot::'` 单跑一族） | 镜像 **`sandlock-dev-f17:latest`**（本机装的是 E2B 测试镜像，脚本自己降权）；容器把**仓库根**挂成 `/src`，**cwd = `/src/third_party/sandlock`** | `CARGO_HOME`/`HOME`/`CARGO_TARGET_DIR` 全在 fork 内：`third_party/sandlock/{tmp/cargo-home,tmp/home,target-linux}`；日志 `tmp/k0s/fork-gate.log` |
| 单点 cargo check/test | 见 A3 | `sandlock-dev-f17:latest`，`-v "$PWD/third_party/sandlock:/w" -w /w -e CARGO_TARGET_DIR=/tmp/ct` | 容器内 `/tmp/ct`（**必须显式给 `CARGO_TARGET_DIR`**，见 A2） |
| aarch64 真内核车道 | `deploy/scripts/arm-lane/xbuild.sh` / `phase-run.sh`（**VM 怎么起见 A9**） | 镜像 **`sandlock-zig-builder:local`** —— 它由**同一份** fork 配方建出来（`--target build`，见下） | 交叉产物 `/var/tmp/aarch64-target/<triple>`；`phase-run.sh` 再把它软链成 `/src/target-linux`（该车道的 `/src` = fork） |

**交叉编译的配置只有一份**：`third_party/sandlock/python/wheel-builder/`
（`Dockerfile` + `cargo-config.toml` + `zigcc`）。它同时是两件事的来源：

- 发布 wheel —— `python/build-wheels.sh` 用它 `-o type=local,dest=…`；
- aarch64 车道的工具镜像 —— 同一份 Dockerfile 的 `build` 阶段（在 fork 根下跑）：

```bash
cd third_party/sandlock
docker buildx build --builder multiarch --platform linux/amd64 \
  --build-arg BASE_IMAGE=quay.io/pypa/manylinux_2_34_x86_64 \
  --target build -t sandlock-zig-builder:local \
  -f python/wheel-builder/Dockerfile --load .
```

父仓曾经还有一份**副本** `third_party/sandlock-wheel-builder/`（`Dockerfile` + `cargo-config.toml`
+ `zigcc`，其中后两个与 fork 的**逐字节相同**）。它 2026-09-09 就被标 SUPERSEDED（出的 wheel 不带
`sandlock-supervise`，install 正常但 route B 静默拒绝起槽位 —— `build-sandlock-wheels.sh` 头部记着
这次实测），**2026-09-30 已删除**：两份几乎相同的配方正是"下一个人照着错的那份跑"的来源，而
`tests/unit/test_one_sandlock_wheel_recipe.py` 现在钉住"父仓里不许再出现第二份"。

**符号链接的边界（两根都叫 `target`，别混）**：

- `third_party/sandlock/target -> /src/target-linux` 是**容器路径**符号链接，**保留**：aarch64 车道的
  相位靠它找 `<fork>/target/debug/...`（fork 挂成 `/src` 时它就指向 fork 自己的 `target-linux/`）；
- 它也是 A2 那个坑的来源：容器把**仓库根**挂成 `/src` 时，它指向的是**仓库根**的 `target-linux/`。
  `fork-gate.sh` 以前正是这样把 16 GB 产物写到根目录的，2026-09-30 已显式加上
  `CARGO_TARGET_DIR=/src/third_party/sandlock/target-linux` 改正；仓库根那根 `target` 符号链接已删。
- 结论：**在容器里跑 cargo 一律显式给 `CARGO_TARGET_DIR`**（指向 fork 内），别依赖这两根链接。

**清干净之后的代价（已实测）**：`third_party/sandlock/tmp/cargo-home` 是 `--offline` 用的 crate 缓存
（399 MB），随 `tmp/` 一起删掉后，**第一次门禁/构建要联网**重新拉：

```bash
docker run --privileged --rm --entrypoint sh -v "$PWD":/src -w /src/third_party/sandlock \
  sandlock-dev-f17:latest -c 'export PATH=/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin; \
    CARGO_HOME=/src/third_party/sandlock/tmp/cargo-home cargo fetch --locked'
```

（2026-09-30 实测：401 MB，几分钟。宿主 `~/.cargo/registry`（623 MB）也可以挂进去复用。）

**A8. sandlock 相关镜像的清单：谁是谁、谁建的、什么时候才该重建。**
这些镜像**不是每次构建都会产出的东西**，反复重建既费时，又会让"哪张才是规范形态"变得模糊。
下表按「用途 → 出处 → 重建策略」记；`tests/unit/test_tool_images_are_documented.py` 钉住
"脚本里默认用的工具镜像都必须出现在这一节里"（极少数由脚本自己在同轮现烤的 tag、或某轮的历史
快照 tag，在钉子里按"带原因的白名单"单独记）。

| 镜像 | 用途 | 出处（谁建的） | 重建策略 |
|---|---|---|---|
| `sandlock-dev:latest` | **fork 的规范开发/门禁镜像**（amd64、Debian trixie、`WORKDIR /workspace`、入口 `/usr/local/bin/docker-entrypoint.sh` 会把测试降到 uid 65534）—— `third_party/sandlock/scripts/test-all.sh` 注释里的 canonical 形态，`python/verify-wheel.sh` 也在它里面跑 | **配方不在本仓、也不在 fork 树里**（上游/维护者侧；本仓唯一的 Dockerfile 是 `python/wheel-builder/Dockerfile`） | **不要重建、不要删**：`cross-platform-lanes.md` §3 的门禁命令直接用它，本仓无法还原它 |
| `sandlock-dev-f17:latest` | E2B 侧"dev 形态"：`deploy/scripts/fork-gate.sh` 的默认 `IMAGE`、pitfall A3 的 `cargo check` 用它 | **就是 `deploy/docker/Dockerfile.test-runner` 的产物**（entrypoint `/usr/local/bin/entrypoint.test-runner.sh`、`CMD pytest tests/unit tests/contract`；2026-09-09 建） | 需要时重建：`IMAGE=sandlock-dev-f17:latest ./deploy/scripts/build-test-image.sh`（同一 recipe；它不会自己降权，所以 `fork-gate.sh` 用 `setpriv` 补这一步） |
| `sandlock-dev-e7:latest` | 2026-09-02 的 manylinux 版 dev 镜像（`manylinux-entrypoint`、`dnf`、`/opt/python/cp314`、rustup@rsproxy、iproute） | 早期手搓，配方不在本仓 | 历史，**不要再指向它**：`fork-gate.sh` 与各文档都已改用 `-f17`；缺了不影响任何 lane |
| `sandlock-zig-builder:local` | **aarch64 真内核车道的工具镜像**（`deploy/scripts/arm-lane/{xbuild,xbuild-debug,lima-vm}.sh`） | **fork 的 `python/wheel-builder/Dockerfile` 的 `build` 阶段**（`--target build`，确切命令见本节上一段） | 缺了才建（几分钟，吃 buildx 缓存）；**wheel 构建不需要它** |
| `quay.io/pypa/manylinux_2_34_x86_64:latest` / `..._aarch64:latest` | wheel 构建的 BASE_IMAGE（宿主架构决定用哪个） | `third_party/sandlock/python/build-wheels.sh` 首次构建时自动 pull | 不用手动建；只是缓存 |
| `e2b-sandlock-test:latest` | E2B 的**测试镜像**（烤进 `wheels/fork/*.whl`）：`test-prod-shaped.sh`、`tests/security` 的生产形态，`deploy/scripts/build-test-image.sh` 是它的 rebuild 入口 | `deploy/docker/Dockerfile.test-runner` | **只有 `wheels/fork/` 变了才重建** —— 不重建就是 B7（31 个 `TypeError: … unexpected keyword argument`）；它本地-only、不推 ACR |
| `sandlock-e2b-{worker-1..3,control-plane,gateway}:latest` | 4 周前本地单机示例栈（`deploy/compose/docker-compose.yml` 的 `build:`）留下的 tag | compose build | 仓库里**已无任何引用**，属残留（可删） |

**A9. arm64 真内核车道的入口：怎么起 VM、怎么跑、`tmp/` 被清之后先做什么。**
（2026-09-30 立此条，与 A7/A8 同类：不是"新踩的坑"，是**口径卡** —— 这套东西散在 5 个文件里，
没有这张卡就只能从头再推一遍。完整方案、lane 边界与基线数字在 `docs/cross-platform-lanes.md`
§2/§5。）

**为什么非要这条 lane**：QEMU 用户态会把 `ptrace`/`regset` 变成 ENOSYS，S0 那批结论在它下面不成立；
syscall 号、结构体布局、字长相关的行为只有真 aarch64 内核才验得了（代价：TCG 比真机慢约 100×，
所以**性能数字不要用这条 lane 取**）。

**现役 VM（2026-09-30 实测）**：`sandlock-arm` —— `qemu` / `aarch64` / 6 vCPU / 8 GiB / 30 GiB，
guest 是 Ubuntu 24.04.5 LTS、内核 `6.14.0-37-generic`，磁盘在 `~/.lima/sandlock-arm`。
定义 = `deploy/scripts/arm-lane/vm.yaml`（**tracked 副本**，文件头写着"改仓路径要同步改"），
驱动 = `deploy/scripts/arm-lane/lima-vm.sh`（`start|stop|shell|sync|run`）。

```bash
limactl list | grep sandlock-arm      # 现役状态（实测当天是 Stopped）
limactl start sandlock-arm            # 起：qemu TCG，第一次要几分钟
limactl stop  sandlock-arm            # 收工停回去
```

**每次改动后的循环**（= `docs/cross-platform-lanes.md` §5.2 那一套）：

```bash
deploy/scripts/arm-lane/xbuild.sh cargo test --no-run --target aarch64-unknown-linux-gnu \
    -p sandlock-ffi -p sandlock-supervise      # 用 sandlock-zig-builder:local 交叉编译（A8）
deploy/scripts/arm-lane/lima-vm.sh sync        # 源码走 tar-over-ssh、产物走 limactl copy（9p 只当窗口）
limactl shell sandlock-arm -- bash -s < deploy/scripts/arm-lane/phase-run.sh   # guest 里跑相位
deploy/scripts/arm-lane/e2b-sync.sh            # 只有改了 E2B 侧才需要
```

**`tmp/` 被清掉之后逐条对**（2026-09-30 清过一次）：

- `tmp/arm-lane/target`（宿主侧交叉产物）**不用手动建**：`xbuild.sh` 的 `-v` 会自动建目录，跑一次上面
  第一条就有；工具镜像 `sandlock-zig-builder:local` 是 A8 的保留项，所以代价只是编译时间；
- `tmp/arm-vm/noble-arm64.img` **只有 `limactl create` 才需要**（`vm.yaml` 里那行 `file://…`）：
  现役 VM 已存在 ⇒ `limactl start` 不受影响（2026-09-30 实测起过一次）；真要重建就按 §5.1 重新下载
  noble arm64 cloud image；
- `third_party/sandlock/tmp/cargo-home`（`--offline` 的 crate 缓存）只在跑 fork 门禁时用得上，
  恢复命令在 A7。

**从零重建 VM（只在 VM 丢了才做）**：`docs/cross-platform-lanes.md` §5.1 有完整三步（下载 cloud image →
`limactl create --name sandlock-arm deploy/scripts/arm-lane/vm.yaml` → `limactl start`）。
注意 `vm.yaml` 里两处**绝对路径**（基础镜像、`/lima-repo` 挂载点）是指向本 checkout 的。

**这条 lane 的两条硬约束**（细节见 `docs/cross-platform-lanes.md` §3，别自己改）：
`/src` 必须是 **bind mount、不能是符号链接**（否则注册控制套接字撞 `SUN_LEN`）；
目标根必须是 `/var/tmp/aarch64-target`、**不能是 `/tmp`**（`CARGO_TARGET_TMPDIR` 被烘焙进测试二进制，
而沙箱策略给 `/tmp` 开了 `fs_write` —— 夹具会落进写授权，五个 landlock named-unix 门禁变假绿）。

---

## B. 跑测试 / lane

> 这一节是**症状 → 真因**的速查。两套 lane（x86_64 容器 + aarch64 真内核）的完整方案、
> 脚本清单、VM 定义、重建步骤与基线数字在 **`docs/cross-platform-lanes.md`**；本节只留
> 踩过的坑，两边互为索引。

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

**B5. 自己拼 `docker run --privileged … pytest tests/unit` 会造出一堆"既有失败"。**
2026-09-21 实测：这样跑出 88 个失败，其中一大半是 `SECCOMP_FILTER_MISSING`
（`envd_service/config.py` 的 `E2B_REQUIRE_SECCOMP_FILTER` 自检：`/proc/self/status` 是
`Seccomp: 0` 就 fail closed，因为 worker 正是跑不可信负载的那个进程）。**正确做法是别自己拼**：
`UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh`（它自带 capset + 线上 seccomp 档 +
`E2B_HOST_PROJECT` + `E2B_TEST_STRICT_SKIPS=1`）。缺 capset 还会多出一条启动披露告警，把
`test_xfs_project_quota_agent` / `test_quota_agent_client` 那类"逐条比对 caplog"的断言整批打红。

**B6. base image 在 lane 里解析不到 ⇒ 建箱回 428，然后一串测试红。**
`python-mcp:3.14` 是我们本地构建的镜像，公共 mirror 链（daocloud 等）**不在白名单**（403）。
做法：`E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 ./deploy/scripts/test-prod-shaped.sh`
（本机 `sandlock-local-registry` 预置了全集镜像；脚本开头也会打这条提示）。少这一条时
`test_control_plane_network_local` / `test_migration_volume_quota` 全是 `assert 428 == 201`。

**B7. 测试镜像比 fork wheel 旧 ⇒ 一批 `TypeError: … unexpected keyword argument`。**
`e2b-sandlock-test:latest` 不会自动跟着 `wheels/fork/*.whl` 重建。2026-09-21 实测：镜像是 09-16 的、
wheel 是 09-20 的（含 N25 的 `max_file_size`），`tests/unit` 因此红 31 个
（`Sandbox.__init__() got an unexpected keyword argument 'max_file_size'`）。改完 fork 要重建：
`docker build -f deploy/docker/Dockerfile.test-runner -t e2b-sandlock-test:latest .`

**B8. 两个测试文件共用同一段 uid 池 ⇒ 后一个文件里只看得见 `exit 127`。**
route-B 每个 uid 只租**一个活槽位**（"W1 recycles a uid only by restarting its process,
never by sharing it"）；若两个文件用同一段，第二个文件建箱时槽位还被前一个文件的沙箱占着，
命令回 127、**原因只在 stderr**（`route-B uid N already has a live slot`）。
做法：每个测试文件用自己的 uid 段（`test_shared_volume_relative_cwd` 现在用 22000），
且控制面与 worker 必须配**同一段**——OBS-9 之后 uid 由控制面分配、worker 只做范围校验，
只配 worker 会得到 `500 uid 10000 is outside this worker's pool`。

**B9. 平台状态搬家后，测试里写死的路径会过期。**
`sandbox.json` 已从沙箱树内搬到 `<base>/_runtime/<id>/`（§12 的平台/workspace 分离）。
`test_agent_uid_lifecycle_and_orphan_reconcile` 原先读 `<workspace>/sandbox.json`，现在读不到
（`FileNotFoundError`）——用 `gateway_common.paths.sandbox_record_path(base, id)`，
读者仍保留 legacy 回落。同类还有 `perSandboxQuotaMb` 的期望值：N28/C（`78285fa`）之后
volume mount 会**原样透传**这个数（`single_file_ceiling_bytes` 靠它知道该挂载点可以放大文件），
测试里再写 `0` 就是把"无限制"当成默认了。

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

**B10. lane 里建箱 503 `this image is not in the allowlist`。**
原因：没设 `E2B_REGISTRY_MIRRORS`，于是基础镜像走公共镜像源链，而本地构建的 `python-mcp:3.14`
不在任何公共源的白名单里（2026-09-16 实测：单条 contract 直接 503）。
做法：带本地预载 registry（§2.6.1）：
`E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 ./deploy/scripts/test-prod-shaped.sh …`
（脚本自己会大声警告，别把警告当噪音跳过。）

**B11. "箱内 `id -u` = 0"不能单独当形态证据。**
原因：pid_ns 关着时这条同样成立（route-B 自映射本来就把客人做成 root），所以拿它当 pid_ns 的
验收时会得到假绿。做法：配对一条只有该形态才成立的观测 —— 例如 `kill(<宿主 pid>, 0)`：
自有 pid ns 里是 `ESRCH`，共享宿主 pid ns 里是 `EPERM`（探针 `deploy/scripts/acceptance/pidns-shape-probe.py`）。

**B12. fork 门禁里 chroot 家族集体红，报 `execvp 'rootfs-helper': Exec format error`（或 exit 127），
而 `tests/rootfs-helper` 变成 0 字节。**
原因：夹具根是 `CARGO_TARGET_TMPDIR`，也就是 fork 仓的 `target-linux/tmp` —— 它**挂在 `/src` 里、
跨容器共享、跨运行存活**，而失败或挂住的用例走不到 `cleanup_rootfs`。残留 rootfs 里的
`usr/bin/rootfs-helper` 与共享的 `tests/rootfs-helper` **是同一个 inode**（硬链接）；下一次
`build_test_rootfs` 的 `hard_link` 因目标已存在而失败，回退 `fs::copy`，它的目标正是那个同 inode 的
路径 ⇒ 打开写入把**源**截断成 0 字节 ⇒ 此后所有 chroot 类用例拿到 ENOEXEC。容器里 pid 复用是常态
（每个容器里测试二进制都是同一个低 pid），所以"换个容器再跑"救不了这个坑。
做法：`test_instance_chroot.rs` 早就修过（`temp_dir` 加单调 seq + 先 `remove_dir_all`），
`test_chroot.rs` 缺这份修复 —— 2026-09-22 补齐；同时 `build.rs::build_static` 改成**先编译到同级
临时文件再 rename 发布**，这样即使 `cc` 原地写也只换目录项，已存在的硬链接仍指向旧的那份完整 inode
（证据：`tmp/k0s/core_integ-after-fix.log`、`tmp/k0s/trace-magicfd.log`）。
排查提示：怀疑 helper 被清零时先看 `ls -l third_party/sandlock/tests/rootfs-helper` 的**大小**——
0 字节就是这条，不是产品回归。

**B13. `E2B_REAL_ROOT=1` 在 aarch64 上永远打不开，报的却是 seccomp 档的问题。**
现象（2026-09-24，arm lane 实测）：`E2B_REAL_ROOT=1` 跑 `tests/security`，12 个用例全红在建箱前 ——
`RuntimeError: E2B_REAL_ROOT is on, but this worker cannot build a sandbox root: pivot_root (the
profile must admit it): No such process`，指向"去应用 `deploy/seccomp/sandlock-worker.json`"。
原因：`_REAL_ROOT_PROBE` 里的探针把 `pivot_root(2)` 的号**写死成 x86_64 的 155**
（`libc.syscall(155, ...)`）。aarch64 用的是 generic syscall 表，`pivot_root` 是 **41**，而 155 在
那里是 `sched_getattr` —— 传 `b"."` 当 pid 就回 `ESRCH`（"No such process"），于是**探针从来没问过
内核 pivot_root**，却把别人的 errno 当成了"profile 没放行"。影响面正好是生产架构：线上是 aarch64，
这个开关在那之前**不可能被打开**，而错误信息会把人送去查 seccomp。
实测（同一台 guest）：`syscall(155)` → ESRCH，`syscall(41)` → EINVAL（这才是真 `pivot_root` 对
"路径不是挂载点"的回答）。
做法：按架构派发（x86_64 = 155，generic 表的 aarch64/riscv64/loongarch64 = 41），**未知架构
fail closed** 并点出架构名（错的号会伪装成 seccomp 问题，见上）；注意别把这条规则误用到
`xfs_quotactl.py` 的 `_SYS_QUOTACTL_FD = 443` —— 那个号在两张表里一致，写一个是对的。
推广：**任何 Python 里的 `__NR_*` 常量都要先确认它在目标架构上是不是同一个号**，跨架构的 lane
（§7 的 arm64 lane）是唯一能抓到这类 bug 的地方，x86_64 容器门禁全绿也说明不了问题。

**B14. fork 门禁在本机跑不起来（三条独立原因叠在一起），以及"基线数字"那条纪律。**
现象（2026-09-25）：照 fork 文档跑 `sh scripts/test-all.sh` 被门禁自己拒绝 ——
*"default and --wheels modes are NON-ROOT suites: they must run as uid 65534"*；按它给的
`setpriv --reuid 65534 … sh scripts/test-all.sh` 改，又变成 `cargo: Permission denied`；
再把权限放开，就停在 `core_lib: baseline says 902 passed, run produced 904`。
三条原因（都不是 fork 的问题）：
1. **本机没有 fork 的规范镜像**。文档里的 `sandlock-dev:latest` 的 entrypoint 会在 root prep 之后
   降到 nobody；本机装的是 **E2B 的测试镜像 `sandlock-dev-f17`**，entrypoint 是
   `entrypoint.test-runner.sh`（Cmd 是 pytest），prep 完 `exec "$@"` **仍然是 root**。
2. **工具链在 65534 够不到的地方**：`cargo` 在 `/root/.cargo/bin`，而 `/root` 是 `0700`
   （规范镜像里是 `/opt/cargo`，所以那边没这个问题）。
3. **这个镜像的 prep 不含 fork 的 net fixture 需要的东西**：`/etc/hosts` 的
   `198.18.0.0/15` 映射与 lo 上的 198.18.0.x 地址。缺了它，core_integ 有 **5 条**红在
   *"no free 198.18.0.x address … run the test container entrypoint (root prep) so the
   unprivileged fixtures exist"*（与 arm64 lane 当年那 42 条同因，见 `cross-platform-lanes.md`）。
做法：**`deploy/scripts/fork-gate.sh`** —— 它在容器里以 root 先跑 lane 的那份 prep
（`deploy/scripts/arm-lane/guest-prep.sh`，一份定义两处调用）、放开工具链可读/可执行、把 `tmp/` 与
`HOME` 指到可写处，再用 `setpriv` 降到 65534 跑门禁，最后把各相位的结果打出来。
两个顺手修掉的 prep 坑（都记在 `guest-prep.sh` 的注释里）：
* **别在已经有 `/usr/local/bin/python3` 的镜像上建那条软链**：本镜像里
  `/usr/bin/python3 -> /usr/local/bin/python3`，照 lane 那句 `ln -sfn /usr/bin/python3
  /usr/local/bin/python3` 会**造出环**，表现为两条毫不相关的 suite 报
  `Too many levels of symbolic links`。改成"只在 `/usr/local/bin/python3` 不可执行时才建"。
* 本镜像**没有 `sysctl`**，prep 改成回落到直接写 `/proc/sys/net/ipv4/ip_unprivileged_port_start`。
**这条纪律也在这里**：门禁把每档的通过数跟 `third_party/sandlock/docs/test-baseline.md` 比对，
**加了测试就必须同步那份基线**，否则你会看到 `baseline says X passed, run produced Y` ——
那不是回归，是提醒登记（这轮 core_lib 902→904、core_integ 551→552 就是这么来的）。
复跑单个家族：**`deploy/scripts/fork-gate.sh --one 'test_chroot::'`**（在同一套 prep/降权环境里
只跑该过滤集；这是 FUP-09 那条纪律的工具，不是「重试门禁」）。

**一条使用警告**：这台机器上**整档 core_integ 的非 root 跑不稳定** —— 同一套环境跑五次，
通过/失败是 547/5、551/1、549/3（基线、暂存了改动）、552/0、537/15，**每次红的用例集合都不同**
（`popen` / `control` / `freeze` 这类进程呈现与线程通知的用例居多，也见过 `test_chroot::` 整族 15 条
一起红）。而**每一族单跑都是绿的**（实测 `test_chroot::` 单跑 49/0）。

> **（2026-10-05 更新）B14 第 1 条已过时：本机现在有 `sandlock-dev:latest`**（1.46 GB，amd64，
> cargo 1.98 / Python 3.11.16），而且 fork 树里的 `target` 是指向 `/src/target-linux` 的软链
> （目录本身 777），所以规范跑法直接可用。四条相位都跑过了，**十一格全绿**：core_lib 922 /
> core_integ 569 / ffi 104 / cli 98 / supervise 57 / supervise_cost 3 / cli_build 0 /
> python 465 / oci 157 / supervise_root 4 / mediation_2uid 9；逐条记录与三个环境坑
> （root 相位需要先在线预热 cargo 缓存、`cli` 的 57 条在 `cli_test` 失败后不会被跑到、
> python 会静默加载过期的 `libsandlock_ffi.so`）都在 `third_party/sandlock/docs/test-baseline.md`
> 的 2026-10-05 注记里，发版侧的记录在 `docs/deploy-clusters.md` §7.40。
>
> **上面那条"整档 core_integ 不稳定"的警告在 2026-10-05 又见了一次，但形态更糟**：不是红，是
> **死等** —— 测试进程卡在 `futex_wait`、`restore-stub` 子进程在 `nanosleep`，日志 35 分钟不动，
> 而门禁当时**没有任何超时**。现在有三层兜底：`scripts/test-all.sh` 每档一个硬预算
> （`timeout`，`SANLOCK_SUITE_TIMEOUT_S` 可覆盖，超时报 `suite TIMED OUT`）、
> `test_instance_exec.rs`/`test_restore.rs` 的 restore await 走 `bounded()`（60 s，点名是哪一步）、
> python 每个用例一个 `faulthandler` 看门狗（dump 落到门禁会打印的文件里）。
> 顺带修掉一个一直存在的采集 bug：脚本在 `set -e` 下，套件失败时子 shell 会在写 rc 之前退出，
> 于是**所有**失败（包括超时）都被记成笼统的 `1`/FAILED。

**B15. checkpoint 落盘踩的两个"形态"坑（2026-09-25，都是同一件事的两个面）。**
两者都是**只在集群上才会出现**的：本机的 fork 套件要么用静态 helper、要么跑在 root 上。

1. **写图的进程是沙箱自己的 uid，不是 worker 的。** route-B 下 slot 以沙箱的池 uid 跑
   （实测 `host_uid=10001`，slot 进程 `uid=10001`，worker 是 root）。所以图所在的目录必须能
   被那个 uid 写：我们第一版按设计写成"worker 0700"（`_runtime/<id>/checkpoint`），结果是
   引擎**捕获成功、保存失败** `checkpoint save failed: … Permission denied`。
   现在图放在 `<base>/_runtime/.checkpoints/<id>`（store `0711`、每个 `<id>` 归该沙箱 `0700`），
   与 runtime 目录**并列**而不是嵌在里面 —— `_runtime/<id>` 本身是 worker 的 `0700`，
   嵌在里面的话 slot 连穿过去都做不到（除非把 record 所在的目录开出来，而那会让沙箱能读
   record 里的 access token）。
2. **park 也算一个活子进程。** 会话的 M0 是 park（`while :; do kill -STOP $$; done`），
   所以"跑过东西的沙箱"恒有 2 个活子进程，引擎的 `checkpoint()`（正确地）拒绝；
   部署侧用 `exclude_main` 说明"我的 M0 是 park"（fork `da0faf5`）。

**B16. fork 的构建产物不止 `libsandlock_ffi.so` 与 `sandlock-supervise`：还有 restore-stub。**
`restore-stub.c` 由 `build.rs` 编译，`stub_path()` 用的是 **build 容器里**的
`target/…/out/restore-stub`，而 wheel 只装 `.so` 与 supervise ⇒ 装到 worker 上的 wheel
**没有 stub**，每次 resume 都被引擎按名拒绝：`restore-stub was not built …`（集群实测
2026-09-25）。修法（fork `2d5f2e9`）：wheel builder 分 arch 暂存 stub、`build-wheels.sh` 把它
注入成 `sandlock/bin/restore-stub`（与 supervise 同样的 0755 + RECORD 重写 + manifest 行）、
`sandlock/__init__.py` 把 `SANDLOCK_RESTORE_STUB` 指过去、`stub_path()` 依次认
「环境变量 → 构建路径 → 可执行文件旁边」。**本机套件抓不到这个**：本机跑的就是构建树，
`RESTORE_STUB_PATH` 存在。

**B17. 写 fork 的"会话"用例时：一个活着的会话子进程会把 libtest 的输出捕获管道留着，
整个测试二进制在退出时挂住 —— 看起来完全像引擎卡死。**
现象（2026-09-25，写 park 形态用例时踩到）：用例主体已经跑完（甚至写了"完成"标记），
但 `cargo test` 永不返回；容器里 `/proc` 看得到测试进程、slot 与 parked 的 M0，测试主线程停在
`futex`，另有一个线程停在 `anon_pipe_read`（libtest 的捕获读线程）。原因：会话子进程
**继承了测试进程的 stdout/stderr**（捕获管道），而 park 既 ignore `TERM` 又处于 `SIGSTOP`，
所以 `shutdown()` 之外还得显式 `kill_child(0, SIGKILL)`；否则管道永不 EOF、libtest 等不到。
排查手法：**别信"没有输出"** —— 用 `--nocapture` 跑（捕获关掉，失败信息就直接出来了），
或者往**文件**里写标记（`std::fs::OpenOptions::append` + `sync_all`），后者在两种模式下都可靠。
本机这个镜像里容器 pid 1 是 cargo、**不 reap 孤儿**，所以源会话的子进程会留在 `Z` 状态，
不要把它当成"泄漏"。

**B18. 写 fork 的"会话"用例时第二个夹具陷阱：`#[tokio::test]` 默认单线程 runtime，
会饿死沙箱的监督循环。**
现象（2026-09-25）：用例里"捕获后往同一个会话 exec 一条命令、再读它的 stdout"约 1/3 概率
卡死（测试主线程停在 `futex`），而同一形状单跑有时又全绿——看着像引擎间歇性 bug。
真因：`#[tokio::test]` 默认是**单线程** runtime，而 `read_exact_bytes` 是**阻塞**读；
那一次读占住唯一的线程后，沙箱的通知循环（同一 runtime 里的 task）再也跑不了，
**被 exec 的孩子的第一次 `write` 得不到应答**，读在等一段永远产不出来的输出。
`sandlock-supervise` 跑的是多线程 runtime，所以夹具也必须：
`#[tokio::test(flavor = "multi_thread", worker_threads = 2)]`。改完那条断言连跑 5 次全绿。
同一个坑还有第二个面：**捕获这类会阻塞线程的调用**同理——单线程 runtime 下派发循环在捕获
期间根本没机会跑，"冻结窗口里挂起的 fork 通知"那类 bug 就永远复现不出来（我们为此写了
`test_a_capture_does_not_wedge_a_forking_sibling`，它必须多线程 + 长窗口才稳定红）。
所以它只能当「看方向」，不能当判定；要判定请用规范镜像或 lane，或者按 FUP-09 的纪律
「留住第一次红的日志 + 单跑那一族」。

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

**C8. "镜像源配了却还是超时"要先看**客户端**能不能写它的 docker config。**
症状：`Template.build` 报 `dial tcp 74.86.228.110:443: i/o timeout`，而 buildkit 日志里躺着两条
`msg="trying next host" error="mkdir /nonexistent: permission denied"` —— 两条正好是两个 mirror，
**它们在发出请求之前就死了**，构建只是回落到 origin 再超时。现场读起来像"没配镜像源"，其实镜子
一次都没轮到。
根因：uid 65534 在那个镜像里是 `nobody`、家目录 `/nonexistent`；buildkit 的 auth provider 在
**客户端一侧**（`buildctl`）按 `$DOCKER_CONFIG` → `$HOME/.docker` 找目录，并且**每个 registry host
都会 `MkdirAll` 一次**。
做法：给 `buildctl` 显式 `DOCKER_CONFIG`（见 `control_plane/api/templates.py::_docker_config_dir()`）；
排查顺序先"客户端能不能建那个目录"，再看 mirror 连通性。细节与实测见 `docs/deploy-clusters.md` §7.13。

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
去程 / 服务端自身 / 回程一目了然（`deploy/scripts/acceptance/mcp-3way.py` 的思路）。这次就是靠它把 390 ms 归到传输层。

**D6. 探针与清理**：脚本放项目 `tmp/`；跑完 kill 掉所有沙箱、删掉目标机上的探针文件
（`tmp/cleanup*.sh` 有模板），别给下一轮留冷缓存陷阱和残留箱。

**D7. "节点失联"要分两侧量：先看控制面访问日志里任意行的间隔，再看心跳行的间隔。**
`heartbeat_gaps.py` 报的是**心跳行**的空档，它能证明节点被判失联，但不能说明是**谁**没说话：
worker 没发，还是控制面没处理。N32（2026-09-22）就是这么分开的 —— 同一次拷贝期间控制面
访问日志**整段**空白 76.1 s（连 kubelet 的 `HEAD /` 都没有），所以堵的是**控制面**自己的
事件循环，不是网络也不是 worker。做法：`kubectl logs <cp> --timestamps` 抓成文件，解析
时间戳排序后打印相邻行的间隔 > 5 s 的位置。
**根因形状**：`async def` 里出现同步的 `httpx.post`，或对共享 NAS 上的树做 `shutil.rmtree` /
同步删除调用 —— 都是"在循环上做几秒到几十秒的 I/O"。修法一律 `await asyncio.to_thread(...)`；
另外心跳的**判据侧**要有"自己落后就别下判决"的守卫，否则控制面自己的停顿会被读成节点死亡
（判决是破坏性的：活沙箱被当孤儿，之后全是 409）。

---

## E. 工作习惯

**E1. `rg -r` 是"替换"不是"递归"。** `rg -rn "pat"` 会把匹配替换成 `n` 打印，看起来像输出被吞字；
要行号用 `rg -n`。

**E2. `exec_command` 里 `nohup … &` 会随会话结束被杀。** 长任务前台跑，或写成脚本 + `setsid`；
别指望后台任务能在下一次工具调用里继续。

**E3. 长任务输出重定向到 `tmp/*.log` 再 `rg` 关键行**，只 `tail` 会漏掉中段的失败细节
（这次踩过：失败详情在中段，`tail` 只看到结尾的 warnings）。
