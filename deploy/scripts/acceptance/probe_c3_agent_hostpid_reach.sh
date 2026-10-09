#!/bin/sh
# c3-agent `hostPID` 收敛评估的差分探针（2026-10-09）。
#
# 问题：agent 的 `hostPID: true` 到底买到什么、拿走它会断什么、留着它多出多少可达面。
#
# 两条臂：
#   ① 可见性：同一目标容器，watcher 有 / 没有 host pid ns 时能不能看见它的进程；
#   ② 可达面：watcher 以 uid 65534（= worker / 控制面主容器的 uid）在 host pid ns 里，
#      对「宿主 uid 0 目标」「同 uid 65534 目标」「池 uid 10000 目标」各能读到什么。
#
# 结论与取舍见 docs/security-audit/c3-agent-hostpid-2026-10-09.md。
#
# 用法：sh deploy/scripts/acceptance/probe_c3_agent_hostpid_reach.sh
# 对象一律 hpid- 前缀，脚本自己清理；不碰既有容器与镜像。
set -eu

IMG=alpine:3
SEEN=hpid-target-seen       # uid 0
SAME=hpid-target-same       # 65534:65534，与 agent face A / worker / 控制面同 uid
OTHER=hpid-target-other     # 10000:10000，形如沙箱槽位 uid

cleanup() {
  for c in "$SEEN" "$SAME" "$OTHER"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT INT TERM
cleanup

echo "=== 起三个目标容器（各自私有 pid ns）==="
docker run -d --name "$SEEN"  "$IMG" sh -c 'sleep 600' >/dev/null
docker run -d --name "$SAME"  --user 65534:65534 "$IMG" sh -c 'sleep 601' >/dev/null
docker run -d --name "$OTHER" --user 10000:10000 "$IMG" sh -c 'sleep 602' >/dev/null

# 目标用 sleep 601/602 而不是 600，且 watcher 排除自己的 pid($$)：否则 `sh -c <脚本>`
# 自己的 cmdline 里就含匹配串，会读出"目标可见"的假阳性（第一版就撞上了）。
WATCHER='
total=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)
echo "可见 pid 数: $total"
hit_same=no; hit_other=no; hit_root=no
for p in /proc/[0-9]*; do
  [ "$p" = "/proc/$$" ] && continue
  cmd=$(tr "\0" " " < "$p/cmdline" 2>/dev/null || true)
  case "$cmd" in
    *"sleep 600"*) hit_root=yes ;;
    *"sleep 601"*) hit_same=yes ;;
    *"sleep 602"*) hit_other=yes ;;
  esac
done
echo "uid 0 目标可见: $hit_root"
echo "同 uid(65534) 目标可见: $hit_same"
echo "池 uid(10000) 目标可见: $hit_other"
'

echo
echo "=== 臂 ①a：watcher 没有 host pid ns（= hostPID: false）==="
docker run --rm --user 65534:65534 "$IMG" sh -c "$WATCHER"

echo
echo "=== 臂 ①b：watcher 有 host pid ns（= hostPID: true，今天的形态）==="
docker run --rm --pid=host --user 65534:65534 "$IMG" sh -c "$WATCHER"

echo
echo "=== 臂 ②：host pid ns + uid 65534 对每个目标能读到什么 ==="
READER='
echo "--- watcher uid: $(id -u) ---"
show() { # $1=标签 $2=命令
  if err=$(eval "$2" 2>&1 >/dev/null); then printf "    %-10s 可读\n" "$1"
  else printf "    %-10s 拒绝（%s）\n" "$1" "$(printf "%s" "$err" | tr -d "\n" | cut -c1-58)"; fi
}
for p in /proc/[0-9]*; do
  [ "$p" = "/proc/$$" ] && continue
  cmd=$(tr "\0" " " < "$p/cmdline" 2>/dev/null || true)
  case "$cmd" in
    *"sleep 600"*|*"sleep 601"*|*"sleep 602"*) ;;
    *) continue ;;
  esac
  uid=$(awk "/^Uid:/{print \$2}" "$p/status" 2>/dev/null || echo "?")
  printf "pid=%s uid=%s\n" "${p#/proc/}" "$uid"
  show status  "head -c 1 $p/status"
  show cgroup  "head -c 1 $p/cgroup"
  show cmdline "head -c 1 $p/cmdline"
  show environ "head -c 1 $p/environ"
  show maps    "head -c 1 $p/maps"
  # mem 的 offset 0 通常未映射（EIO），所以取 maps 第一段起始地址再读，
  # 这样"权限被拒"与"地址没映射"才分得开。
  addr=$(awk "NR==1{split(\$1,a,\"-\"); print a[1]}" "$p/maps" 2>/dev/null || true)
  if [ -n "$addr" ]; then
    show "mem@首段" "dd if=$p/mem bs=1 skip=\$((0x$addr)) count=1"
  else
    show mem "head -c 1 $p/mem"
  fi
  show "kill -0" "kill -0 ${p#/proc/}"
done
'
docker run --rm --pid=host --user 65534:65534 "$IMG" sh -c "$READER"

echo
echo "=== 清理 ==="
cleanup
