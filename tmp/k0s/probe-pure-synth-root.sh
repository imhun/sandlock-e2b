#!/bin/sh
# 合成根探针的 runner：**生产 cap 形状**（worker 声明的那五个 cap，无 SYS_ADMIN）
# + 出厂 seccomp 档 + 项目内 scratch。用法：probe-pure-synth-root.sh <part> <log>
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
    -e HOST_ONLY="${HOST_ONLY:-/src}" \
    -e DEV_VARIANT="${DEV_VARIANT:-host-tree}" \
    -v "$(pwd):/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    python tmp/k0s/probe-pure-synth-root-plaindir.py "$part" > "$log" 2>&1
