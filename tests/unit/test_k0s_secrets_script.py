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
    # `E2B_INTERNAL_API_KEYS` 是轮换窗口用的键（Task 3/4 写它），本脚本不认识它 ——
    # 正因如此它是"apply 不许把别人的键抹掉"的探针。`E2B_API_KEYS` 已存在 ⇒ 保留。
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
        "        ④ kubectl -n sandlock rollout restart deploy/control-plane deploy/autoscaler"
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
