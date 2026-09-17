# 沙箱逃逸深度安全分析与测试 · 整体计划（2026-09-16）

范围：L1 逃逸 / L2 横向 / L3 越权 / L4 可用性（四类全部纳入）。
边界：**P0 发现即修，P1 以上出方案待批**；先在本地生产形态容器验证，
最终以目标环境复验为准。

## 阶段与执行结果

| 阶段 | 内容 | 结果 |
|---|---|---|
| P0 基线 | 用项目自带的 `deploy/scripts/test-prod-shaped.sh` 跑 unit+contract+security（生产形态：部署 seccomp profile + lane 能力集 + 本地 registry） | ✅ `1475 passed / 3 skipped / 0 failed`（[baseline.md](baseline.md)） |
| P1.1 Python 静态审计 | `paths.py` / `priv_helpers.py` / `route_b.py` / `http/*` / `control_plane/api/*` / 网络策略 | ✅ 见 [attack-surface.md](attack-surface.md) |
| P1.2 Rust 面 | seccomp 分发表、Landlock 规则、procfs 虚拟化、网络规则与 connect 代执行 | ✅ 定位 SEC-001 根因（`IpCidr` 不跨地址族匹配 + 代执行在 worker netns） |
| P1.3 配置/部署 | compose / stack / k8s 清单、Dockerfile、caps、seccomp profile、默认值 | ✅ 发现并修复 4 处默认值；记录 OBS-3/4/5 |
| P1.4 工具链扫描 | `bandit` / `semgrep(p/python+p/security-audit)` / `pip-audit` / `cargo-audit` | ⚠️ 前三项完成（findings 已逐条判读）；`cargo-audit` 因 GitHub/RustSec 不可达未完成，`cargo install` 编译中 |
| P2 对抗性逃逸测试 | 内核接口、文件系统、fd 继承、跨沙箱、网络写法矩阵、DoS 四维 | ✅ 新增 `tests/security/escape/`；4 个独立探针脚本在 `tmp/sec-*.py` |
| P3 组合/全链路 | 真实 envd ASGI 应用 + 真实端口 + 沙箱内发起请求 | ✅ 端到端复现 SEC-001（HTTP 200 → 修复后拒绝） |
| P4 修复与回归 | SEC-001 修复 + 回归用例 + 全量回归 | ✅ `1478 passed / 3 skipped / 0 failed` |
| P4b 修复与回归 | OBS-1（拒绝 `sys_chroot`）：fork 源码 + 单元/集成回归 + wheel 重建 + E2B 复验 | ✅ fork lib `849 passed`、chroot 集成 `50 passed`、E2B 矩阵全 EPERM；lane 全量 `1478 passed / 3 skipped / 0 failed`（两 phase） |
| P4c 基础设施 | INFRA-1：非 root worker 验收阶段（lane phase 2）因镜像目录属主而**跑不起来**（非本次引入） | ✅ 一行 `chown` 修复后 phase 2 `51 passed / 1 skipped` |
| P5 清单完整（用户指定） | fork 侧路径面账本 `sys/path_surface.rs`：把"拦截清单完整"变成 CI 不变量（中介清单双向相等 / 名字可解析 / **全部 syscall 必须被分类**） | ✅ fork lib `854 passed`；守卫非空转已实测（故意破坏 ⇒ 3 条用例变红） |
| P5 产出 | 账本立刻抓到一个**活的**宿主信息泄露：`inotify_add_watch` 未中介、按宿主根解析、实测收到宿主文件名（OBS-2） | ✅ **chroot 形态已按"中介"落地**（fork `handle_chroot_inotify_add_watch`，代执行 + `dup_fd_from_pid`）；E2B 侧验收用例 + pure 形态 `xfail(strict=True)` 残留 pin |
| P6 backlog | N14：真根（`mount ns + pivot_root`）形态评估，只出决策文档不动代码 | ✅ 已登记进 `docs/task-backlog.md` |
| P7a 资源创建限流（用户指定） | OBS-8：限流只覆盖沙箱创建与模板构建，**快照创建（复制整个 fs）与卷创建完全无限** | ✅ 统一准入 `enforce_resource_limit`，限流器按端点独立，默认 = 创建预算（120）且有各自 env 覆盖；新增 4 条契约用例 |
| P7b 账本 pure 维度 | OBS-7：账本只按 chroot 形态分类，pure 形态的"谁没被管"要靠人肉推 | ✅ 三张清单恰好划分路径面；算出的 `PURE_UNGATED` **35 条**（不是 1 条），逐个 pin |
| P7c 结论 | pure 形态泄露的是宿主**元数据**（存在性/大小/时间戳/inode/链接目标/xattr 名值）+ 目录事件；chroot 形态不受影响 | ⏳ 修法（① 统一闸门 / ② N14 扩展）待决策，已记 N15 |

## 判定口径

- L1/L2 类：任何一条成立即 P0 阻断项。
- 结论一律以**运行时证据**为准（退出码 / errno / 原始报文 / 日志），
  文档只当线索 —— 本轮实测发现旧文档里 4 项「未解决」其实早已修复。
- 断言用精确匹配（清单全等、结果字典全等），不用 `in`/`contains`。

## 未完成 / 交棒项

1. **目标环境复验**：本地是 OrbStack 容器（Landlock ABI=8）。最终以目标机
   （compose stack）+ k8s 集群为准，两条形态都要复跑
   `tests/security/escape/` 与网络写法矩阵。
2. **`cargo-audit`**：本机 GitHub/RustSec 不可达；需在能访问 RustSec advisory-db
   的环境对 `third_party/sandlock/Cargo.lock`（253 个 crate）跑一次。
3. **OBS-1（已修复）**：`chroot(2)` 是真实的 seccomp fallthrough —— 模拟 chroot 形态下
   子进程内核根就是宿主 `/`（fork 从不 chroot，只让子进程 chdir 到 rootfs 内的宿主路径 +
   中介翻译），因此"宿主独有目录 chroot 成功"被实测证实。已按批准方案**拒绝 `sys_chroot`**：
   `DEFAULT_BLOCKLIST_SYSCALLS` 加 `"chroot"` + fork 单元/集成回归各一条 + wheel 重建 +
   E2B 侧矩阵复验全 EPERM。详见 [findings.md](findings.md) 的 OBS-1。
   **残留**：`chroot_path_syscalls()` 与"带路径 syscall 全集"的系统性差集核对，见同节。
4. **OBS-6（已定性，修法是配置不是代码）**：租户隔离 E3.1 已完整落地并有 contract 用例，
   但 `E2B_TENANTS` 未配置即单租户兼容模式、出厂 env 示例未启用 ⇒ 默认部署下任一 key
   可列/读/删/驱动全部沙箱与卷（两 key 矩阵已实测）。**修法：部署时设
   `E2B_TENANTS` + `E2B_ADMIN_API_KEYS`，存量跑 `migrate-tenants.py`。**
