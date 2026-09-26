#!/usr/bin/env bash
#
# O3 Task 2：轮换 `E2B_SECRET_MASTER_KEY` —— **三拍**（rotate → 全副本滚动 → finalize）。
#
# 为什么主 key 不能像别的凭据那样"换个值 + 滚动"：它是**加密其它所有 secret** 的那把
# key（落盘 `<workspace_base>/_secrets/**`、redis 镜像 `e2b:secret:*`）。新 key 只有在
# control-plane 重启、`SecretRegistry.__init__` 的 `_scan_disk()` / `_scan_redis()` 把每条
# 用旧 key 解开的记录**就地重写**成主 key 密文之后，才真正生效。所以：
#
#   第 1 拍 rotate   ：`E2B_SECRET_MASTER_KEY` 换成新值，**被换下来的旧值追加进
#                      `E2B_SECRET_MASTER_KEYS`**（旧密文在窗口里仍可解密），然后滚 CP。
#   第 2 拍 滚动      ：每个副本起来时把旧密文重写成新主 key（rollout restart + status）。
#   第 3 拍 finalize ：**先证明"全副本已滚动"**，再从 `E2B_SECRET_MASTER_KEYS` 摘掉旧 key。
#
# 算法与 `deploy/scripts/upgrade.sh:171-220` 的 `--rotate-secret-master-key` /
# `--finalize-secret-master-key-rotation` **同一套语义**（两条守卫逐句相同），只换介质：
# compose 写 `.env`，这里写 k8s Secret（k0s 侧与 `deploy/k8s-k0s/secrets.sh` 同一族）。
# **没有发明第三种。**
#
# "全副本已滚动"这一拍怎么被证明（finalize 的前置判据，`status` 只读地打同一批）
# --------------------------------------------------------------------------
# 1) **没有旧副本在跑**：读 `kubectl get deploy control-plane -o json`，比
#    `status.observedGeneration == metadata.generation`、`status.updatedReplicas ==
#    status.replicas == spec.replicas == status.availableReplicas`、
#    `status.unavailableReplicas == 0` —— 也就是"没有任何 pod 来自上一版 ReplicaSet"。
# 2) **每个 running 副本进程拿的就是当前主 key**：
#    `kubectl exec <pod> -c control-plane -- printenv E2B_SECRET_MASTER_KEY`（`secretKeyRef`
#    由 kubelet 在**容器创建时**解析，所以这是这个进程此刻真正持有的值），比
#    `sha256(前16)`：全部等于 Secret 当前 `E2B_SECRET_MASTER_KEY` 的指纹，且副本数等于
#    `spec.replicas`。
# 3) **at-rest 上没有还要旧 key 才能解开的记录**：在 CP pod 里（`python3 -`，与
#    `deploy/scripts/cleanup-plaintext-secrets.py` 同一投递方式）用**该 pod 的主 key**扫
#    `<_secrets>/**` 与 `e2b:secret:*`：每一条都必须 `encrypted: true` 且**主 key 单独就能解开**。
#
# 判据 3 单独看**不够** —— 如果副本还没滚，pod 里那把 key 还是旧的，"所有记录都能解开"
# 照样成立，而 Secret 早已指向新 key，下次重启就全解不开了。1+2 是让 3 有意义的前提。
#
# 不打印任何凭据明文
# ------------------
# 与 `secrets.sh` 同一条纪律：只打 `sha256(前16)` 与长度（**不开 shell trace** —— trace 会把
# 每个展开的变量打出来）。finalize 的地址既可以是 key 本身（upgrade.sh 的形状），也可以是
# rotate 打出来的 `sha256:<前16>`（推荐：操作者本来只看得到指纹，值也不必进 shell 历史）。
#
# 用法：
#   KUBECONFIG=... deploy/k8s-k0s/rotate-secret-master.sh rotate
#   KUBECONFIG=... deploy/k8s-k0s/rotate-secret-master.sh status
#   KUBECONFIG=... deploy/k8s-k0s/rotate-secret-master.sh finalize sha256:<旧主 key 的指纹>
#
# 环境变量：NAMESPACE（默认 sandlock）、SECRET_NAME（默认 e2b-secrets）、
# DEPLOYMENT（默认 control-plane）、CONTAINER（默认 control-plane）、
# ROLLOUT_TIMEOUT（默认 300s）。
set -euo pipefail

