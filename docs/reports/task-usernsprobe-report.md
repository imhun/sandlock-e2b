# Task userns 机制探针报告 —— 无设备/无 CAP_SYS_ADMIN 下映射到非自身 uid

**状态**：探针完成（本机三档矩阵 + 目标机决定性三档）。**未改产品代码/清单/线上栈**，
目标机用一次性 `--rm` 容器，跑完已核对无残留。

**一行结论**：**在"当前部署 BND（`0xc3`，含 SETUID）+ 发行版 `newuidmap` + 配好 subuid"这一档下，
把子 userns 映射成 `0 → 100000` 是成立的**（`newuidmap`/`newgidmap` rc=0，宿主视图
`/proc/<pid>/uid_map` = `0 100000 1`）；把 BND 清空后同一个 helper **连 exec 都被拒**
（rc=126 `Operation not permitted`）。⇒ **不需要 `CAP_SYS_ADMIN`**，需要的是 BND 里有 SETUID。

**证据日志**：`tmp/usernsprobe-local.log`（OrbStack，amd64，kernel 7.0.14）、
`tmp/usernsprobe-target.log`（目标机 aarch64，kernel 6.12.0-211）。首行 `ENV-HEADER`、末行 `EXIT=`。

---

## 1. 矩阵结论表（全部实测，每档条件）

固定：`--user 65534:65534`、`--security-opt seccomp=unconfined`、`NoNewPrivs=0`，
helper 走**发行版** `newuidmap/newgidmap`（无自研 broker），`/etc/subuid` 对调用者配
`nobody:65534:100000:1000`。

### 目标机（决定性，aarch64 / kernel 6.12 / Rocky 10 helper = `0755` + `cap_setuid=ep`）

| BND 档 | BND 值 | `newuidmap` | `newgidmap` | 宿主视图 `uid_map` | 结论 |
|---|---|---|---|---|---|
| **(a) 部署形态** `--cap-drop ALL --cap-add SETUID,SETGID,CHOWN,DAC_OVERRIDE` | `00000000000000c3` | **rc=0** | **rc=0** | **`0 100000 1`** | ✅ **成立** |
| (b) `--cap-drop ALL` | `0000000000000000` | rc=126 `/usr/bin/newuidmap: Operation not permitted` | rc=126 同 | 空 | ❌ helper **exec 就被拒**（file caps ⊄ BND） |
| (c) Docker 默认 BND | `00000000a80425fb` | rc=0 | rc=0 | `0 100000 1` | ✅ 成立（默认集本就含 SETUID/SETGID） |

对照项（同容器、同档）：负对照 `unshare -U sh -c 'echo 0 10001 1 > /proc/self/uid_map'`
→ `rc=1 write error: Operation not permitted`；正对照 `unshare -U -r id` → `rc=0 uid=0(root)`
（零 cap 自映射路径成立）。helper 身份：`-rwxr-xr-x root root`（**非 4755**）+ `getcap`
`cap_setuid=ep` / `cap_setgid=ep`；其挂载 `ro,relatime,attr2,inode64,…`（无 `nosuid`）。

### 本机（OrbStack，amd64 / kernel 7.0.14-orbstack / Debian helper = `4755` root）

| BND 档 | BND 值 | helper exec | `newuidmap` 结果 | 结论 |
|---|---|---|---|---|
| 部署形态（0xc3） | `00000000000000c3` | 成功（4755 生效） | rc=1 `write to uid_map failed: Operation not permitted` | ❌ **本机环境特有**的失败 |
| `--cap-drop ALL` | `0` | 成功 | rc=1 `open of uid_map failed: Permission denied` | ❌ |
| Docker 默认 | `00000000a80425fb` | 成功 | rc=1 `write to uid_map failed: Operation not permitted` | ❌ |

> 本机三档都失败、而目标机 (a)/(c) 都成功 ⇒ 本机 OrbStack VM（kernel 7.0.14-orbstack）
> 对 uid_map 写入有自己的限制，**不能代表目标内核**；用户要求的"决定性那一档"以目标机为准。
> （另：本机把 subuid 写成 `sandbox:`/`worker:` 时 helper 报 `uid range ... not allowed`——
> `newuidmap` 按**调用者用户名**（容器内 65534 = `nobody`）查条目，这是配置坑，不是机制限制。）

## 2. 决定性结论：是否真的需要 `CAP_SYS_ADMIN`？

**不需要。** 目标机 (a) 档（正是线上 worker 的 BND）在**没有 SYS_ADMIN**、`CapEff=0`、
`NoNewPrivs=0` 的条件下，用发行版 helper 成功写出 `0 100000 1`；唯一被移除的东西是 BND 里的
SETUID 时（(b) 档）helper 连 exec 都起不来 ⇒ **"能映射非自身 uid"的充分条件是
BND ⊇ `SETUID`（`newgidmap` 需 `SETGID`）+ helper + subuid**，与 SYS_ADMIN 无关。

