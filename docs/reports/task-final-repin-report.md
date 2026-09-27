# Task: 终态收口（wheel 重钉 + 子模块指针 + 清场）

状态：COMPLETE（全部步骤完成，广度 lane 全绿）

## 结论（一行）

fork tip `a063daf` 上重建双架构 wheel 并 verify 全绿（163/163 符号双向相等、RECORD 精确、
supervise 三方指纹一致、`--uid` 冒烟通过，manifest HEAD = `a063daf`）；主仓子模块指针与镜像已重钉。

## 关键值

| 项 | 值 |
|---|---|
| 主仓 commit | `b51fd0d`（chore(sandlock): repin submodule to a063daf and wheel in lockstep） |
| fork tip | `a063dafe6835d4cf3cfdd259d4c1b1156f54df30` |
| wheel sha256（x86_64） | `7c17fa1fc9a68f45713a5f92aa525e69e8679c356b5add9585010b1080caf3e1` |
| wheel sha256（aarch64） | `cdd66bbbddbfa98baa7f8c2a0d772b1ba3941dabb61bdc12e239d5e4a9087388` |
| wheel 内 `libsandlock_ffi*.so` sha256（x86_64） | `013bf12fa5d6ded524d41b20e8a6c8bf5302c0d29d5ace2da1c3a9939b56fb34` |
| 镜像 id | `sha256:f28b65e87fb05d467f9af32e5c36e02546743400a19ee760be674f6d48d9557f` |
| 镜像内 `.so` sha256 | `013bf12fa5d6ded524d41b20e8a6c8bf5302c0d29d5ace2da1c3a9939b56fb34`（== wheel 内那份） |
| 旧镜像留底 | `e2b-sandlock-test:pre-final` → `92ffe9d9d93f` |
| supervise sha256（x86_64） | `a2e469ff1853944bf1a370e213a1fd02e0ebbad53e0885e82175401959712d7c`（wheel=manifest=standalone） |
| supervise sha256（aarch64） | `19451a41859c667edc75bc9d6b612ad16db5fa06c27e7be0150d2158dcbbdf59`（wheel=manifest=standalone） |

## 交付

1. **wheel 重建 + verify**：`sh python/build-wheels.sh`（fork，CONTEXT_DIR=`tmp/wheel-context2`）
   → `python/verify-wheel.sh` 在 `sandlock-dev` 容器内以 uid 65534 跑，全绿：
   - release lib 与 wheel 动态符号集双向相等：163 == 163（x86_64 与 aarch64 各一次）；
   - wheel 内 `sandlock/bin/sandlock-supervise` 存在、RECORD 恰一行且 sha256/长度精确匹配；
   - supervise 三方指纹一致（wheel 内 / 独立 `supervise/<arch>/` / manifest）；
   - `--uid` 拒绝冒烟：euid 65534 + `--uid 0` 被拒（exit 1），stderr 同时点名两个 uid；
   - manifest HEAD = `a063dafe6835d4cf3cfdd259d4c1b1156f54df30` == fork HEAD。
   产物已同步主仓 `wheels/fork/`：两份 wheel + `SHA256SUMS.supervise` + `supervise/{x86_64,aarch64}`（覆盖）。
   证据：`tmp/sdd/final-repin-wheel-build2.log`、`tmp/sdd/final-repin-wheel-verify.log`。

2. **镜像重建**：`docker build -f deploy/docker/Dockerfile.test-runner -t e2b-sandlock-test:latest .`
   （旧镜像先 `docker tag e2b-sandlock-test:latest e2b-sandlock-test:pre-final` 留底）。
   镜像内 `libsandlock_ffi.cpython-314-x86_64-linux-gnu.so` sha256 与新 wheel 内那份逐字节相等
   （两者都 `013bf12f…`）。证据：`tmp/sdd/final-repin-image-build.log`。

3. **子模块 bump**：主仓 commit `b51fd0d`，`third_party/sandlock` 4b4012b → a063daf。
   只提交了子模块指针；并行代理的在制品（README.md、deploy/scripts/*、tests/* 等）保持未暂存。

4. **清场**：`git worktree remove tmp/a4-baseline-wt` 完成；`git worktree list` 仅剩主工作树
   （`/Users/polus/project/ai/sandlock-e2b`，main）。

5. **广度关键 lane**（冻结树）：`PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh`
   —— **1130 passed / 0 failed / 0 error / 3 skipped**（312.68s），达标。

## Lane 计数

- 目标：0 failed / 0 error（上轮基线 1124 passed / 3 skipped）。
- 实测：**1130 passed, 3 skipped, 0 failed, 0 error in 312.68s**（`EXIT=0`）。
  比上轮基线多 6 passed，来自并行代理在本轮工作区内新增的用例（如
  `tests/unit/test_buildkit_mirrors.py` 等镜像源相关测试）；本 lane 跑的是工作树当前代码
  （含该并行在制品），0 failed/0 error 与上轮一致，无新增红。
- 3 skipped 均为既有形态门控（pure-shape / unprivileged worker / non-root uid pool）。

## 疑虑 / 判因记录

- **首轮 verify 红的成因（已判因，非产品缺陷）**：第一次我复用了主仓既存的
  `tmp/wheel-context/`（15:14 的旧 staging，源码含 B3 已删的 `mediation_run_as`）作为 verify 的
  release-lib 构建源，导致“旧源码 release lib（164 符号） vs 新 wheel（163 符号）”的假红；
  同时首版 staging 只拷了 wheel 根目录文件、漏了 `supervise/` 子目录，触发 standalone 指纹 MISSING。
  两者都是我这一侧的取证/staging 问题。改用**全新** CONTEXT_DIR 重跑后 verify 全绿。
- **`tmp/wheel-context/` 旧副本残留**：`sh python/build-wheels.sh` 内的 `cp -R crates` 不会覆盖已存在
  实体的 mtime，导致旧的 `tmp/wheel-context/` 看起来"没刷新"；本次已绕开（新目录）。
  建议给 fork 的 `build-wheels.sh` 加一条 staging 后自检（或在 CI 里每次用新目录），
  避免他人复用陈旧 context 再踩同一个坑。遗留目录 `tmp/wheel-context/`（旧）与 `tmp/wheel-context2/`（本次）
  均 git-ignored，可在确认后清理。
- 新 wheel 的 aarch64 字节与本日早先 staging 产物一致（确定性重建），非缓存误用：
  两个构建的 fork 源码都是 a063daf（a063daf 与 4b4012b 的 FFI 源码均无 `mediation_run_as`，
  该符号在 4b4012b/d96a036/a063daf 已不存在）。

## 证据日志

- `tmp/sdd/final-repin-wheel-build2.log`（构建，首行 ENV-HEADER，末行 `BUILD-EXIT=0`）
- `tmp/sdd/final-repin-wheel-verify.log`（verify，首行 ENV-HEADER，末行 `EXIT=0`）
- `tmp/sdd/final-repin-image-build.log`（镜像构建）
- `tmp/sdd/final-repin-prod-lane.log`（广度 lane，首行 ENV-HEADER、第二行 REPO-STATUS-BEFORE、末行 `EXIT=0`）