NAMESPACE="${NAMESPACE:-sandlock}"
SECRET_NAME="${SECRET_NAME:-e2b-secrets}"
DEPLOYMENT="${DEPLOYMENT:-control-plane}"
CONTAINER="${CONTAINER:-control-plane}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"

#: 单值槽（当前主 key）与并存列表（窗口里的旧 key）。读方是
#: `control_plane/config.py::secret_master_key / secret_master_keys` ⇒
#: `SecretRegistry(master_key=…, legacy_master_keys=…)`：列表里的 key 只用来解开旧密文，
#: 解开的记录会被主 key 重新写一遍。
MASTER_KEY=E2B_SECRET_MASTER_KEY
MASTER_KEYS_LIST=E2B_SECRET_MASTER_KEYS

#: 新主 key 的长度：32 hex 字节（64 字符），与 `deploy/scripts/upgrade.sh` 的
#: `openssl rand -hex 32` 同一口径。
RAND_BYTES=32

say() { printf '%s\n' "$*" >&2; }
die() { printf 'rotate-secret-master.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n "2,/^set -euo pipefail\$/p" "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
}

# 指纹：值只从 printf 进管道到 sha256，绝不落到 stdout / 日志 / argv。
fingerprint() {
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$1" | sha256sum | cut -c1-16
    else
        printf '%s' "$1" | shasum -a 256 | cut -c1-16
    fi
}

# 指纹的书写形状（与 secrets.sh 的 --fingerprint 表一致）。
fp_label() { printf 'sha256:%s' "$(fingerprint "$1")"; }

# ---------------------------------------------------------------------------
# 逗号列表（并存窗口）
# ---------------------------------------------------------------------------
list_at() {  # 1 = 逗号列表, 2 = 下标
    local -a parts
    IFS=',' read -r -a parts <<<"$1"
    [ "$2" -lt "${#parts[@]}" ] || return 1
    printf '%s' "${parts[$2]}"
}

list_has() {  # 1 = 逗号列表, 2 = key（精确相等，不做子串匹配）
    local key
    local -a parts
    IFS=',' read -r -a parts <<<"$1"
    for key in "${parts[@]}"; do
        [ "$key" = "$2" ] && return 0
    done
    return 1
}

# 保序去重的合并（upgrade.sh 的 rotate 就是"当前列表 + 被换下来的主 key"这一次合并）。
join_keys() {
    local raw key result=""
    local -a parts
    for raw in "$@"; do
        [ -n "$raw" ] || continue
        IFS=',' read -r -a parts <<<"$raw"
        for key in "${parts[@]}"; do
            [ -n "$key" ] || continue
            case ",$result," in
                *",$key,"*) continue ;;
            esac
            result="${result:+$result,}$key"
        done
    done
    printf '%s' "$result"
}

# 从列表里去掉一个 key（去掉全部同值项；key 本身不进消息）。
drop_key() {  # 1 = 逗号列表, 2 = 要去掉的 key
    local key result=""
    local -a parts
    IFS=',' read -r -a parts <<<"$1"
    for key in "${parts[@]}"; do
        [ -n "$key" ] || continue
        [ "$key" = "$2" ] && continue
        result="${result:+$result,}$key"
    done
    printf '%s' "$result"
}