**实测 vs 推断**：
* 实测：映射写入成功（rc=0 + 内核 `uid_map` 文本）、BND=∅ 时 exec 失败、helper 身份/属性、
  负/正对照、subuid 名称键控、本机对照。
* 推断（本轮未取到直接证据）：**子进程真正以宿主 uid X 运行**这一步。我的探针里子进程是"先
  fork 再等父进程写 map"的形态，map 写完后它的宿主 uid 65534 **不在** map 内（map 只含
  `0↔X`），因此它已无法 `setuid(0)`（实测 `setpriv: setresuid failed: Operation not permitted`）。
  正常启动器（如 `unshare --map-users` 或 F1 的 slot broker）是**在 map 写好之后再 exec** 并以
  ns-uid 0 落地 ⇒ 这一步仍建议在 T2 实施时用真实启动器补证。

## 3. fork 自检实测（用户要求逐字报错）

在 worker 镜像里、同一 BND 下执行 `sandlock-supervise --policy /tmp/policy.json --uid 100000`：

```
--- plain host userns (uid 65534):
  sandlock-supervise: refusing to start: euid 65534 does not match --uid 100000; the launcher must drop privileges before exec (otherwise the sandbox would silently run in the wrong identity class)
--- inside unshare -U -r (ns uid 0 -> host uid 65534):
  uid=0(root) gid=0(root) groups=0(root)
  sandlock-supervise: refusing to start: euid 0 does not match --uid 100000; the launcher must drop privileges before exec (otherwise the sandbox would silently run in the wrong identity class)
```

⇒ **userns 路线下 fork 的 `--uid` 自检必须改成"验证映射"**（例如：接受 `euid==0` 当且仅当
`--uid` 等于该 ns 根 uid 的宿主映射；或传 `--uid` 时同时给出 ns 期望根），否则任何
userns 形态都会被这条自检按名拒绝——这与 F1 探针的结论一致，本轮把**逐字报错**取了回来。

## 4. T2 触发条件是否仍成立

原判断："**xattr 被禁且 setuid 渠道可用 ⇒ 用 userns（`newuidmap` + subuid）**"。

| 项 | 结论 |
|---|---|
| 机制层面 | **仍成立**（且比之前更强）：映射到非自身 uid **不要求** `CAP_SYS_ADMIN`，只要求 BND 含 `SETUID`（`newgidmap` 需 `SETGID`）+ 发行版 helper + subuid 配置 |
| 需要修正的措辞 | 原文的"setuid 渠道可用"应写成"**BND 含 SETUID/SETGID（不要求 euid 0、不要求 CAP_SYS_ADMIN）**"——因为线上形态 worker 自身 `CapEff=0`，能力只从 BND 交给 file-cap/setuid helper |
| T2 前置清单（新增） | ① 镜像内装 `uidmap`（或按 Rocky 那样提供带 file caps 的 helper）；② `/etc/subuid`、`/etc/subgid` 用**调用者用户名**（容器内 `nobody`）配段；③ helper 必须不是 `nosuid` 挂载（实测挂载选项为 `ro,…` 无 `nosuid`）；④ fork 的 `--uid` 自检改成映射校验；⑤ 启动器要在 map 写好后 exec 落地（本轮未直接取证，见 §2） |

## 5. 清理与残留核对（目标机）

* 一次性容器全部 `--rm`；跑完 `docker ps -a | grep -Ei 'userns|rockylinux'` → **无**；
* 临时目录/文件 `/tmp/subuid-probe`、`/tmp/subgid-probe`、`/tmp/userns-shared` 与上传的两个脚本
  (`/opt/sandlock/userns-probe.sh`、`userns-target-final.sh`) **已删除**（日志末尾逐项 `No such file`）；
* 为 glibc 兼容拉入的 `rockylinux/rockylinux:10` **已 `docker rmi`**（日志 `removed the throwaway rocky image`）；
* 现有 compose 栈未触碰（v5 容器状态与本探针无关，探针只起 `--rm` 容器）。

## 6. 疑虑 / 建议

1. **本机 OrbStack 与目标机行为不一致**（本机 uid_map 写入 EPERM）⇒ 任何 userns 相关验证都应在
   目标内核上做；本机结论只能当"负对照"。
2. 目标机 helper 是 **file-cap 形态（0755 + cap_setuid=ep）**，不是任务描述里预期的
   `4755 root:root` + 空 getcap。两者机制等价（都靠能力/权限在 exec 时获得 CAP_SETUID），
   但**发行版差异要写进文档**：Debian 系给 setuid-root，RHEL 系给 file caps。
3. 若 T2 真要落地，建议把"helper + subuid + 映射校验"做成启动自检（与现有 `E2B_PRIV_HELPERS`
   自检同风格），并在镜像里显式安装 `uidmap`；本轮**未**做任何这类改动。
