#!/bin/sh
# 合成根探针的 runner：**生产 cap 形状**（worker 声明的那五个 cap，无 SYS_ADMIN）
# + 出厂 seccomp 档 + 项目内 scratch。用法：probe-pure-synth-root.sh <part> <log>
#
# 退出码就是探针的判定契约（`ok()`/各 part 的 return 值）：
#   0 = 该 part 的判定成立：b2 PASS / tmpfs PASS-NEGATIVE / symlinks PASS /
#       proc、dev 的事实记录
#   1 = 走不下去：某一步 `FAILED errno=…`（形状不允许、绑定失败、pivot 失败），
#       或 b2 里 host-only 在 pivot 之后**仍然可见**（隔离没成立）
#   2 = VACUOUS：只有 b2 会返回 —— 传进来的 HOST_ONLY 在 pivot **之前**就不存在，
#       那句 `hidden` 会白给、没有任何信息量，所以直接判无效而不是报 PASS。
#       默认值 `HOST_ONLY=/workspace/AGENTS.md` 是 lane 镜像里真有的路径，
#       因此**不传 -e HOST_ONLY 也应当 PASS**；拿到 2 只说明你显式传了一个不存在的路径。
set -eu
cd "$(dirname "$0")/../.."
part="$1"
log="$2"
mkdir -p tmp/k0s/scratch
docker run --rm --init --network host \
    --cap-drop ALL \
    --cap-add NET_BIND_SERVICE --cap-add SETUID --cap-add SETGID \
    --cap-add CHOWN --cap-add DAC_OVERRIDE \
    --security-opt seccomp="$(pwd)/deploy/seccomp/sandlock-worker.json" \
    --security-opt apparmor=unconfined \
    -e HOST_ONLY="${HOST_ONLY:-/workspace/AGENTS.md}" \
    -e DEV_VARIANT="${DEV_VARIANT:-host-tree}" \
    -v "$(pwd):/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    python tmp/k0s/probe-pure-synth-root-plaindir.py "$part" > "$log" 2>&1