# finalize 的地址解析：地址可以是列表里的 key 本身（upgrade.sh 的形状），也可以是它打印过的
# `sha256:<前16>`（推荐 —— 操作者只看得到指纹）。成功时打印那个 key（进调用方变量，不进输出）。
address_is() {  # 1 = 地址（值或指纹）, 2 = key 值 ⇒ 地址指的就是这把 key？
    [ "$1" = "$2" ] && return 0
    [ "$1" = "$(fp_label "$2")" ] && return 0
    [ "$1" = "$(fingerprint "$2")" ] && return 0
    return 1
}

resolve_key_address() {  # 1 = 逗号列表, 2 = 地址
    local key cand="$2"
    local -a parts
    if list_has "$1" "$2"; then
        printf '%s' "$2"
        return 0
    fi
    case "$cand" in
        sha256:*) cand="${cand#sha256:}" ;;
    esac
    case "$cand" in
        *[!0-9a-f]*) return 1 ;;
    esac
    [ "${#cand}" -eq 16 ] || return 1
    IFS=',' read -r -a parts <<<"$1"
    for key in "${parts[@]}"; do
        [ -n "$key" ] || continue
        if [ "$(fingerprint "$key")" = "$cand" ]; then
            printf '%s' "$key"
            return 0
        fi
    done
    return 1
}

say_member_fingerprints() {  # 1 = 键名, 2 = 逗号列表
    local i=0 member
    while member="$(list_at "$2" "$i")"; do
        [ -n "$member" ] || { i=$((i + 1)); continue; }
        say "    $1 成员 $(fp_label "$member") len:${#member}"
        i=$((i + 1))
    done
}

# ---------------------------------------------------------------------------
# 读现有的 Secret（与 secrets.sh 同一套：apply 覆盖整份 data，读漏一个键就是删键）
# ---------------------------------------------------------------------------
secret_exists() {
    kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1
}

live_keys() {
    kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
        -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}'
}

live_key_count() {
    kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
        -o go-template='{{len .data}}'
}

live_value() {  # base64 解码在 kubectl 自己的 go-template 里做：值不进 argv，也不落本地文件
    kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
        -o "go-template={{index .data \"$1\" | base64decode}}"
}

out_keys=()
out_values=()

out_index() {  # 1 = key ⇒ 打印它的下标；找不到返回非零
    local i=0
    while [ "$i" -lt "${#out_keys[@]}" ]; do
        if [ "${out_keys[$i]}" = "$1" ]; then
            printf '%s' "$i"
            return 0
        fi
        i=$((i + 1))
    done
    return 1
}

set_out() {  # 1 = key, 2 = value（已有则覆盖，没有则追加）
    local idx
    if idx="$(out_index "$1")"; then
        out_values[$idx]="$2"
    else
        out_keys+=("$1")
        out_values+=("$2")
    fi
}

out_value() {  # 1 = key；不存在时打印空串（调用方自己判空）
    local idx
    if idx="$(out_index "$1")"; then
        printf '%s' "${out_values[$idx]}"
    fi
}

load_live() {
    local key keys_raw count printed=0
    # 命令替换（不是进程替换）：kubectl 失败 ⇒ 赋值失败 ⇒ `set -e` 立刻退出。
    keys_raw="$(live_keys)"
    count="$(live_key_count)"
    if [ -n "$keys_raw" ]; then
        while IFS= read -r key; do
            [ -n "$key" ] || continue
            printed=$((printed + 1))
            set_out "$key" "$(live_value "$key")"
        done <<<"$keys_raw"
    fi
    [ "$printed" = "$count" ] ||
        die "读 $NAMESPACE/$SECRET_NAME 对不上：Secret 里 $count 个键，只读出 $printed 个 —— 拒绝在不完整的视图上写回（apply 覆盖整份 data，漏读等于删键）"
}

