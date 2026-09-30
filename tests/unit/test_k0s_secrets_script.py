"""O3 Task 1: the k0s `e2b-secrets` Secret gets a script, and the control
plane gets a master key.

Why this is the "zero window" hardening: `deploy/k8s/control-plane.yaml` named
no `E2B_SECRET_MASTER_KEY`, so `SecretRegistry` degraded to "in memory +
plaintext on disk" (it logs exactly one warning and keeps going,
`control_plane/registry/secrets.py`) and wrote the values in clear text under
`<workspace_base>/_secrets/**`. That path is the *shared NAS volume*, which the
worker mounts read-write and whole (`deploy/k8s/worker.yaml`), so a root shell in
any worker pod could read every tenant's secret. Turning the key on is
zero-window because it changes only what *new* writes look like; the two
`secretKeyRef`s stay `optional: true` so applying the manifest before the Secret
carries the key still starts the control plane (in the degraded mode, with the
warning), which makes the order of the two steps irrelevant.

Two halves. The text assertions pin the manifest and the script's contract (the
four keys, the replayable apply, no value on stdout, a refusal to overwrite a
live Secret without `--rotate`) -- same convention as
`tests/unit/test_worker_manifest_permissions.py`. Then the behaviour cases run
the real script against a stub `kubectl` (there is no cluster in the unit lane),
which is what caught the two bugs this file was written too late to see: a
failed `live_keys` looked exactly like "the Secret has no keys" (and apply
overwrites the whole `data` map, so that is a silent key deletion), and
`kubectl apply`'s own `secret/... configured` line was landing on stdout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "deploy" / "k8s-k0s" / "secrets.sh"
README = REPO / "deploy" / "k8s-k0s" / "README.md"
CONTROL_PLANE = REPO / "deploy" / "k8s" / "control-plane.yaml"

#: The keys the manifests read through `secretKeyRef` from `e2b-secrets`. The
#: script creates and backfills these; everything else in the Secret is carried
#: through untouched (Task 3 adds `E2B_INTERNAL_API_KEYS` that way).
MANAGED_KEYS = (
    "E2B_API_KEYS",
    "E2B_INTERNAL_API_KEY",
    "E2B_REDIS_PASSWORD",
    "E2B_SECRET_MASTER_KEY",
    # C3 Task 3: the CP→agent credential (`deploy/k8s/c3-agent.yaml`'s face A
    # reads it, `deploy/k8s/control-plane.yaml` presents it). Managed here with
    # the rest so a fresh cluster gets one -- the agent refuses to start without
    # it, and no worker manifest may ever carry it.
    "E2B_C3_AGENT_TOKEN",
)

#: 行为用例的假 `kubectl`：只认识 `secrets.sh` 真的会发的两种调用（`get`/`create
#: --dry-run=client`/`apply -f -`），状态放在一个 JSON 文件里。测试断言因此可以是
#: **精确**的（整份 stdout 逐字比较、逐键比较），而不是"看起来像"。
_STUB_BODY = r'''
"""Fake kubectl for the secrets.sh contract tests."""
import base64
import json
import os
import re
import sys
from pathlib import Path

state = Path(os.environ["STUB_KUBECTL_STATE"])
argv = sys.argv[1:]


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def current():
    if not state.exists():
        return None
    return json.loads(state.read_text(encoding="utf-8"))


ns = None
args = []
i = 0
while i < len(argv):
    if argv[i] in ("-n", "--namespace"):
        ns = argv[i + 1]
        i += 2
        continue
    args.append(argv[i])
    i += 1

data = current()
verb = args[0]

if verb == "get":
    if ns != "sandlock":
        fail("stub kubectl: expected -n sandlock, got %r in %r" % (ns, argv))
    if args[2] != "e2b-secrets":
        fail("stub kubectl: unexpected secret %r" % (args[2],))
    if data is None:
        fail('Error from server (NotFound): secrets "e2b-secrets" not found')
    if "-o" not in args:
        print("e2b-secrets")
        raise SystemExit(0)
    output = args[args.index("-o") + 1]
    if not output.startswith("go-template="):
        fail("stub kubectl: unexpected -o %r" % (output,))
    template = output[len("go-template="):]
    if template == r'{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}':
        for key in data:
            print(key)
        raise SystemExit(0)
    if template == "{{len .data}}":
        print(len(data), end="")
        raise SystemExit(0)
    match = re.fullmatch(r'\{\{index \.data "([^"]+)" \| base64decode\}\}', template)
    if not match:
        fail("stub kubectl: unexpected template %r" % (template,))
    print(data[match.group(1)], end="")
    raise SystemExit(0)

if verb == "create":
    if ns != "sandlock":
        fail("stub kubectl: expected -n sandlock, got %r in %r" % (ns, argv))
    if "--dry-run=client" not in args or args[1:3] != ["secret", "generic"]:
        fail("stub kubectl: create only supports the client dry-run shape")
    literals = {}
    for arg in args:
        if arg.startswith("--from-literal="):
            key, _, value = arg[len("--from-literal="):].partition("=")
            literals[key] = value
    print("apiVersion: v1")
    print("data:")
    for key, value in literals.items():
        print("  %s: %s" % (key, base64.b64encode(value.encode()).decode()))
    print("kind: Secret")
    print("metadata:")
    print("  name: e2b-secrets")
    print("  namespace: sandlock")
    raise SystemExit(0)

if verb == "apply":
    if args[1:] != ["-f", "-"]:
        fail("stub kubectl: apply only supports -f - (got %r)" % (args[1:],))
    text = sys.stdin.read()
    # kubectl resolves the namespace from the object, then the kubeconfig context.
    # This stub has no context, so an object without metadata.namespace really
    # does land outside the namespace the script reads back.
    namespace = re.search(r"^  namespace: (\S+)$", text, re.M)
    target = namespace.group(1) if namespace else "default"
    if "\ndata:" not in text:
        fail("stub kubectl: the manifest carries no data block")
    block = text.split("\ndata:", 1)[1].split("\nkind:", 1)[0]
    payload = {}
    for key, encoded in re.findall(r"^  ([A-Za-z0-9_]+): (\S+)$", block, re.M):
        payload[key] = base64.b64decode(encoded).decode()
    if target == "sandlock":
        state.write_text(json.dumps(payload), encoding="utf-8")
    else:
        print(
            "stub kubectl: applied into namespace %r" % (target,),
            file=sys.stderr,
        )
        (state.parent / "WRONG_NAMESPACE.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    print("secret/e2b-secrets configured")
    raise SystemExit(0)

fail("stub kubectl: unsupported verb %r" % (verb,))
'''


@pytest.fixture()
def stub_cluster():
    """一个只有 `secrets.sh` 能看懂的假集群（仓库内 tmp/，用完即删）。"""
    (REPO / "tmp").mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="o3-secrets-", dir=REPO / "tmp"))
    try:
        stub_dir = root / "bin"
        stub_dir.mkdir()
        kubectl = stub_dir / "kubectl"
        kubectl.write_text(f"#!{sys.executable}\n{_STUB_BODY}", encoding="utf-8")
        kubectl.chmod(0o755)
        yield stub_dir, root / "state.json"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _seed(state_path: Path, payload: dict[str, str]) -> None:
    state_path.write_text(json.dumps(payload), encoding="utf-8")


def _state(state_path: Path) -> dict[str, str]:
    return json.loads(state_path.read_text(encoding="utf-8"))


def _run(stub_cluster, *args: str) -> subprocess.CompletedProcess:
    stub_dir, state_path = stub_cluster
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env["STUB_KUBECTL_STATE"] = str(state_path)
    # 假 kubectl 一旦没被用上，真的那个会立刻失败 —— 这个用例永远不会碰到集群。
    env["KUBECONFIG"] = str(stub_dir / "no-such-kubeconfig")
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _fingerprint_table(values: dict[str, str]) -> str:
    lines = [
        "# sandlock/e2b-secrets 指纹（sha256 前 16 位；值不出机器；未轮换的键指纹逐字不变）"
    ]
    for key, value in values.items():
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        lines.append(f"{key} sha256:{digest} len:{len(value)}")
    return "\n".join(lines) + "\n"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_script_generates_every_key_the_manifests_read():
    text = _script_text()
    for key in MANAGED_KEYS:
        assert f"{key}" in text


def test_script_is_idempotent_and_never_prints_secrets():
    text = _script_text()
    assert "--dry-run=client -o yaml | kubectl apply -f -" in text
    assert "sha256" in text  # only fingerprints leave the machine
    assert "set -x" not in text  # trace prints every variable it expands


def test_script_never_echoes_a_secret_variable():
    for line in _script_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        for key in MANAGED_KEYS:
            if f"${key}" in line:
                assert "echo" not in line, line


def test_script_refuses_to_overwrite_a_live_secret_without_a_flag():
    text = _script_text()
    assert "--rotate" in text
    assert "kubectl -n" in text and "get secret e2b-secrets" in text


def test_control_plane_reads_the_master_key_from_the_same_optional_secret():
    docs = [
        doc
        for doc in yaml.safe_load_all(CONTROL_PLANE.read_text(encoding="utf-8"))
        if doc
    ]
    deployment = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "control-plane"
    )
    containers = deployment["spec"]["template"]["spec"]["containers"]
    env = {
        entry["name"]: entry
        for entry in next(c for c in containers if c["name"] == "control-plane")["env"]
    }
    for key in ("E2B_SECRET_MASTER_KEY", "E2B_SECRET_MASTER_KEYS"):
        ref = env[key]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "e2b-secrets"
        assert ref["key"] == key
        # Transitional on purpose: apply the manifest first, run the script
        # second, and the control plane still starts either way.
        assert ref["optional"] is True


def test_the_readme_deployment_order_runs_the_script_before_apply():
    text = _readme_text()
    section = text[text.index("## 部署顺序") :]
    assert "deploy/k8s-k0s/secrets.sh" in section
    assert section.index("deploy/k8s-k0s/secrets.sh") < section.index(
        "deploy/k8s-k0s/apply.sh"
    )


def test_the_accepted_redis_rotation_window_is_written_down():
    # 2026-09-26 decision: no ACL double-user, the 10-30 s interruption is
    # accepted -- so the window has to be visible in the artifact that performs
    # the rotation and in the deploy order the operator reads.
    for text in (_script_text(), _readme_text()):
        assert "10–30 s" in text
    assert "E2B_REDIS_PASSWORD" in _script_text()


# ---------------------------------------------------------------------------
# 行为：拿假 kubectl 真跑一遍脚本
# ---------------------------------------------------------------------------


def test_a_fresh_run_creates_the_four_keys_and_prints_only_fingerprints(stub_cluster):
    _, state_path = stub_cluster
    result = _run(stub_cluster)

    assert result.returncode == 0
    stored = _state(state_path)
    assert list(stored) == list(MANAGED_KEYS)
    for value in stored.values():
        assert re.fullmatch(r"[0-9a-f]{64}", value)
    assert result.stdout == _fingerprint_table(stored)
    # 断言的是"值不出现在输出里"——这必须按子串找，否则没法证明它没漏。
    for value in stored.values():
        assert value not in result.stdout
        assert value not in result.stderr


def test_a_second_run_keeps_every_value_and_reports_the_same_fingerprints(stub_cluster):
    _, state_path = stub_cluster
    assert _run(stub_cluster).returncode == 0
    before = _state(state_path)

    second = _run(stub_cluster)

    assert second.returncode == 0
    assert _state(state_path) == before
    assert second.stdout == _fingerprint_table(before)
    assert "保留 E2B_API_KEYS（已有值；要换值请显式 --rotate E2B_API_KEYS）" in (
        second.stderr.splitlines()
    )


def test_an_existing_extra_key_survives_and_only_the_missing_one_is_added(stub_cluster):
    _, state_path = stub_cluster
    # `E2B_INTERNAL_API_KEYS` 是轮换窗口用的键（Task 3 的双窗脚本**点名**才会写它）——
    # 没点名时它必须原样带过去，所以它是"apply 不许把别人的键抹掉"的探针。
    # `E2B_API_KEYS` 已存在 ⇒ 保留。
    _seed(
        state_path,
        {"E2B_INTERNAL_API_KEYS": "old-key-a,old-key-b", "E2B_API_KEYS": "keep-me"},
    )

    result = _run(stub_cluster)

    assert result.returncode == 0
    stored = _state(state_path)
    assert stored["E2B_INTERNAL_API_KEYS"] == "old-key-a,old-key-b"
    assert stored["E2B_API_KEYS"] == "keep-me"
    for key in ("E2B_INTERNAL_API_KEY", "E2B_REDIS_PASSWORD", "E2B_SECRET_MASTER_KEY"):
        assert re.fullmatch(r"[0-9a-f]{64}", stored[key])


def test_rotate_changes_only_the_named_key(stub_cluster):
    _, state_path = stub_cluster
    _seed(state_path, {key: f"{key}-old" for key in MANAGED_KEYS})

    result = _run(stub_cluster, "--rotate", "E2B_INTERNAL_API_KEY")

    assert result.returncode == 0
    stored = _state(state_path)
    for key in MANAGED_KEYS:
        if key == "E2B_INTERNAL_API_KEY":
            assert re.fullmatch(r"[0-9a-f]{64}", stored[key])
        else:
            assert stored[key] == f"{key}-old"


def test_rotate_refuses_the_master_key_and_unknown_keys(stub_cluster):
    _, state_path = stub_cluster
    _seed(state_path, {key: f"{key}-old" for key in MANAGED_KEYS})

    refused_master = _run(stub_cluster, "--rotate", "E2B_SECRET_MASTER_KEY")
    refused_unknown = _run(stub_cluster, "--rotate", "E2B_NOT_A_KEY")
    missing_argument = _run(stub_cluster, "--rotate")

    assert refused_master.returncode != 0
    assert refused_unknown.returncode != 0
    assert missing_argument.returncode != 0
    assert "被拒" in refused_master.stderr
    assert "不认识 E2B_NOT_A_KEY" in refused_unknown.stderr
    assert _state(state_path) == {key: f"{key}-old" for key in MANAGED_KEYS}


def test_rotate_redis_prints_the_accepted_window_and_the_rollout_steps(stub_cluster):
    _, state_path = stub_cluster
    _seed(state_path, {key: f"{key}-old" for key in MANAGED_KEYS})

    result = _run(stub_cluster, "--rotate", "E2B_REDIS_PASSWORD")

    assert result.returncode == 0
    lines = result.stderr.splitlines()
    assert (
        "  不可用，建箱与路由失败 —— **10–30 s 中断**。2026-09-26 用户裁定：**接受**"
        in lines
    )
    assert "        ③ kubectl -n sandlock rollout restart deploy/redis" in lines
    assert (
        "        ④ kubectl -n sandlock rollout restart deploy/control-plane"
        in lines
    )
    assert (
        "           （读 redis 的只有 control-plane —— 它同时托管 autoscaler，这一步把扩缩容循环一并重起）"
        in lines
    )


def test_fingerprint_mode_changes_nothing_and_matches_sha256(stub_cluster):
    _, state_path = stub_cluster
    seeded = {key: f"{key}-value" for key in MANAGED_KEYS}
    _seed(state_path, seeded)
    before = state_path.read_text(encoding="utf-8")

    result = _run(stub_cluster, "--fingerprint")

    assert result.returncode == 0
    assert result.stdout == _fingerprint_table(seeded)
    assert state_path.read_text(encoding="utf-8") == before


def test_fingerprint_mode_refuses_a_missing_secret(stub_cluster):
    result = _run(stub_cluster, "--fingerprint")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "e2b-secrets 不存在" in result.stderr


# ---------------------------------------------------------------------------
# 双窗轮换（O3 Task 3）：E2B_INTERNAL_API_KEYS 与 E2B_API_KEYS
# ---------------------------------------------------------------------------
# rotate 的语义照 deploy/scripts/upgrade.sh:122-168（compose 侧已用过的两窗算法）：
# 旧 key 留在列表里继续可用，新 key 进单值槽（internal）或追加进列表（api），
# 等三个消费者都滚到新 key 之后再由 finalize 把旧 key 摘掉。中间不断服。
#
# 差别只有一处，且是刻意的：upgrade.sh 的 finalize 会把旧 key **明文**打进终端，
# 本脚本只打 sha256(前 16)，并且 finalize 可以**用这个指纹**当地址 —— 操作者本来
# 也只拿得到指纹（值从不离开脚本）。


def _seed_internal_window(
    state_path: Path,
    primary: str = "internal-old",
    keys: str | None = None,
) -> dict[str, str]:
    payload = {
        "E2B_API_KEYS": "api-old",
        "E2B_INTERNAL_API_KEY": primary,
        "E2B_REDIS_PASSWORD": "redis-old",
        "E2B_SECRET_MASTER_KEY": "master-old",
        "E2B_C3_AGENT_TOKEN": "agent-old",
    }
    if keys is not None:
        payload["E2B_INTERNAL_API_KEYS"] = keys
    _seed(state_path, payload)
    return payload


def _fp(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def test_a_plain_run_does_not_invent_a_window_list(stub_cluster):
    """没有点名轮换就绝不写列表：重复 apply 不该把"窗口"打开。"""
    _, state_path = stub_cluster
    before = _seed_internal_window(state_path)

    result = _run(stub_cluster)

    assert result.returncode == 0
    assert _state(state_path) == before


def test_rotate_internal_key_keeps_the_old_key_in_the_window_list(stub_cluster):
    _, state_path = stub_cluster
    _seed_internal_window(state_path)

    result = _run(stub_cluster, "--rotate-internal-key")

    assert result.returncode == 0
    stored = _state(state_path)
    new_primary = stored["E2B_INTERNAL_API_KEY"]
    assert re.fullmatch(r"[0-9a-f]{64}", new_primary)
    # upgrade.sh 的形状：列表 = 旧列表（或旧主 key）+ 新主 key，主 key 另存单值槽。
    # 两个读者都会把单值槽并进列表，所以新旧两把在窗口内都认 —— 这就是不断服的那一步。
    assert stored["E2B_INTERNAL_API_KEYS"] == f"internal-old,{new_primary}"
    assert stored["E2B_API_KEYS"] == "api-old"
    assert stored["E2B_REDIS_PASSWORD"] == "redis-old"
    assert stored["E2B_SECRET_MASTER_KEY"] == "master-old"
    # 硬要求：任何输出里不许出现凭据明文（含被轮换掉的旧 key）。
    assert "internal-old" not in result.stdout + result.stderr
    assert new_primary not in result.stdout + result.stderr
    # 整个 runbook（含逐步命令）逐行固定：窗口的两把 key 只以指纹出现。
    assert result.stdout == _fingerprint_table(stored)
    assert result.stderr.splitlines() == [
        "已轮换 internal key：新主 key 写入 E2B_INTERNAL_API_KEY；旧 key 留在 "
        "E2B_INTERNAL_API_KEYS（窗口内新旧都认，这一步不重启任何 pod）",
        f"  新主 key {_fp(new_primary)}",
        "  窗口列表成员（finalize 用这里的指纹）：",
        f"    E2B_INTERNAL_API_KEYS 成员 {_fp('internal-old')} len:12",
        f"    E2B_INTERNAL_API_KEYS 成员 {_fp(new_primary)} len:64",
        "  滚动顺序（唯一不断服的顺序；第 2 步会杀光全部 running 沙箱 ⇒ 放低峰/窗口）：",
        "    1) kubectl -n sandlock rollout restart deploy/control-plane && "
        "kubectl -n sandlock rollout status deploy/control-plane",
        "    2) kubectl -n sandlock rollout restart statefulset/e2b-worker",
        "       （这一步会杀光全部 running 沙箱 —— 树与卷数据保留，但放低峰/窗口做）",
        "    3) 两处都滚完后：deploy/k8s-k0s/secrets.sh "
        f"--finalize-internal-key-rotation {_fp('internal-old')}",
        "       （autoscaler 自 2026-09-30 起是控制面里的一个任务，随第 1 步一起滚，没有第三次 rollout）",
        "保留 E2B_API_KEYS（已有值；要换值请显式 --rotate E2B_API_KEYS）",
        "保留 E2B_INTERNAL_API_KEY（已有值；要换值请显式 --rotate E2B_INTERNAL_API_KEY）",
        "保留 E2B_REDIS_PASSWORD（已有值；要换值请显式 --rotate E2B_REDIS_PASSWORD）",
        "保留 E2B_SECRET_MASTER_KEY（已有值；要换值请显式 --rotate E2B_SECRET_MASTER_KEY）",
        "保留 E2B_C3_AGENT_TOKEN（已有值；要换值请显式 --rotate E2B_C3_AGENT_TOKEN）",
        "secret/e2b-secrets configured",
    ]


def test_rotate_internal_key_refuses_without_a_current_key_or_a_secret(stub_cluster):
    _, state_path = stub_cluster
    _seed(state_path, {"E2B_API_KEYS": "api-old"})

    no_primary = _run(stub_cluster, "--rotate-internal-key")

    assert no_primary.returncode != 0
    assert no_primary.stderr.splitlines() == [
        "secrets.sh: 无法确定当前 internal key（Secret 缺 E2B_INTERNAL_API_KEY）"
    ]
    assert _state(state_path) == {"E2B_API_KEYS": "api-old"}

    state_path.unlink()
    missing = _run(stub_cluster, "--rotate-internal-key")

    assert missing.returncode != 0
    assert missing.stdout == ""
    assert missing.stderr.splitlines() == [
        "secrets.sh: --rotate-internal-key / --rotate-api-keys / --finalize-* 都需要先有 "
        "sandlock/e2b-secrets：先跑一次不带这些参数的本脚本把它建出来"
    ]


def test_finalize_internal_key_rotation_removes_the_named_key(stub_cluster):
    _, state_path = stub_cluster
    _seed(
        state_path,
        {
            "E2B_API_KEYS": "api-old",
            "E2B_INTERNAL_API_KEY": "internal-new",
            "E2B_INTERNAL_API_KEYS": "internal-old,internal-new",
            "E2B_REDIS_PASSWORD": "redis-old",
            "E2B_SECRET_MASTER_KEY": "master-old",
            "E2B_C3_AGENT_TOKEN": "agent-old",
        },
    )

    # 地址既能是值（upgrade.sh 的形状），也能是 --fingerprint 打出来的 sha256 前 16 位。
    by_fingerprint = _run(
        stub_cluster, "--finalize-internal-key-rotation", _fp("internal-old")
    )

    assert by_fingerprint.returncode == 0
    stored = _state(state_path)
    assert stored["E2B_INTERNAL_API_KEYS"] == "internal-new"
    assert stored["E2B_INTERNAL_API_KEY"] == "internal-new"
    assert stored["E2B_API_KEYS"] == "api-old"
    assert "internal-old" not in by_fingerprint.stdout + by_fingerprint.stderr
    assert by_fingerprint.stdout == _fingerprint_table(stored)
    assert by_fingerprint.stderr.splitlines() == [
        f"已从 E2B_INTERNAL_API_KEYS 移除 {_fp('internal-old')}：该 key 立即失效",
        "保留 E2B_API_KEYS（已有值；要换值请显式 --rotate E2B_API_KEYS）",
        "保留 E2B_INTERNAL_API_KEY（已有值；要换值请显式 --rotate E2B_INTERNAL_API_KEY）",
        "保留 E2B_REDIS_PASSWORD（已有值；要换值请显式 --rotate E2B_REDIS_PASSWORD）",
        "保留 E2B_SECRET_MASTER_KEY（已有值；要换值请显式 --rotate E2B_SECRET_MASTER_KEY）",
        "保留 E2B_C3_AGENT_TOKEN（已有值；要换值请显式 --rotate E2B_C3_AGENT_TOKEN）",
        "secret/e2b-secrets configured",
    ]

    by_value = _run(stub_cluster, "--finalize-internal-key-rotation", "internal-new")
    assert by_value.returncode != 0
    assert by_value.stderr.splitlines() == [
        f"secrets.sh: 不能移除当前主 key（{_fp('internal-new')}）："
        "先 --rotate-internal-key 生成新主 key"
    ]
    assert _state(state_path) == stored


def test_finalize_internal_key_rotation_refuses_an_unknown_key_or_an_empty_list(
    stub_cluster,
):
    _, state_path = stub_cluster
    seeded = _seed_internal_window(
        state_path, primary="internal-new", keys="internal-old"
    )

    unknown = _run(stub_cluster, "--finalize-internal-key-rotation", "not-in-list")

    assert unknown.returncode != 0
    assert unknown.stderr.splitlines() == [
        f"secrets.sh: E2B_INTERNAL_API_KEYS 里没有 {_fp('not-in-list')}："
        "地址既不是列表里的 key，也不是它打印过的 sha256 前 16 位"
    ]
    assert _state(state_path) == seeded

    _seed(
        state_path,
        {
            "E2B_API_KEYS": "api-old",
            "E2B_INTERNAL_API_KEY": "internal-new",
            "E2B_REDIS_PASSWORD": "redis-old",
            "E2B_SECRET_MASTER_KEY": "master-old",
            "E2B_C3_AGENT_TOKEN": "agent-old",
        },
    )
    empty = _run(stub_cluster, "--finalize-internal-key-rotation", "internal-old")

    assert empty.returncode != 0
    assert empty.stderr.splitlines() == [
        "secrets.sh: E2B_INTERNAL_API_KEYS 为空：旧 key 已不在生效列表"
    ]


def test_rotate_api_keys_appends_a_new_key_and_keeps_the_old_one(stub_cluster):
    _, state_path = stub_cluster
    _seed_internal_window(state_path)

    result = _run(stub_cluster, "--rotate-api-keys")

    assert result.returncode == 0
    stored = _state(state_path)
    new_key = stored["E2B_API_KEYS"].removeprefix("api-old,")
    assert re.fullmatch(r"[0-9a-f]{64}", new_key)
    assert stored["E2B_INTERNAL_API_KEY"] == "internal-old"
    assert stored["E2B_REDIS_PASSWORD"] == "redis-old"
    assert "api-old" not in result.stdout + result.stderr
    assert new_key not in result.stdout + result.stderr
    assert result.stdout == _fingerprint_table(stored)
    assert result.stderr.splitlines() == [
        "已轮换 API key：新 key 追加进 E2B_API_KEYS（旧 key 仍有效，"
        "这一步不重启任何 pod）",
        "  窗口列表成员（finalize 用这里的指纹）：",
        f"    E2B_API_KEYS 成员 {_fp('api-old')} len:7",
        f"    E2B_API_KEYS 成员 {_fp(new_key)} len:64",
        "  滚动顺序（只有 control-plane 读外部 API key）：",
        "    1) kubectl -n sandlock rollout restart deploy/control-plane && "
        "kubectl -n sandlock rollout status deploy/control-plane",
        f"    2) 客户端逐个切到新 key（{_fp(new_key)}）并验证",
        "    3) 都切完：deploy/k8s-k0s/secrets.sh "
        "--finalize-api-key-rotation sha256:<要退役那把的指纹>",
        "保留 E2B_API_KEYS（已有值；要换值请显式 --rotate E2B_API_KEYS）",
        "保留 E2B_INTERNAL_API_KEY（已有值；要换值请显式 --rotate E2B_INTERNAL_API_KEY）",
        "保留 E2B_REDIS_PASSWORD（已有值；要换值请显式 --rotate E2B_REDIS_PASSWORD）",
        "保留 E2B_SECRET_MASTER_KEY（已有值；要换值请显式 --rotate E2B_SECRET_MASTER_KEY）",
        "保留 E2B_C3_AGENT_TOKEN（已有值；要换值请显式 --rotate E2B_C3_AGENT_TOKEN）",
        "secret/e2b-secrets configured",
    ]


def test_finalize_api_key_rotation_removes_the_named_key_but_never_the_last_one(
    stub_cluster,
):
    _, state_path = stub_cluster
    _seed(
        state_path,
        {
            "E2B_API_KEYS": "api-old,api-new",
            "E2B_INTERNAL_API_KEY": "internal-old",
            "E2B_REDIS_PASSWORD": "redis-old",
            "E2B_SECRET_MASTER_KEY": "master-old",
            "E2B_C3_AGENT_TOKEN": "agent-old",
        },
    )

    result = _run(stub_cluster, "--finalize-api-key-rotation", "api-old")

    assert result.returncode == 0
    stored = _state(state_path)
    assert stored["E2B_API_KEYS"] == "api-new"
    assert "api-old" not in result.stdout + result.stderr
    assert result.stdout == _fingerprint_table(stored)
    assert result.stderr.splitlines() == [
        f"已从 E2B_API_KEYS 移除 {_fp('api-old')}：该 key 立即失效",
        "保留 E2B_API_KEYS（已有值；要换值请显式 --rotate E2B_API_KEYS）",
        "保留 E2B_INTERNAL_API_KEY（已有值；要换值请显式 --rotate E2B_INTERNAL_API_KEY）",
        "保留 E2B_REDIS_PASSWORD（已有值；要换值请显式 --rotate E2B_REDIS_PASSWORD）",
        "保留 E2B_SECRET_MASTER_KEY（已有值；要换值请显式 --rotate E2B_SECRET_MASTER_KEY）",
        "保留 E2B_C3_AGENT_TOKEN（已有值；要换值请显式 --rotate E2B_C3_AGENT_TOKEN）",
        "secret/e2b-secrets configured",
    ]

    last = _run(stub_cluster, "--finalize-api-key-rotation", "api-new")

    assert last.returncode != 0
    assert last.stderr.splitlines() == [
        "secrets.sh: 不能移除最后一个 API key：所有客户端会被锁在门外"
    ]
    assert _state(state_path)["E2B_API_KEYS"] == "api-new"


def test_fingerprint_mode_and_the_window_flags_are_mutually_exclusive(stub_cluster):
    _, state_path = stub_cluster
    seeded = _seed_internal_window(state_path)

    for args in (
        ("--rotate-internal-key",),
        ("--rotate-api-keys",),
        ("--finalize-internal-key-rotation", "internal-old"),
        ("--finalize-api-key-rotation", "api-old"),
    ):
        result = _run(stub_cluster, "--fingerprint", *args)
        assert result.returncode != 0, args
        assert result.stdout == "", args
        assert result.stderr.splitlines() == [
            "secrets.sh: --fingerprint 只读，不能与 --rotate-internal-key / "
            "--rotate-api-keys / --finalize-* 一起用"
        ], args
    assert _state(state_path) == seeded


def test_the_window_finalize_flags_need_a_non_empty_address(stub_cluster):
    """空地址必须报错，不能被当成"没点名"而静默地做一次幂等 apply。"""
    _, state_path = stub_cluster
    seeded = _seed_internal_window(state_path, primary="internal-new", keys="internal-old")

    for flag in (
        "--finalize-internal-key-rotation",
        "--finalize-api-key-rotation",
    ):
        result = _run(stub_cluster, flag, "")
        assert result.returncode != 0, flag
        assert result.stdout == "", flag
        assert result.stderr.splitlines() == [
            f"secrets.sh: {flag} 需要参数（旧 key 或它的 sha256 前 16 位；不能是空串）"
        ], flag
    assert _state(state_path) == seeded


def test_the_single_slot_rotate_of_a_windowed_key_points_at_the_window_pair(
    stub_cluster,
):
    """`--rotate <KEY>` 仍是单槽换值（旧 key 立刻失效）；对这两个键要指路。"""
    _, state_path = stub_cluster
    _seed_internal_window(state_path)

    mixed = _run(
        stub_cluster,
        "--rotate",
        "E2B_INTERNAL_API_KEY",
        "--rotate-internal-key",
    )

    assert mixed.returncode != 0
    assert mixed.stderr.splitlines() == [
        "secrets.sh: E2B_INTERNAL_API_KEY 不能同时用 --rotate 与 --rotate-internal-key"
    ]
    assert _state(state_path) == {
        "E2B_API_KEYS": "api-old",
        "E2B_INTERNAL_API_KEY": "internal-old",
        "E2B_REDIS_PASSWORD": "redis-old",
        "E2B_SECRET_MASTER_KEY": "master-old",
        "E2B_C3_AGENT_TOKEN": "agent-old",
    }

    warned = _run(stub_cluster, "--rotate", "E2B_API_KEYS")
    assert warned.returncode == 0
    assert (
        "⚠ --rotate E2B_API_KEYS 是单槽换值（旧 key 立刻失效）；"
        "双窗轮换请用 --rotate-api-keys" in warned.stderr.splitlines()
    )
