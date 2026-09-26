#!/bin/sh
# Task 12 的第二个探针：真跑"pause→(worker 没了)→restore"这条链，两态各一遍。
#
# 为什么简报 Step 1 那条不够：`tests/contract/test_pause_resume_sandlock.py` 到不了恢复路径
# （`E2B_PAUSE_CHECKPOINT` 默认关 ⇒ pause 不写图；resume 时 session 还在 ⇒ 走 thaw 分支，
# restore verb 一次都不调）。实测两份日志里 `checkpoint` / stub 字样各 0 次，见报告。
# 本探针走 worker 自己的两个入口（`capture_checkpoint_image` = pause 那一半，
# `restore_checkpoint_image` + 新 executor = worker 重启后的 resume 那一半），
# 判据是"图能从合成根外投递进去、恢复后的进程还在计数、session 还能 exec"。
#
# 用法：probe-restore-synthroot.sh
set -eu
cd /Users/polus/project/ai/sandlock-e2b
E2B_TEST_IMAGE="${E2B_TEST_IMAGE:-e2b-sandlock-test:task12cur}"
export E2B_TEST_IMAGE
log="tmp/k0s/task12/restore-two-states.log"
: > "$log"
printf '# E2B_TEST_IMAGE=%s\n' "$E2B_TEST_IMAGE" >> "$log"
for real_root in 0 1; do
    if [ "$real_root" = 1 ]; then
        name=synth-realroot1
    else
        name=identity
    fi
    printf '===== E2B_REAL_ROOT=%s (%s) =====\n' "$real_root" "$name" >> "$log"
    sh tmp/k0s/gateB-pure-rootfs.sh "$real_root" \
        "tmp/k0s/task12/restore-$name.log" \
        tmp/k0s/task12/test_restore_under_synth_root.py >> "$log" 2>&1 || true
    tail -1 "tmp/k0s/task12/restore-$name.log" >> "$log"
    printf -- '--- verdict (%s) ---\n' "$name" >> "$log"
    cat "tmp/k0s/task12/verdict-$name.json" >> "$log" 2>/dev/null || echo '(no verdict file)' >> "$log"
    printf -- '--- e2b log (%s) ---\n' "$name" >> "$log"
    cat "tmp/k0s/task12/verdict-$name.eb-log" >> "$log" 2>/dev/null || true
done