print_fingerprint_table() {
    local idx
    printf '%s\n' "# $NAMESPACE/$SECRET_NAME 指纹（sha256 前 16 位；值不出机器；未轮换的键指纹逐字不变）"
    for idx in "${!out_keys[@]}"; do
        printf '%s sha256:%s len:%s\n' \
            "${out_keys[$idx]}" \
            "$(fingerprint "${out_values[$idx]}")" \
            "${#out_values[$idx]}"
    done
}

require_tools() {
    command -v kubectl >/dev/null 2>&1 || die "缺少 kubectl"
    command -v python3 >/dev/null 2>&1 ||
        die "缺少 python3（读 Deployment / Pod 的 -o json 用）"
}

# ---------------------------------------------------------------------------
# 三批判据的读法（见文件头）
# ---------------------------------------------------------------------------
# 判据 1/3：Deployment 的 status 是否描述当前 generation，且副本全是新的。
deploy_status_fields() {
    kubectl -n "$NAMESPACE" get deploy "$DEPLOYMENT" -o json | python3 -c '
import json
import sys

obj = json.load(sys.stdin)
status = obj.get("status") or {}
spec = obj.get("spec") or {}
fields = (
    (obj.get("metadata") or {}).get("generation", ""),
    status.get("observedGeneration", ""),
    spec.get("replicas", ""),
    status.get("replicas", 0),
    status.get("updatedReplicas", 0),
    status.get("availableReplicas", 0),
    status.get("unavailableReplicas", 0),
)
print("\t".join(str(value) for value in fields))
'
}

# 判据 2/3 的输入：running 且**没有被删除标记**的 pod（旧 ReplicaSet 的 pod 在滚动期间会
# 带 deletionTimestamp 停留一小会儿；数它们等于把"正在退场"当成"还在用旧 key"）。
running_deployment_pods() {
    kubectl -n "$NAMESPACE" get pods -l "app=$DEPLOYMENT" -o json | python3 -c '
import json
import sys

obj = json.load(sys.stdin)
for item in obj.get("items") or []:
    metadata = item.get("metadata") or {}
    if metadata.get("deletionTimestamp"):
        continue
    if (item.get("status") or {}).get("phase") != "Running":
        continue
    print(metadata.get("name", ""))
'
}

# 判据 2/3：逐副本读它此刻持有的主 key，与 Secret 的当前主 key 比指纹。
# stdout 只放**给判据 3 用的那个 pod 名**；判据本身打到 stderr。
judge_replicas_use_current_key() {  # 1 = 当前主 key, 2 = spec.replicas
    local primary="$1" expected="$2" pods pod value segment="" count=0 failed=0
    local -a names=()
    if ! pods="$(running_deployment_pods)"; then
        say "判据 2/3（每个 running 副本拿的都是当前主 key）：读不到 deploy/$DEPLOYMENT 的 pod 列表 ⇒ 未通过"
        return 1
    fi
    while IFS= read -r pod; do
        [ -n "$pod" ] || continue
        names+=("$pod")
    done <<<"$pods"
    for pod in "${names[@]}"; do
        count=$((count + 1))
        if ! value="$(kubectl -n "$NAMESPACE" exec "$pod" -c "$CONTAINER" -- printenv "$MASTER_KEY")"; then
            segment="${segment}${segment:+、}$pod 读不到（exec 失败或 env 未设置）✗"
            failed=1
            continue
        fi
        if [ -z "$value" ]; then
            segment="${segment}${segment:+、}$pod 的 $MASTER_KEY 为空 ✗"
            failed=1
            continue
        fi
        if [ "$(fingerprint "$value")" = "$(fingerprint "$primary")" ]; then
            segment="${segment}${segment:+、}$pod $(fp_label "$value") ✓"
        else
            segment="${segment}${segment:+、}$pod $(fp_label "$value") ✗（≠ 主 key）"
            failed=1
        fi
    done
    if [ "$count" = 0 ]; then
        say "判据 2/3（每个 running 副本拿的都是当前主 key）：一个 running 副本都没有（spec.replicas=${expected}）⇒ 未通过（没有副本能证明它在用当前主 key）"
        return 1
    fi
    if [ "$count" != "$expected" ]; then
        say "判据 2/3（每个 running 副本拿的都是当前主 key）：running 副本 $count 个（spec.replicas=${expected}）：${segment}⇒ 未通过（有副本没起来，或旧副本还没被替换）"
        return 1
    fi
    if [ "$failed" != 0 ]; then
        say "判据 2/3（每个 running 副本拿的都是当前主 key）：Secret 主 key $(fp_label "$primary")；running 副本 $count 个（spec.replicas=${expected}）：${segment}⇒ 未通过"
        return 1
    fi
    say "判据 2/3（每个 running 副本拿的都是当前主 key）：Secret 主 key $(fp_label "$primary")；running 副本 $count 个（spec.replicas=${expected}）：${segment}⇒ 通过"
    printf '%s' "${names[0]}"
}

