#!/bin/sh
# pure + 合成根下的 pause/resume：restore stub 从"根内"变成"根外"，正是
# docs/chroot-workspace-exec.md §11.6.1 那条 fd 路线要覆盖的新情形。
# 用法：probe-pure-restore-synthroot.sh <log>
#
# Task 12 的镜像纪律（简报头部"lane 镜像"那条）：Task 9/10 都查到共享
# `:latest` 里是旧一代 native 引擎，所以 lane 必须显式指镜像、且先核同源。
# 本轮 `wheels/fork` 已重建到 fork tip `290761e`（checkpoint 回复带上 exe/argv
# ⇒ `sandlock/bin/sandlock-supervise` 换了字节），故默认指本轮重烤的
# `task12cur`（逐文件 sha 与 `wheels/fork` 相等，见 tmp/k0s/task12/image-source-check.txt）。
# 共享 `:latest` 一字未动。
set -eu
cd "$(cd "$(dirname "$0")/../.." && pwd)"
E2B_TEST_IMAGE="${E2B_TEST_IMAGE:-e2b-sandlock-test:task12cur}"
export E2B_TEST_IMAGE
log="$1"
: > "$log"
printf '# E2B_TEST_IMAGE=%s\n' "$E2B_TEST_IMAGE" >> "$log"
for real_root in 0 1; do
    printf '===== E2B_REAL_ROOT=%s =====\n' "$real_root" >> "$log"
    sh deploy/scripts/acceptance/gateB-pure-rootfs.sh "$real_root" \
        "tmp/k0s/pure-rootfs-restore-$real_root.log" \
        tests/contract/test_pause_resume_sandlock.py >> "$log" 2>&1 || true
    tail -1 "tmp/k0s/pure-rootfs-restore-$real_root.log" >> "$log"
done
