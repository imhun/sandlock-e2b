## Task 1: 面 A 原语（`as_uid`）+ 单测

**Deliverable:** 一个只能写恒等 uid/gid map 的小二进制，越界一律 fail closed 并点名。

- [ ] 写失败用例：`tests/unit/test_priv_helpers.py` 同形，断言 ① 非池内 uid 被拒；
  ② 目标 pid 的 `uid_map` 非空（已写过）被拒；③ 目标未 unshare（`uid_map` 是初始全量）被拒；
  ④ 传入 `0 X 1` 形式的"非恒等"映射被拒（面 A **只**接受恒等）。
- [ ] 跑测试确认**红**（`pytest tests/unit/test_priv_helpers.py -q`）。
- [ ] 实现 `deploy/priv/as_uid.c`：`--uid X --pid N`；写前校验上四条；成功后打印一行
  `C3-ASUID-OK pid=N uid=X`。
- [ ] 跑测试确认**绿**；`getcap` 断言 file caps 恰为 `cap_setuid,cap_setgid+ep`。
- [ ] 建**独立镜像** `deploy/docker/Dockerfile.agent`：装 `as_uid` + `e2b-maint` 到
  `/var/lib/e2b-priv/`（`0710 root:<agent-gid>`），`as_uid` 打 `cap_setuid,cap_setgid+ep`。
  **写两条 pin**：① 该目录里**恰好这两个**特权二进制，caps 与预期**逐字相等**；
  ② **worker 镜像里 `/var/lib/e2b-priv/` 不存在**（这是判据 2 的硬性质，靠独立镜像换来的）。
- [ ] Commit。