# 判据 3/3：在 CP pod 里扫 at-rest。这段是**在 pod 内**跑的（主 key 与 `_secrets` 都在那里），
# 只读：只打路径与计数，从不打凭据值或明文。crypto 用 registry 自己的 `_fernet`
# （HKDF-SHA256 ⇒ Fernet），不在这里另写一份。
at_rest_snippet() {
    cat <<'PY'
"""finalize 判据 3 的实弹：at-rest 上没有"还要旧 key 才能解开"的记录。"""
import json
import os
import sys
from pathlib import Path

for candidate in (Path("/app"), Path.cwd()):
    if (candidate / "control_plane" / "registry" / "secrets.py").is_file():
        sys.path.insert(0, str(candidate))
        break
else:
    print("找不到 control_plane（镜像里 /app 应该在）—— 没法做密文校验")
    sys.exit(1)

from control_plane.registry.secrets import _fernet

primary = os.environ.get("E2B_SECRET_MASTER_KEY") or ""
if not primary:
    print("本 pod 的 E2B_SECRET_MASTER_KEY 为空：它没拿到主 key，没法做密文校验")
    sys.exit(1)

fernet = _fernet(primary)
problems = []


def classify(payload, where):
    if payload.get("encrypted") is not True:
        return "%s：明文记录（encrypted != true）" % where
    try:
        fernet.decrypt(str(payload.get("value", "")).encode("ascii"))
    except Exception:
        return "%s：需要旧 key 才能解开（主 key 单独解不开）" % where
    return None


base = (
    os.environ.get("E2B_WORKSPACE_BASE")
    or os.environ.get("E2B_SHARED_WORKSPACE_ROOT")
    or "/var/lib/e2b-sandboxes"
)
secrets_dir = Path(base) / "_secrets"

disk = 0
for path in sorted(secrets_dir.rglob("*")):
    if not path.is_file():
        continue
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if not isinstance(payload, dict) or "value" not in payload:
        continue
    disk += 1
    problem = classify(payload, str(path))
    if problem:
        problems.append(problem)

redis_count = 0
url = os.environ.get("E2B_REDIS_URL") or ""
if url:
    from control_plane.registry.redis_backend import create_redis_client

    client = create_redis_client(url)
    for raw_key in client.keys("e2b:secret:*"):
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        raw = client.get(key)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or "value" not in payload:
            continue
        redis_count += 1
        problem = classify(payload, key)
        if problem:
            problems.append(problem)

total = disk + redis_count
for problem in problems:
    print("✗ " + problem)
if problems:
    print(
        "扫到 %d 条 at-rest 记录（磁盘 %d + redis %d）：%d 条有问题（见上）"
        % (total, disk, redis_count, len(problems))
    )
else:
    print(
        "扫到 %d 条 at-rest 记录（磁盘 %d + redis %d）："
        "全部 encrypted:true 且主 key 单独可解" % (total, disk, redis_count)
    )
sys.exit(1 if problems else 0)
PY
}

