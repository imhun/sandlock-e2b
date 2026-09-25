#!/usr/bin/env bash
# 在本机跑 fork 的默认门禁（`third_party/sandlock/scripts/test-all.sh`）。
#
# 为什么需要这个脚本：门禁的正规形态是**以 uid 65534 跑**（脚本自己写着
# "the default suites are NON-ROOT suites: they must run as uid 65534"），而本机装的镜像
# `sandlock-dev-f17` 不是 fork 的那张规范镜像（`sandlock-dev:latest`）——它的 entrypoint 是
# E2B 的 test-runner（Cmd 是 pytest），做完 root prep 后 `exec "$@"` **仍然是 root**，
# 而且工具链在 `/root/.cargo`（`/root` 是 0700）。所以直接照文档跑会被门禁拒绝，
# 用 setpriv 又会撞上 `cargo: Permission denied`。
#
# 这个脚本把那三处补齐：放开工具链的读/执行、给 tmp 可写、把 home 指到可写处，
# 再用 setpriv 以 65534 跑门禁。**门禁的全部相位都在一个容器里、跑在共享的 target 缓存上**，
# 所以它和 CI/规范镜像的行为一致（差别只在"谁降权"这一步由脚本做）。
#
# 用法：
#   deploy/scripts/fork-gate.sh                 # 默认门禁（非 root 相位）
#   IMAGE=my-dev:latest deploy/scripts/fork-gate.sh
#   deploy/scripts/fork-gate.sh --oci-root      # 透传给 test-all.sh 的 root 相位
#   deploy/scripts/fork-gate.sh --one 'test_chroot::'   # 在同一环境里单跑一族（复跑 flaky 用）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
IMAGE="${IMAGE:-sandlock-dev-f17:latest}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    cat >&2 <<EOF
缺少镜像 $IMAGE。
规范形态是 fork 的 sandlock-dev:latest（entrypoint 会在 root prep 之后降到 nobody）；
本机装的是 E2B 的测试镜像，所以这个脚本自己完成降权。
EOF
    exit 1
fi

# `--one <filter>`：在同一套 prep/降权环境里只跑 core_integ 的一个过滤集。
# 这不是"重试门禁"，而是 FUP-09 那条纪律的工具：整套跑红了先留住日志，再单跑那一族，
# 把两次结果都写清楚 —— 本机这套环境里整档 core_integ 的非 root 跑不稳定，而单族通常是绿的。
if [ "${1:-}" = "--one" ]; then
    FILTER="${2:?--one needs a test filter, e.g. 'test_chroot::'}"
    # `sh ignored "$FILTER"`: the extra word becomes `$0` so the filter lands in `$1`.
    set +e
    docker run --privileged --rm --entrypoint sh \
        -v "$REPO_ROOT":/src -w /src/third_party/sandlock \
        "$IMAGE" -c '
set -e
sh /src/deploy/scripts/arm-lane/guest-prep.sh >/dev/null
chmod a+rx /root
chmod -R a+rX /root/.cargo /root/.rustup 2>/dev/null || true
chmod -R a+rwX tmp 2>/dev/null || true
mkdir -p tmp/home && chmod a+rwx tmp/home
exec setpriv --reuid 65534 --regid 65534 --clear-groups \
    env CARGO_HOME=/src/third_party/sandlock/tmp/cargo-home \
        HOME=/src/third_party/sandlock/tmp/home \
        PATH=/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin \
    cargo test -p sandlock-core --offline --test integration -- "$1" --test-threads=1
' ignored "$FILTER"
    rc=$?
    set -e
    exit "$rc"
fi

# 门禁的日志落在 fork 的 tmp/（gitignored），与在规范镜像里跑时同一位置。
LOG="$REPO_ROOT/tmp/k0s/fork-gate.log"
mkdir -p "$(dirname "$LOG")"

echo "==> 跑门禁：$IMAGE（as uid 65534）"
set +e
docker run --privileged --rm --entrypoint sh \
    -v "$REPO_ROOT":/src -w /src/third_party/sandlock \
    "$IMAGE" -c '
set -e
# 1. 夹具 prep（root 阶段）。这是 net fixture 的"非特权模式"开关：它读预置的
#    /etc/hosts 映射，而不是自己往 lo 上加 198.18.0.x（那需要 CAP_NET_ADMIN）。
#    没有它，整个网络家族会以 "no free 198.18.0.x address ... run the test
#    container entrypoint (root prep)" 失败 —— 本机实测 core_integ 5 条正是这个。
#    复用 lane 的那份 prep（一份定义，两处调用），它已兼容以 root 运行。
sh /src/deploy/scripts/arm-lane/guest-prep.sh
# 2. 让 uid 65534 能够到工具链（规范镜像里 cargo 在 /opt/cargo，本机在 /root/.cargo）。
chmod a+rx /root
chmod -R a+rX /root/.cargo /root/.rustup 2>/dev/null || true
# 3. 门禁的夹具与日志都写在 fork 的 tmp/ 下，非 root 要能写。
chmod -R a+rwX tmp 2>/dev/null || true
mkdir -p tmp/home && chmod a+rwx tmp/home
# 4. 非 root 相位。HOME 指到可写处（规范镜像里 HOME=/root 是不可写的，脚本自己也会兜底）。
exec setpriv --reuid 65534 --regid 65534 --clear-groups \
    env HOME=/src/third_party/sandlock/tmp/home \
        PATH=/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin \
    sh scripts/test-all.sh "$@"
' sh "$@" >"$LOG" 2>&1
rc=$?
set -e

echo "==> 相位结果（$LOG）"
grep -E '^==>|passed --|baseline says|suite FAILED|no baseline|skipped/ignored' "$LOG" \
    | sed 's/^/    /' || true
if [ "$rc" -ne 0 ]; then
    echo "门禁失败（退出码 $rc）——上面每一行都指名了是哪个相位、差在哪。" >&2
    cat >&2 <<'HINT'

如果失败的是**一整族**用例（比如 test_chroot:: 十几条同时红、报 "No such file or directory"
或 ETXTBSY），先别当成产品回归：本机这套环境里整档 core_integ 的非 root 跑**不稳定**，
而**单跑那一族通常是绿的**（实测：test_chroot:: 单跑 49/0，而整套跑时红了 15 条，且每次红的
用例集合都不同 —— 基线代码也一样）。按 FUP-09 的纪律：**留着第一次红的日志**，再单跑那一族，
把"红"和"绿"两次都写清楚。复跑单族的入口：

    deploy/scripts/fork-gate.sh --one 'test_chroot::'

（`fork-gate.sh` 的完整红日志在 tmp/k0s/fork-gate.log，每个相位的原文在
 third_party/sandlock/tmp/test-all-<相位>.log。）
HINT
    exit "$rc"
fi
echo "==> 门禁通过"
