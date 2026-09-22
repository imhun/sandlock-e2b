# pure（无 chroot）形态：要修、要换真根，还是让它显式选择

**2026-09-22。这是一份决策文档，不含代码改动** —— 它要把 backlog 里四条挂着的东西
合成一个决定：**N15**（pure 形态只有 Landlock 一道网）、**OBS-5**（该形态没有磁盘硬闸门）、
**N27**（平台状态可见性依赖形态）、**N14**（用 mount ns + pivot_root 换掉"虚拟根"）。
它们不是四个问题，是**同一个缺失**的四个面：pure 形态没有 rootfs，也就没有路径中介。

## 1. 今天这个形态是什么、怎么落进去的

不是"可选特性"，是**没有镜像 rootfs 时自动落到**的那条路：

```
mediation_shape = bool(settings.base_image and image_rootfs is not None)
```

（`envd_service/executors/sandlock.py`）。生产清单设了 `E2B_BASE_IMAGE=python-mcp:3.14`
⇒ 走 chroot/image-rootfs，路径中介在；把 base image 留空（或解析不到 rootfs）⇒ **沙箱
直接落在宿主根下，只有 Landlock**。平台自己的 lane 里就有这样一个 gate（
`test_pure_shape_workspace_ownership` 的 skip 理由写着 "requires an empty E2B_BASE_IMAGE"），
所以这不是纸上的形态。

## 2. 缺什么（实测，不是推断）

| 面 | chroot 形态 | pure 形态 |
|---|---|---|
| 路径可见性 | 路径 syscall 全被中介，`chroot_root` 翻译 | **Landlock 一道网**；Landlock 访问位是闭集，"带路径但不在闭集里"的调用无人拦 |
| 具体暴露 | — | 同一宿主文件上实测：`openat` EACCES，而 `getxattr` **读回宿主 xattr**、`open_tree` **返回 fd**、`inotify_add_watch` **投递宿主事件与宿主文件名**（OBS-7）；`path_surface.rs::PURE_UNGATED` 把这一类**逐条 pin 成 33 条**（stat/readlink/chdir/chmod/utimensat/*xattr/inotify_add_watch + 5 条 at 风格，其中 5 条在当前内核 ENOSYS 或被 worker seccomp 档拒） |
| 磁盘闸门 | 中介的**活账本**：`openat` 按剩余额度发上限、超预算建条目 ENOSPC、unlink 即时归还、N31 的条目计数闸门 | 只有 init 在 fork 里施加的 **per-exec `RLIMIT_FSIZE` 硬上限**（单文件、shape 无关，仍生效）；**没有活账本**：跑飞的写者可以一直写到自己那条 exec 的额度，`diskMB` 那层语义在这个形态下不成立（OBS-5） |
| 平台状态可见性 | `_runtime` 对沙箱 ENOENT（沙箱的根是它自己的 rootfs） | `/home/user` 就是 `<base>/<id>` 的真实路径 ⇒ `..` 到 `<base>`，`_runtime` "看得见但打不开"（DAC `0700` → EACCES）。**不是洞，但是形态漂移**（N27） |

## 3. 三条路

**A. 给 33 条补统一闸门（N15 ①）**
给 `NotifPolicy` 加非 chroot 的 readable/writable 集合 + 一个"仅 Landlock 档位"标志，
对"Landlock 覆盖不到的带路径 syscall"注册统一策略判定（`dup_fd_from_pid` 代执行，同
`handle_chroot_inotify_add_watch`）。worklist 现成、被单测 pin 住。
*代价*：33 条里每条都要决定语义（`chmod` 是 DAC 门控的、`readlink` 要落在 root 内……），
而且**新增内核 syscall 会继续进这个桶**（有单测兜，但每加一条就要处理一次）；
它关掉的是"元数据泄漏"，**关不掉 OBS-5 与 N27**（那两件是"没有中介"本身）。

**B. 换真根：mount ns + pivot_root（N14）**
pure 形态也合成一个真根（bind `/usr`、`/lib`、`/bin`、`/opt`、workspace、`minimal_dev` 后 pivot）。
*一次关掉整类*：路径空间变成内核不变量，`chroot_path_syscalls()` 的完整性不再承担安全职责；
`/workspace` 与 `/home/user` 同一宿主目录的别名由 bind mount 天然表达（可删掉 chdir 记录机制）；
exec 的 `PT_INTERP` 补丁 + memfd 那套可删（内核按新根解析解释器）；OBS-5 与 N27 也随之落地
（状态在真根之外 / 绑定视图之内）。
*代价*：core 现在**完全不建 mount ns**（`CLONE_NEWNS` 只出现在"拒绝 clone 命名空间"的过滤器里），
要先做宿主兼容性矩阵；`fs_mount`（workspace/卷/`minimal_dev` 六节点）全在 rootfs 之外 ⇒
必须引入 mount namespace 与整套 bind 装配；这是三条里唯一动**形态语义**的。

**C. 让这个形态变成显式选择（fail closed 默认）**
不修能力，先修"默认"：没有 rootfs 时**拒绝建箱**（给出可读原因），除非运维显式设置
（例如 `E2B_ALLOW_PURE_SHAPE=1`）—— 平台自己的 gate B 改成显式设置它。
这与本仓库既有的做法一致（SL-1 的路径中介"建箱前拒绝且没有降级档"、route-B "强开而前置
不满足 ⇒ 建箱报错，不静默退 route-A"）。
*代价*：几乎没有（一个开关 + 一段文档 + lane 里那一处显式设置）；*收益*：把"默认部署下
一不小心就落到无中介形态"这件事**变成一次显式决定**。它**不修** pure 形态本身的三个缺口。

## 4. 建议

**先 C，把 B 立在"要上 pure 的时候"**：

1. **C 立刻做**：今天的默认是"base image 忘配 / 解析失败 ⇒ 静默降级到无中介形态"，
   这本身就是最该消除的形状（它有单测能证明：把 base image 置空，`mediation_shape` 变 false）。
2. **B 作为"pure 形态要进生产"的前提**：一旦有人在生产上要这个形态，就走 B（一次关掉
   N15/OBS-5/N27 三件），而不是给 33 条打补丁（A）—— A 只解决三件里的一件，而且维护面随内核增长。
3. **A 不作默认路线**，除非出现了"必须保留无 rootfs 形态、又必须堵住元数据泄漏"的具体需求；
   届时按 `PURE_UNGATED` 的 33 条逐个定语义（表里已经有每条的实测与理由）。

## 5. 需要你定的

1. **要不要在生产上支持 pure（无 rootfs）形态？**
   - 不支持 ⇒ 批 **C**（我把默认改成 fail closed + lane 里显式打开），N15/OBS-5/N27 全部转为
     "不适用（该形态不被支持）"，只留触发条件；
   - 支持 ⇒ 批 **B**（我按 N14 的评估清单出实施计划：mount ns 装配 + 宿主兼容矩阵 + exec 简化），
     并保留 C 作为过渡（在 B 落地前默认仍 fail closed）。
2. **如果支持，pure 形态的验收线是哪一条？** 我建议照 chroot 形态的现有契约等强：
   宿主文件的存在性/大小/时间戳/inode/链接目标/xattr/事件都不可见（即 33 条全部有处置），
   且 `diskMB` 那层活账本在这个形态下也有等价物（或明确写"该形态没有磁盘闸门，按 unsupported 记"）。