judge_at_rest_records() {  # 1 = 用哪个 pod 跑
    local pod="$1" report rc=0 line
    if ! report="$(at_rest_snippet | kubectl -n "$NAMESPACE" exec -i "$pod" -c "$CONTAINER" -- python3 -)"; then
        rc=1
    fi
    if [ "$rc" = 0 ]; then
        say "判据 3/3（at-rest 没有还要旧 key 才能解开的记录）：$report ⇒ 通过"
    else
        while IFS= read -r line; do
            [ -n "$line" ] && say "    $line"
        done <<<"$report"
        say "判据 3/3（at-rest 没有还要旧 key 才能解开的记录）⇒ 未通过"
    fi
    return "$rc"
}

# 三批判据一起跑；任一不过 ⇒ 非零（调用方据此拒跑 finalize）。只读。
run_judgement() {  # 1 = 当前主 key
    local primary="$1" failures=0 pod_for_check="" fields
    local generation observed spec_replicas replicas updated available unavailable line

    if fields="$(deploy_status_fields)"; then
        IFS=$'\t' read -r generation observed spec_replicas replicas updated available unavailable <<<"$fields"
        line="判据 1/3（没有旧副本在跑）：deploy/$DEPLOYMENT metadata.generation=$generation status.observedGeneration=$observed spec.replicas=$spec_replicas status.replicas=$replicas status.updatedReplicas=$updated status.availableReplicas=$available status.unavailableReplicas=$unavailable"
        if [ "$observed" = "$generation" ] && [ "$updated" = "$spec_replicas" ] &&
            [ "$replicas" = "$spec_replicas" ] && [ "$available" = "$spec_replicas" ] &&
            [ "$unavailable" = "0" ]; then
            say "$line ⇒ 通过"
        else
            say "$line ⇒ 未通过"
            failures=$((failures + 1))
        fi
    else
        say "判据 1/3（没有旧副本在跑）：读不到 deploy/$DEPLOYMENT 的 status（kubectl get deploy ... -o json 失败）⇒ 未通过"
        spec_replicas=""
        failures=$((failures + 1))
    fi

    if [ -n "$spec_replicas" ] &&
        pod_for_check="$(judge_replicas_use_current_key "$primary" "$spec_replicas")"; then
        :
    else
        [ -n "$spec_replicas" ] ||
            say "判据 2/3（每个 running 副本拿的都是当前主 key）：读不到 spec.replicas ⇒ 未通过"
        failures=$((failures + 1))
        pod_for_check=""
    fi

    if [ -n "$pod_for_check" ]; then
        judge_at_rest_records "$pod_for_check" || failures=$((failures + 1))
    else
        say "判据 3/3（at-rest 没有还要旧 key 才能解开的记录）⇒ 未执行（判据 2 未过：副本还没拿到当前主 key，此刻在 pod 里扫没有意义，不算通过）"
        failures=$((failures + 1))
    fi

    [ "$failures" = "0" ]
}

# ---------------------------------------------------------------------------
# 落盘：k8s 侧唯一可重放的写法（与 secrets.sh 逐字同形），apply 后回读校验
# ---------------------------------------------------------------------------
apply_secret() {
    local idx count live_primary
    local from_literals=()
    for idx in "${!out_keys[@]}"; do
        from_literals+=("--from-literal=${out_keys[$idx]}=${out_values[$idx]}")
    done
    kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
        "${from_literals[@]}" \
        --dry-run=client -o yaml | kubectl apply -f - >&2
    secret_exists ||
        die "apply 之后 $NAMESPACE/$SECRET_NAME 仍不存在 —— 你的 kubectl 可能把 Secret 写进了别的 namespace（老版本 create --dry-run=client 不带 metadata.namespace）；检查 kubectl 版本后重跑"
    count="$(live_key_count)"
    live_primary="$(live_value "$MASTER_KEY")"
    [ "$live_primary" = "$(out_value "$MASTER_KEY")" ] ||
        die "apply 之后回读的 $MASTER_KEY 与刚写入的不一致 —— 拒绝继续（写进了别的 namespace，或被别的进程改过）"
    say "已 apply 并回读 $NAMESPACE/${SECRET_NAME}（$count 个键）"
}

# ---------------------------------------------------------------------------
# 三拍
# ---------------------------------------------------------------------------
# 第 1 拍 + 第 2 拍：旧主 key 进并存列表、新主 key 进单值槽，然后滚 CP。
do_rotate() {
    local current new_primary new_list
    require_tools
    command -v openssl >/dev/null 2>&1 || die "缺少 openssl（生成新 key 用）"
    secret_exists ||
        die "$NAMESPACE/$SECRET_NAME 不存在：先跑 deploy/k8s-k0s/secrets.sh 把它建出来"
    load_live
    current="$(out_value "$MASTER_KEY")"
    [ -n "$current" ] ||
        die "无法确定当前 secret master key（$NAMESPACE/$SECRET_NAME 缺 ${MASTER_KEY}）：没有它 rotate 会把旧记录的窗口切断 —— 先跑 deploy/k8s-k0s/secrets.sh 补上它（O3 Task 1）"
    new_primary="$(openssl rand -hex "$RAND_BYTES")"
    # upgrade.sh 的同一条公式：列表 = 去重(原列表 + 被换下来的主 key)；新主 key 只进单值槽。
    new_list="$(join_keys "$(out_value "$MASTER_KEYS_LIST")" "$current")"
    set_out "$MASTER_KEY" "$new_primary"
    set_out "$MASTER_KEYS_LIST" "$new_list"
    say "rotate：$MASTER_KEY 换主 key：旧 $(fp_label "$current") → 新 $(fp_label "$new_primary")（只打指纹，值不出脚本）"
    say "窗口：$MASTER_KEYS_LIST 现在持有下面这些旧 key（finalize 的地址就是它们的指纹）"
    say_member_fingerprints "$MASTER_KEYS_LIST" "$new_list"
    apply_secret
    say "第 2 拍（全副本滚动）：kubectl -n $NAMESPACE rollout restart deploy/$DEPLOYMENT"
    kubectl -n "$NAMESPACE" rollout restart "deploy/$DEPLOYMENT" >&2 ||
        die "rollout restart 失败 —— Secret 已经写好（窗口开着，旧 key 还在 ${MASTER_KEYS_LIST}，旧密文仍可解密），手动滚：kubectl -n $NAMESPACE rollout restart deploy/$DEPLOYMENT"
    say "第 2 拍（续）：kubectl -n $NAMESPACE rollout status deploy/$DEPLOYMENT --timeout=$ROLLOUT_TIMEOUT"
    kubectl -n "$NAMESPACE" rollout status "deploy/$DEPLOYMENT" --timeout="$ROLLOUT_TIMEOUT" >&2 ||
        die "rollout status 没在 $ROLLOUT_TIMEOUT 内完成 —— 窗口开着（旧 key 还在 ${MASTER_KEYS_LIST}，旧密文仍可解密），先看 kubectl -n $NAMESPACE get pods -l app=$DEPLOYMENT 再决定重试还是回滚；**不要**在此状态下 finalize"
    say "第 3 拍之前先只读地验判据：deploy/k8s-k0s/rotate-secret-master.sh status（不改任何东西）"
    say "判据过了再摘旧 key：deploy/k8s-k0s/rotate-secret-master.sh finalize $(fp_label "$current")"
    say "⚠ rotate 只开了窗口：窗口开着 != 可以摘 —— 先 status 过判据，再 finalize"
    print_fingerprint_table
}

# 只读地把三批判据打出来（operator 在 finalize 之前看的就是它）。
do_status() {
    local primary
    require_tools
    secret_exists ||
        die "$NAMESPACE/$SECRET_NAME 不存在：先跑 deploy/k8s-k0s/secrets.sh 把它建出来"
    load_live
    primary="$(out_value "$MASTER_KEY")"
    [ -n "$primary" ] ||
        die "无法确定当前 secret master key（$NAMESPACE/$SECRET_NAME 缺 ${MASTER_KEY}）：判据 2 要拿它比副本指纹"
    if run_judgement "$primary"; then
        say "判据全过：可以 finalize（摘旧 key 是不可逆点，执行前再确认一次）"
        print_fingerprint_table
        return 0
    fi
    say "判据未全过：finalize 会被拒 —— 见上面逐条（Secret 一个字没动）"
    print_fingerprint_table
    return 1
}

# 第 3 拍：判据全过才从并存列表里摘掉旧 key。
do_finalize() {
    local address="$1" primary existing_list removed
    require_tools
    secret_exists ||
        die "$NAMESPACE/$SECRET_NAME 不存在：先跑 deploy/k8s-k0s/secrets.sh 把它建出来"
    load_live
    primary="$(out_value "$MASTER_KEY")"
    [ -n "$primary" ] ||
        die "无法确定当前 secret master key（$NAMESPACE/$SECRET_NAME 缺 ${MASTER_KEY}）：finalize 的守卫（不能摘当前主 key）需要它"
    existing_list="$(out_value "$MASTER_KEYS_LIST")"
    [ -n "$existing_list" ] || die "$MASTER_KEYS_LIST 为空：旧 key 已不在生效列表"
    # 守卫①排在"列表里找不着"之前，与 upgrade.sh 的次序一致：点名当前主 key 时，要得到的
    # 是"不能移除当前主 key"这句，而不是一句含糊的"列表里没有"。
    if address_is "$address" "$primary"; then
        die "不能移除当前主 key（cannot remove the current master key）：$(fp_label "$primary") 就是 $MASTER_KEY —— 先跑 rotate 生成新主 key（与 deploy/scripts/upgrade.sh 的同一句守卫）"
    fi
    removed="$(resolve_key_address "$existing_list" "$address")" ||
        die "$MASTER_KEYS_LIST 里没有 $(fp_label "$address")：地址既不是列表里的 key，也不是它打印过的 sha256 前 16 位"
    if ! run_judgement "$primary"; then
        die "finalize 被拒：上面有判据未通过 —— 旧 key 一旦摘掉，仍由它加密的记录就永久解不开。让所有副本都滚到主 key（判据 1/2）、确认 at-rest 全是主 key 能单独解开的密文（判据 3）再重试；Secret 一个字没动。"
    fi
    set_out "$MASTER_KEYS_LIST" "$(drop_key "$existing_list" "$removed")"
    apply_secret
    say "finalize：已从 $MASTER_KEYS_LIST 移除 $(fp_label "$removed")（旧 key 立即失效）；$MASTER_KEY 未动（仍是 $(fp_label "$primary")）"
    say "finalize 完成后建议再滚一次 CP（可选）：旧 key 已不参与密文，滚掉它只是让运行时内存里也不再持有 —— kubectl -n $NAMESPACE rollout restart deploy/$DEPLOYMENT"
    print_fingerprint_table
}

case "${1:-}" in
    rotate)
        [ "$#" -eq 1 ] || die "rotate 不接受参数"
        do_rotate
        ;;
    status)
        [ "$#" -eq 1 ] || die "status 不接受参数"
        do_status
        ;;
    finalize)
        [ "$#" -eq 2 ] && [ -n "$2" ] ||
            die "finalize 需要一个地址（旧 key 或它的 sha256 前 16 位；不能是空串）"
        do_finalize "$2"
        ;;
    -h | --help)
        usage
        ;;
    "")
        usage
        exit 2
        ;;
    *)
        say "未知子命令：$1"
        usage
        exit 2
        ;;
esac
