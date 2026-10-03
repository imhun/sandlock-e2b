"""O3 Task 2: rotating the **master** key on k0s (`E2B_SECRET_MASTER_KEY`).

Why this one is different from the other two credentials: the master key is what
encrypts every other secret at rest (``<workspace_base>/_secrets/**`` and the
redis mirror ``e2b:secret:*``). Removing an old master key while some record
still needs it is not "one client gets a 401" -- it is that record becoming
unreadable **forever** (``control_plane/registry/secrets.py`` only re-encrypts a
legacy record when a replica *loads* it, and the retired key's value is gone
from the Secret). So the middle beat of the three (rotate -> every replica
rolled -> finalize) has to be *provable*, not asserted.

The judgement `finalize` runs before it touches anything (and `status` prints
read-only) is three reads, each exact:

1. ``kubectl get deploy control-plane -o json``: ``status.observedGeneration ==
   metadata.generation``, ``status.updatedReplicas == status.replicas ==
   spec.replicas == status.availableReplicas`` and ``status.unavailableReplicas
   == 0`` -- i.e. no pod is left over from the previous ReplicaSet.
2. every running pod's **live** ``E2B_SECRET_MASTER_KEY`` (``kubectl exec <pod>
   -c control-plane -- printenv E2B_SECRET_MASTER_KEY``; a ``secretKeyRef`` is
   resolved by the kubelet when the container is created, so this is what the
   process is actually holding) fingerprints equal to the fingerprint of the
   Secret's current ``E2B_SECRET_MASTER_KEY``, and the pod count equals
   ``spec.replicas``.
3. inside a control-plane pod (``python3 -``, the shape
   ``deploy/scripts/cleanup-plaintext-secrets.py`` already ships in): every
   at-rest record -- ``<workspace_base>/_secrets/**`` and every ``e2b:secret:*``
   -- is ``encrypted: true`` **and decrypts with the primary key alone**.

Criterion 3 on its own is not enough, and that is the trap: if the replicas have
not rolled yet, the key inside the pod is still the *old* one, so "every record
decrypts" is trivially true while the Secret already points somewhere else --
finalize would then strand the data on the next restart. Criteria 1+2 are what
make 3 meaningful.

Text assertions pin the script's contract (the same three beats as
``deploy/scripts/upgrade.sh:171-220``, the same guard sentences, fingerprints
only). The behaviour cases then run the real script against a stub ``kubectl``
whose ``exec`` verb really executes the shipped in-pod snippet with real Fernet
cryptography against a real ``_secrets`` tree (plus a fake ``redis`` module),
which is what makes "a record that still needs the old key" a non-circular test
rather than a grep.
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

from control_plane.registry.secrets import SecretRegistry, _fernet

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "deploy" / "k8s-k0s" / "rotate-secret-master.sh"
DOC = REPO / "docs" / "k8s-deployment.md"
UPGRADE = REPO / "deploy" / "scripts" / "upgrade.sh"

#: The four keys `deploy/k8s-k0s/secrets.sh` manages. rotate/finalize must carry
#: every one of them through untouched (the apply overwrites the whole `data`
#: map, so a key the script fails to read back is a key it deletes).
MANAGED_KEYS = (
    "E2B_API_KEYS",
    "E2B_INTERNAL_API_KEY",
    "E2B_REDIS_PASSWORD",
    "E2B_SECRET_MASTER_KEY",
)

MASTER = "E2B_SECRET_MASTER_KEY"
MASTER_LIST = "E2B_SECRET_MASTER_KEYS"

OLD_MASTER = "master-old-0123456789abcdef0123456789abcdef"
NEW_MASTER = "master-new-fedcba9876543210fedcba9876543210"


def _fp(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _token(master_key: str, value: str) -> str:
    """A Fernet token from the registry's own derivation (no test-local crypto)."""
    return _fernet(master_key).encrypt(value.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# 假 kubectl：只认识这个脚本真的会发的调用；`exec` 会**真的**跑 shipped snippet
# ---------------------------------------------------------------------------

_STUB_BODY = r'''
"""Fake kubectl for rotate-secret-master.sh's contract tests."""
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path(os.environ["STUB_ROOT"])
argv = sys.argv[1:]


def fail(message):
    print("stub kubectl: " + message, file=sys.stderr)
    raise SystemExit(1)


def load(name, default=None):
    path = root / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


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

# 记下来的形状与操作者手敲的一致（`-n` 已剥掉，apply 本来就不带它）。
with (root / "calls.log").open("a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\n")

verb = args[0]

# --- 集群身份闸门（deploy/scripts/lib/cluster-guard.sh）---------------------
# 闸门在任何 kubectl 之前问这三个只读问题（都是 cluster-scoped，不带 `-n`）；答不出/
# 答错就是"连错集群"。它们在下面那条"每个调用都要带 -n sandlock"的断言之前。
if verb == "config" and args[1:] == ["current-context"]:
    print("k0s-sandlock")
    raise SystemExit(0)

if verb == "version":
    if args[1:] != ["-o", "json"]:
        fail("version only supports -o json")
    print(json.dumps({
        "clientVersion": {"gitVersion": "v1.37.1"},
        "serverVersion": {"gitVersion": "v1.36.4+k0s"},
    }))
    raise SystemExit(0)

if verb == "get" and args[1:2] == ["nodes"]:
    if args[2:] != ["-o", "json"]:
        fail("get nodes only supports -o json")
    print(json.dumps({"items": [
        {"metadata": {"name": "izuf697v12g31dyz4uvsjlz"},
         "status": {"nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}}},
        {"metadata": {"name": "izuf6d1usviqv6x9qk1hpcz"},
         "status": {"nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}}},
    ]}))
    raise SystemExit(0)

# `get`/`create`/`rollout`/`exec` 都要带 -n sandlock；`apply -f -` 的 namespace 来自
# 清单对象本身（与 secrets.sh 一样不打 -n），所以那个分支单独判。
if verb != "apply" and ns != "sandlock":
    fail("expected -n sandlock, got %r in %r" % (ns, argv))

if verb == "get" and args[1] == "secret":
    data = load("secret.json")
    if data is None:
        fail('Error from server (NotFound): secrets "e2b-secrets" not found')
    if args[2] != "e2b-secrets":
        fail("unexpected secret %r" % (args[2],))
    if "-o" not in args:
        print("e2b-secrets")
        raise SystemExit(0)
    output = args[args.index("-o") + 1]
    if not output.startswith("go-template="):
        fail("unexpected -o %r" % (output,))
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
        fail("unexpected template %r" % (template,))
    print(data[match.group(1)], end="")
    raise SystemExit(0)

if verb == "get" and args[1] == "deploy":
    if args[2] != "control-plane":
        fail("unexpected deployment %r" % (args[2],))
    if args[args.index("-o") + 1] != "json":
        fail("get deploy only supports -o json")
    obj = load("deploy.json")
    if obj is None:
        fail('Error from server (NotFound): deployments.apps "control-plane" not found')
    print(json.dumps(obj))
    raise SystemExit(0)

if verb == "get" and args[1] == "pods":
    if args[2:4] != ["-l", "app=control-plane"]:
        fail("unexpected pod selector %r" % (args[2:],))
    if args[args.index("-o") + 1] != "json":
        fail("get pods only supports -o json")
    print(json.dumps({"items": load("pods.json", [])}))
    raise SystemExit(0)

if verb == "create":
    if "--dry-run=client" not in args or args[1:3] != ["secret", "generic"]:
        fail("create only supports the secret client dry-run shape")
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
        fail("apply only supports -f -")
    text = sys.stdin.read()
    namespace = re.search(r"^  namespace: (\S+)$", text, re.M)
    target = namespace.group(1) if namespace else "default"
    if "\ndata:" not in text:
        fail("the manifest carries no data block")
    block = text.split("\ndata:", 1)[1].split("\nkind:", 1)[0]
    payload = {}
    for key, encoded in re.findall(r"^  ([A-Za-z0-9_]+): (.*)$", block, re.M):
        # kubectl 对空值渲染成 `K: ""`（不是空串），其余是 base64。
        if encoded == '""':
            payload[key] = ""
        else:
            payload[key] = base64.b64decode(encoded).decode()
    if target != "sandlock":
        fail("applied into namespace %r" % (target,))
    (root / "secret.json").write_text(json.dumps(payload), encoding="utf-8")
    print("secret/e2b-secrets configured")
    raise SystemExit(0)

if verb == "rollout":
    if args[1] not in ("restart", "status") or args[2] != "deploy/control-plane":
        fail("unexpected rollout %r" % (args[1:],))
    # NOTE: restart does NOT move the pods' env here -- a real cluster would,
    # which is exactly why the judgement has to read the live pods instead of
    # trusting that "a restart was issued". Tests simulate the roll by editing
    # pods.json (and by re-running the production SecretRegistry over the tree).
    if args[1] == "restart":
        print("deployment.apps/control-plane restarted")
    else:
        print('deployment "control-plane" successfully rolled out')
    raise SystemExit(0)

if verb == "exec":
    rest = args[1:]
    pod = None
    container = None
    command = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--":
            command = rest[i + 1:]
            break
        if token == "-c":
            container = rest[i + 1]
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        pod = token
        i += 1
    if pod is None or container != "control-plane" or not command:
        fail("exec only supports <pod> -c control-plane -- <cmd>")
    pods = {p["metadata"]["name"]: p for p in load("pods.json", [])}
    if pod not in pods:
        fail("no such pod %r" % (pod,))
    env = dict(os.environ)
    env.update(pods[pod].get("env") or {})
    if command[0] == "printenv":
        key = command[1]
        if key in env:
            print(env[key])
            raise SystemExit(0)
        raise SystemExit(1)
    if command[0] == "python3" and command[1:] == ["-"]:
        snippet = sys.stdin.read()
        extra = os.environ.get("STUB_PYTHONPATH")
        if extra:
            env["PYTHONPATH"] = extra + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-"],
            input=snippet,
            text=True,
            env=env,
            cwd=os.environ["STUB_CWD"],
            capture_output=True,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)
    fail("unsupported exec command %r" % (command,))

fail("unsupported verb %r" % (verb,))
'''

#: A fake `redis` module for the in-pod snippet (`create_redis_client` is
#: `redis.from_url(url, decode_responses=False)`). Records live in a JSON file so
#: a test can seed old-key ciphertext under `e2b:secret:*`.
_FAKE_REDIS = r'''
"""Minimal fake `redis` module (only from_url/keys/get/set are used)."""
import fnmatch
import json
import os
from pathlib import Path


class _Client:
    def __init__(self, path):
        self._path = Path(path)

    def _data(self):
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def keys(self, pattern):
        return [key for key in self._data() if fnmatch.fnmatch(key, pattern)]

    def get(self, key):
        value = self._data().get(key)
        return None if value is None else value.encode("utf-8")

    def set(self, key, value):
        data = self._data()
        data[key] = value.decode("utf-8") if isinstance(value, bytes) else value
        self._path.write_text(json.dumps(data), encoding="utf-8")


def from_url(url, decode_responses=False):
    return _Client(os.environ["STUB_REDIS_STATE"])
'''


class Cluster:
    """一个只有这个脚本能看懂的假集群（仓库内 tmp/，用完即删）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin_dir = root / "bin"
        self.workspace = root / "workspace"
        self.redis_state = root / "redis.json"
        self.calls = root / "calls.log"
        #: 闸门要求 KUBECONFIG 指向一份**真实存在**的文件（先验存在、再问集群身份）。
        self.kubeconfig = root / "kubeconfig"
        self.python_path = ""

    @property
    def secrets_dir(self) -> Path:
        return self.workspace / "_secrets"

    def secret_path(self) -> Path:
        return self.root / "secret.json"

    def seed_secret(self, payload: dict[str, str]) -> None:
        self.secret_path().write_text(json.dumps(payload), encoding="utf-8")

    def secret(self) -> dict[str, str]:
        return json.loads(self.secret_path().read_text(encoding="utf-8"))

    def seed_deploy(self, **overrides) -> dict:
        obj = {
            "metadata": {"name": "control-plane", "generation": 7},
            "spec": {"replicas": 2},
            "status": {
                "observedGeneration": 7,
                "replicas": 2,
                "updatedReplicas": 2,
                "availableReplicas": 2,
                "unavailableReplicas": 0,
            },
        }
        for key, value in overrides.items():
            section, field = key.split("__", 1)
            obj[section][field] = value
        (self.root / "deploy.json").write_text(json.dumps(obj), encoding="utf-8")
        return obj

    def seed_pods(self, master_keys: list[str | None]) -> list[dict]:
        """``None`` = 这个副本的 env 里根本没有 ``E2B_SECRET_MASTER_KEY``。"""
        pods = []
        for index, key in enumerate(master_keys):
            env = {
                "E2B_WORKSPACE_BASE": str(self.workspace),
                "E2B_REDIS_URL": "redis://redis:6379/0",
                "STUB_REDIS_STATE": str(self.redis_state),
            }
            if key is not None:
                env[MASTER] = key
            pods.append(
                {
                    "metadata": {
                        "name": f"control-plane-{index}",
                        "deletionTimestamp": None,
                    },
                    "status": {"phase": "Running"},
                    "env": env,
                }
            )
        (self.root / "pods.json").write_text(json.dumps(pods), encoding="utf-8")
        return pods

    def seed_redis_payload(self, redis_key: str, payload: dict) -> None:
        state = {}
        if self.redis_state.exists():
            state = json.loads(self.redis_state.read_text(encoding="utf-8"))
        state[redis_key] = json.dumps(payload)
        self.redis_state.write_text(json.dumps(state), encoding="utf-8")

    def calls_made(self) -> list[str]:
        if not self.calls.exists():
            return []
        return self.calls.read_text(encoding="utf-8").splitlines()


@pytest.fixture()
def cluster():
    (REPO / "tmp").mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="o3-rotate-master-", dir=REPO / "tmp"))
    try:
        fake = Cluster(root)
        fake.bin_dir.mkdir()
        kubectl = fake.bin_dir / "kubectl"
        kubectl.write_text(f"#!{sys.executable}\n{_STUB_BODY}", encoding="utf-8")
        kubectl.chmod(0o755)
        fake.kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
        # 脚本用本地 python3 解析 `-o json`；把它钉在本测试解释器上（PATH 里第一个
        # 就是这个 stub bin，所以真的 kubectl 从来不会被碰到）。
        python3 = fake.bin_dir / "python3"
        python3.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
        )
        python3.chmod(0o755)
        fake_redis = root / "fake_redis"
        fake_redis.mkdir()
        (fake_redis / "redis.py").write_text(_FAKE_REDIS, encoding="utf-8")
        fake.python_path = str(fake_redis)
        fake.workspace.mkdir()
        yield fake
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run(cluster_obj: Cluster, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{cluster_obj.bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_ROOT"] = str(cluster_obj.root)
    env["STUB_CWD"] = str(REPO)
    env["STUB_PYTHONPATH"] = cluster_obj.python_path
    env["STUB_REDIS_STATE"] = str(cluster_obj.redis_state)
    # 假 kubectl 一旦没被用上，真的那个会立刻失败 —— 这个用例永远不会碰到集群。
    env["KUBECONFIG"] = str(cluster_obj.kubeconfig)
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


#: 集群身份闸门（`deploy/scripts/lib/cluster-guard.sh`）成功时打在 **stderr** 的
#: 那一行确认。它在任何 kubectl 之前跑，所以下面每个断言 `stderr` 序号的用例都往后
#: 挪一行 —— 闸门的输出也是脚本输出的一部分，一个字都不许变。
TARGET_CLUSTER_LINE = (
    "✓ target cluster: context=k0s-sandlock server=v1.36.4+k0s nodes=2"
    " (izuf697v12g31dyz4uvsjlz izuf6d1usviqv6x9qk1hpcz)"
)


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _plain_secret(primary: str, *, keys: str | None = None) -> dict[str, str]:
    payload = {
        "E2B_API_KEYS": "api-key-0123456789abcdef0123456789abcdef",
        "E2B_INTERNAL_API_KEY": "internal-key-0123456789abcdef0123456789abcdef",
        "E2B_REDIS_PASSWORD": "redis-password-0123456789abcdef",
        MASTER: primary,
    }
    if keys is not None:
        payload[MASTER_LIST] = keys
    return payload


def _write_records(cluster_obj: Cluster, key: str) -> dict[str, str]:
    """经真 `SecretRegistry` 在 `_secrets/**` 写下由 ``key`` 加密的记录。"""
    registry = SecretRegistry(cluster_obj.secrets_dir, master_key=key)
    ids = {}
    for name in ("api_key", "token"):
        ids[name] = registry.create(
            name, f"{name}-value-0123456789abcdef0123456789abcdef"
        ).secret_id
    return ids


def _all_rolled(cluster_obj: Cluster, master_keys: list[str]) -> None:
    """判据 1+2 都过的那一版集群（generation 已 observed，副本都拿主 key）。"""
    cluster_obj.seed_deploy()
    cluster_obj.seed_pods(master_keys)


# ---------------------------------------------------------------------------
# 文本契约（简报 Step 1）
# ---------------------------------------------------------------------------


def test_rotate_script_has_the_same_three_beats_as_the_compose_one():
    text = _script_text()
    assert "rotate" in text and "finalize" in text
    assert "E2B_SECRET_MASTER_KEYS" in text
    assert "cannot remove the current master key" in text   # 与 upgrade.sh 同一句守卫


def test_finalize_checks_for_stale_ciphertext_before_it_is_allowed():
    text = _script_text()
    assert "e2b:secret:" in text          # 扫 Redis 里的 legacy 密文
    assert "rollout status" in text       # 确认没有旧副本在跑


def test_the_script_says_which_fields_it_reads_and_which_it_compares():
    """判据必须能在脚本里读出来：读什么、比什么，而不是一句"已经滚完了"。"""
    text = _script_text()
    for needle in (
        "observedGeneration",       # 判据 1：status 描述的是当前 generation
        "updatedReplicas",          # 判据 1：没有 pod 还挂在旧 ReplicaSet 上
        "printenv",                 # 判据 2：直接读副本进程里的 key
        "_secrets",                 # 判据 3：磁盘记录
        "e2b:secret:",              # 判据 3：redis 镜像
        "encrypted",                # 判据 3：明文记录也要拦
    ):
        assert needle in text, needle


def test_the_script_never_prints_a_credential_and_never_traces():
    text = _script_text()
    # trace prints every variable it expands, including the key.
    assert "set -x" not in text
    assert "sha256" in text
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for name in (MASTER, MASTER_LIST):
            if f"${name}" in line:
                assert "echo" not in line, line


def test_the_guard_sentences_are_the_ones_upgrade_sh_uses():
    """"不要发明第三种"：两条守卫逐句与 compose 侧相同（指纹形态只改措辞）。"""
    compose = UPGRADE.read_text(encoding="utf-8")
    k0s = _script_text()
    assert "不能移除当前主 key" in compose
    assert "旧 key 已不在生效列表" in compose
    assert "不能移除当前主 key" in k0s
    assert "旧 key 已不在生效列表" in k0s


def test_the_runbook_writes_the_three_beats_and_the_judgement_down():
    text = DOC.read_text(encoding="utf-8")
    section = text[text.index("## 4.6"):]
    for needle in (
        "deploy/k8s-k0s/rotate-secret-master.sh rotate",
        "deploy/k8s-k0s/rotate-secret-master.sh status",
        "finalize",
        "observedGeneration",
        "updatedReplicas",
        "printenv",
        "e2b:secret:",
        "upgrade.sh",
    ):
        assert needle in section, needle


# ---------------------------------------------------------------------------
# 行为：拿假 kubectl 真跑脚本
# ---------------------------------------------------------------------------


def test_rotate_retires_the_current_primary_into_the_window_and_rolls_the_cp(cluster):
    cluster.seed_secret(_plain_secret(OLD_MASTER))
    _all_rolled(cluster, [OLD_MASTER, OLD_MASTER])
    _write_records(cluster, OLD_MASTER)

    result = _run(cluster, "rotate")

    assert result.returncode == 0
    stored = cluster.secret()
    new_primary = stored[MASTER]
    assert re.fullmatch(r"[0-9a-f]{64}", new_primary)
    # upgrade.sh 的形状：旧主 key 进并存列表，单值槽换成新主 key（新 key 不进列表）。
    assert stored[MASTER_LIST] == OLD_MASTER
    for key in ("E2B_API_KEYS", "E2B_INTERNAL_API_KEY", "E2B_REDIS_PASSWORD"):
        assert stored[key] == _plain_secret(OLD_MASTER)[key]
    # 硬要求：任何输出里不许出现凭据明文（含被退役的旧 key）。
    for secret_value in list(stored.values()) + [OLD_MASTER]:
        assert secret_value not in result.stdout + result.stderr
    assert result.stdout == _fingerprint_table(stored)
    assert result.stderr.splitlines() == [
        TARGET_CLUSTER_LINE,
        f"rotate：{MASTER} 换主 key：旧 {_fp(OLD_MASTER)} → 新 {_fp(new_primary)}"
        "（只打指纹，值不出脚本）",
        f"窗口：{MASTER_LIST} 现在持有下面这些旧 key（finalize 的地址就是它们的指纹）",
        f"    {MASTER_LIST} 成员 {_fp(OLD_MASTER)} len:{len(OLD_MASTER)}",
        "secret/e2b-secrets configured",
        "已 apply 并回读 sandlock/e2b-secrets（5 个键）",
        "第 2 拍（全副本滚动）：kubectl -n sandlock rollout restart deploy/control-plane",
        "deployment.apps/control-plane restarted",
        "第 2 拍（续）：kubectl -n sandlock rollout status deploy/control-plane --timeout=300s",
        'deployment "control-plane" successfully rolled out',
        "第 3 拍之前先只读地验判据：deploy/k8s-k0s/rotate-secret-master.sh status（不改任何东西）",
        f"判据过了再摘旧 key：deploy/k8s-k0s/rotate-secret-master.sh finalize {_fp(OLD_MASTER)}",
        "⚠ rotate 只开了窗口：窗口开着 != 可以摘 —— 先 status 过判据，再 finalize",
    ]
    assert [call for call in cluster.calls_made() if call.startswith("rollout")] == [
        "rollout restart deploy/control-plane",
        "rollout status deploy/control-plane --timeout=300s",
    ]


def test_rotate_keeps_an_existing_window_list_and_appends_the_retired_primary(cluster):
    older = "master-oldest-0123456789abcdef01234567"
    cluster.seed_secret(_plain_secret(OLD_MASTER, keys=older))

    result = _run(cluster, "rotate")

    assert result.returncode == 0
    stored = cluster.secret()
    # 逐字等于 upgrade.sh 的 join：原列表在前，被退役的主 key 追加在后。
    assert stored[MASTER_LIST] == f"{older},{OLD_MASTER}"
    assert older not in result.stdout + result.stderr
    assert result.stderr.splitlines()[:5] == [
        TARGET_CLUSTER_LINE,
        f"rotate：{MASTER} 换主 key：旧 {_fp(OLD_MASTER)} → 新 {_fp(stored[MASTER])}"
        "（只打指纹，值不出脚本）",
        f"窗口：{MASTER_LIST} 现在持有下面这些旧 key（finalize 的地址就是它们的指纹）",
        f"    {MASTER_LIST} 成员 {_fp(older)} len:{len(older)}",
        f"    {MASTER_LIST} 成员 {_fp(OLD_MASTER)} len:{len(OLD_MASTER)}",
    ]


def test_rotate_refuses_without_a_current_master_key_or_a_secret(cluster):
    cluster.seed_secret({"E2B_API_KEYS": "api-key-0123456789abcdef"})
    before = cluster.secret()

    no_primary = _run(cluster, "rotate")

    assert no_primary.returncode != 0
    assert no_primary.stdout == ""
    assert no_primary.stderr.splitlines() == [
        TARGET_CLUSTER_LINE,
        "rotate-secret-master.sh: 无法确定当前 secret master key"
        "（sandlock/e2b-secrets 缺 E2B_SECRET_MASTER_KEY）：没有它 rotate 会把旧记录的"
        "窗口切断 —— 先跑 deploy/k8s-k0s/secrets.sh 补上它（O3 Task 1）"
    ]
    assert cluster.secret() == before

    cluster.secret_path().unlink()
    missing = _run(cluster, "rotate")

    assert missing.returncode != 0
    assert missing.stdout == ""
    assert missing.stderr.splitlines() == [
        TARGET_CLUSTER_LINE,
        "rotate-secret-master.sh: sandlock/e2b-secrets 不存在："
        "先跑 deploy/k8s-k0s/secrets.sh 把它建出来"
    ]


def test_finalize_refuses_the_current_primary_an_empty_list_and_an_unknown_key(cluster):
    cluster.seed_secret(_plain_secret(NEW_MASTER, keys=OLD_MASTER))

    current = _run(cluster, "finalize", NEW_MASTER)

    assert current.returncode != 0
    assert current.stdout == ""
    assert current.stderr.splitlines() == [
        TARGET_CLUSTER_LINE,
        "rotate-secret-master.sh: 不能移除当前主 key（cannot remove the current master key）："
        f"{_fp(NEW_MASTER)} 就是 {MASTER} —— 先跑 rotate 生成新主 key"
        "（与 deploy/scripts/upgrade.sh 的同一句守卫）"
    ]
    assert cluster.secret()[MASTER_LIST] == OLD_MASTER

    unknown = _run(cluster, "finalize", "not-in-the-list")

    assert unknown.returncode != 0
    assert unknown.stderr.splitlines() == [
        TARGET_CLUSTER_LINE,
        f"rotate-secret-master.sh: {MASTER_LIST} 里没有 {_fp('not-in-the-list')}："
        "地址既不是列表里的 key，也不是它打印过的 sha256 前 16 位"
    ]

    cluster.seed_secret(_plain_secret(NEW_MASTER))
    empty = _run(cluster, "finalize", OLD_MASTER)

    assert empty.returncode != 0
    assert empty.stderr.splitlines() == [
        TARGET_CLUSTER_LINE,
        f"rotate-secret-master.sh: {MASTER_LIST} 为空：旧 key 已不在生效列表"
    ]


def test_finalize_refuses_while_a_replica_still_runs_the_old_key(cluster):
    """判据 1+2 就是"全副本滚动"这一拍：有一个旧副本在，finalize 必须被拒。"""
    cluster.seed_secret(_plain_secret(NEW_MASTER, keys=OLD_MASTER))
    # rollout 正在半途：observedGeneration 落后，只有一个副本是新的。
    cluster.seed_deploy(
        metadata__generation=8,
        status__observedGeneration=7,
        status__updatedReplicas=1,
        status__availableReplicas=1,
        status__unavailableReplicas=1,
    )
    cluster.seed_pods([NEW_MASTER, OLD_MASTER])
    _write_records(cluster, NEW_MASTER)
    before = cluster.secret()

    result = _run(cluster, "finalize", _fp(OLD_MASTER))

    assert result.returncode != 0
    lines = result.stderr.splitlines()
    assert lines[0] == TARGET_CLUSTER_LINE
    assert lines[1] == (
        "判据 1/3（没有旧副本在跑）：deploy/control-plane "
        "metadata.generation=8 status.observedGeneration=7 spec.replicas=2 "
        "status.replicas=2 status.updatedReplicas=1 status.availableReplicas=1 "
        "status.unavailableReplicas=1 ⇒ 未通过"
    )
    assert lines[2] == (
        f"判据 2/3（每个 running 副本拿的都是当前主 key）：Secret 主 key {_fp(NEW_MASTER)}；"
        f"running 副本 2 个（spec.replicas=2）：control-plane-0 {_fp(NEW_MASTER)} ✓、"
        f"control-plane-1 {_fp(OLD_MASTER)} ✗（≠ 主 key）⇒ 未通过"
    )
    assert lines[3] == (
        "判据 3/3（at-rest 没有还要旧 key 才能解开的记录）⇒ 未执行"
        "（判据 2 未过：副本还没拿到当前主 key，此刻在 pod 里扫没有意义，不算通过）"
    )
    assert lines[4] == (
        "rotate-secret-master.sh: finalize 被拒：上面有判据未通过 —— 旧 key 一旦摘掉，"
        "仍由它加密的记录就永久解不开。让所有副本都滚到主 key（判据 1/2）、确认 at-rest "
        "全是主 key 能单独解开的密文（判据 3）再重试；Secret 一个字没动。"
    )
    assert cluster.secret() == before


def test_finalize_refuses_when_a_disk_record_still_needs_the_old_key(cluster):
    """判据 3 的实弹：磁盘上还有只用旧 key 加过密的记录 ⇒ 拒跑。"""
    cluster.seed_secret(_plain_secret(NEW_MASTER, keys=OLD_MASTER))
    _all_rolled(cluster, [NEW_MASTER, NEW_MASTER])      # 判据 1+2 都过
    ids = _write_records(cluster, OLD_MASTER)           # …但记录还是旧 key 的密文
    before = cluster.secret()

    result = _run(cluster, "finalize", _fp(OLD_MASTER))

    assert result.returncode != 0
    lines = result.stderr.splitlines()
    paths = sorted(
        str(cluster.secrets_dir / secret_id / "secret.json")
        for secret_id in ids.values()
    )
    assert lines[0] == TARGET_CLUSTER_LINE
    assert lines[1].endswith("status.unavailableReplicas=0 ⇒ 通过")
    assert lines[2] == (
        f"判据 2/3（每个 running 副本拿的都是当前主 key）：Secret 主 key {_fp(NEW_MASTER)}；"
        f"running 副本 2 个（spec.replicas=2）：control-plane-0 {_fp(NEW_MASTER)} ✓、"
        f"control-plane-1 {_fp(NEW_MASTER)} ✓⇒ 通过"
    )
    assert lines[3:5] == [
        f"    ✗ {path}：需要旧 key 才能解开（主 key 单独解不开）" for path in paths
    ]
    assert lines[5] == (
        "    扫到 2 条 at-rest 记录（磁盘 2 + redis 0）：2 条有问题（见上）"
    )
    assert lines[6] == (
        "判据 3/3（at-rest 没有还要旧 key 才能解开的记录）⇒ 未通过"
    )
    assert lines[7] == (
        "rotate-secret-master.sh: finalize 被拒：上面有判据未通过 —— 旧 key 一旦摘掉，"
        "仍由它加密的记录就永久解不开。让所有副本都滚到主 key（判据 1/2）、确认 at-rest "
        "全是主 key 能单独解开的密文（判据 3）再重试；Secret 一个字没动。"
    )
    assert cluster.secret() == before


def test_finalize_refuses_when_a_redis_mirror_record_still_needs_the_old_key(cluster):
    cluster.seed_secret(_plain_secret(NEW_MASTER, keys=OLD_MASTER))
    _all_rolled(cluster, [NEW_MASTER, NEW_MASTER])
    _write_records(cluster, NEW_MASTER)
    cluster.seed_redis_payload(
        "e2b:secret:sec_old",
        {
            "secret_id": "sec_old",
            "name": "old",
            "value": _token(OLD_MASTER, "value-only-the-old-key-opens"),
            "encrypted": True,
        },
    )
    before = cluster.secret()

    result = _run(cluster, "finalize", _fp(OLD_MASTER))

    assert result.returncode != 0
    assert (
        "    ✗ e2b:secret:sec_old：需要旧 key 才能解开（主 key 单独解不开）"
        in result.stderr.splitlines()
    )
    # 明文值（哪怕是测试里的假值）不许出现在输出里。
    assert "value-only-the-old-key-opens" not in result.stderr
    assert cluster.secret() == before


def test_finalize_refuses_when_a_replica_has_no_master_key_in_its_env(cluster):
    """副本的 env 里根本没有主 key（`optional: true` 被漏掉的那种 Secret）也要拒跑。"""
    cluster.seed_secret(_plain_secret(NEW_MASTER, keys=OLD_MASTER))
    _all_rolled(cluster, [NEW_MASTER, None])
    _write_records(cluster, NEW_MASTER)
    before = cluster.secret()

    result = _run(cluster, "finalize", _fp(OLD_MASTER))

    assert result.returncode != 0
    lines = result.stderr.splitlines()
    assert lines[0] == TARGET_CLUSTER_LINE
    assert lines[2] == (
        f"判据 2/3（每个 running 副本拿的都是当前主 key）：Secret 主 key {_fp(NEW_MASTER)}；"
        f"running 副本 2 个（spec.replicas=2）：control-plane-0 {_fp(NEW_MASTER)} ✓、"
        "control-plane-1 读不到（exec 失败或 env 未设置）✗⇒ 未通过"
    )
    assert lines[3] == (
        "判据 3/3（at-rest 没有还要旧 key 才能解开的记录）⇒ 未执行"
        "（判据 2 未过：副本还没拿到当前主 key，此刻在 pod 里扫没有意义，不算通过）"
    )
    assert cluster.secret() == before


def test_status_is_read_only_and_prints_the_same_judgement(cluster):
    cluster.seed_secret(_plain_secret(NEW_MASTER, keys=OLD_MASTER))
    _all_rolled(cluster, [NEW_MASTER, NEW_MASTER])
    _write_records(cluster, NEW_MASTER)
    before = cluster.secret()

    result = _run(cluster, "status")

    assert result.returncode == 0
    assert cluster.secret() == before
    assert [
        call for call in cluster.calls_made()
        if call.split()[0] in ("rollout", "apply", "create")
    ] == []
    assert result.stdout == _fingerprint_table(before)
    assert result.stderr.splitlines()[0] == TARGET_CLUSTER_LINE
    assert result.stderr.splitlines()[1].startswith("判据 1/3（没有旧副本在跑）：")
    assert result.stderr.splitlines()[-1] == (
        "判据全过：可以 finalize（摘旧 key 是不可逆点，执行前再确认一次）"
    )


def test_finalize_removes_the_old_key_once_every_replica_and_record_has_rolled(cluster):
    cluster.seed_secret(_plain_secret(NEW_MASTER, keys=f"older-key,{OLD_MASTER}"))
    _all_rolled(cluster, [NEW_MASTER, NEW_MASTER])
    _write_records(cluster, NEW_MASTER)

    result = _run(cluster, "finalize", _fp(OLD_MASTER))

    assert result.returncode == 0
    stored = cluster.secret()
    # 只摘掉点名的那一个；更早的旧 key 还在列表里（upgrade.sh 的同一条公式）。
    assert stored[MASTER_LIST] == "older-key"
    assert stored[MASTER] == NEW_MASTER
    for key in ("E2B_API_KEYS", "E2B_INTERNAL_API_KEY", "E2B_REDIS_PASSWORD"):
        assert stored[key] == _plain_secret(NEW_MASTER)[key]
    assert OLD_MASTER not in result.stdout + result.stderr
    assert result.stdout == _fingerprint_table(stored)
    assert result.stderr.splitlines()[:4] == [
        TARGET_CLUSTER_LINE,
        "判据 1/3（没有旧副本在跑）：deploy/control-plane "
        "metadata.generation=7 status.observedGeneration=7 spec.replicas=2 "
        "status.replicas=2 status.updatedReplicas=2 status.availableReplicas=2 "
        "status.unavailableReplicas=0 ⇒ 通过",
        f"判据 2/3（每个 running 副本拿的都是当前主 key）：Secret 主 key {_fp(NEW_MASTER)}；"
        f"running 副本 2 个（spec.replicas=2）：control-plane-0 {_fp(NEW_MASTER)} ✓、"
        f"control-plane-1 {_fp(NEW_MASTER)} ✓⇒ 通过",
        "判据 3/3（at-rest 没有还要旧 key 才能解开的记录）："
        "扫到 2 条 at-rest 记录（磁盘 2 + redis 0）："
        "全部 encrypted:true 且主 key 单独可解 ⇒ 通过",
    ]
    assert result.stderr.splitlines()[-4:] == [
        "secret/e2b-secrets configured",
        "已 apply 并回读 sandlock/e2b-secrets（5 个键）",
        f"finalize：已从 {MASTER_LIST} 移除 {_fp(OLD_MASTER)}（旧 key 立即失效）；"
        f"{MASTER} 未动（仍是 {_fp(NEW_MASTER)}）",
        "finalize 完成后建议再滚一次 CP（可选）：旧 key 已不参与密文，滚掉它只是让运行时"
        "内存里也不再持有 —— kubectl -n sandlock rollout restart deploy/control-plane",
    ]


def test_finalize_can_empty_the_window_list_when_that_was_the_last_old_key(cluster):
    cluster.seed_secret(_plain_secret(NEW_MASTER, keys=OLD_MASTER))
    _all_rolled(cluster, [NEW_MASTER, NEW_MASTER])
    _write_records(cluster, NEW_MASTER)

    result = _run(cluster, "finalize", OLD_MASTER)      # 地址也可以就是 key 本身

    assert result.returncode == 0
    # 空列表 = 没有窗口，正是常态（`optional: true`；键留着但为空，与 compose 侧同形）。
    assert cluster.secret()[MASTER_LIST] == ""


def test_the_three_beats_end_to_end(cluster):
    """rotate →（模拟全副本滚动）→ 判据从"未过"变"通过" → finalize 摘旧 key。"""
    cluster.seed_secret(_plain_secret(OLD_MASTER))
    _all_rolled(cluster, [OLD_MASTER, OLD_MASTER])
    _write_records(cluster, OLD_MASTER)

    rotated = _run(cluster, "rotate")

    assert rotated.returncode == 0
    new_primary = cluster.secret()[MASTER]
    assert cluster.secret()[MASTER_LIST] == OLD_MASTER

    # 中间态：Secret 已经是新主 key，但副本还没滚完、记录还是旧 key 的密文。
    cluster.seed_deploy(
        metadata__generation=8,
        status__observedGeneration=8,
        status__updatedReplicas=1,
        status__availableReplicas=1,
        status__unavailableReplicas=1,
    )
    cluster.seed_pods([new_primary, OLD_MASTER])

    mid = _run(cluster, "status")

    assert mid.returncode != 0
    assert mid.stderr.splitlines()[0] == TARGET_CLUSTER_LINE
    assert mid.stderr.splitlines()[1].endswith("status.unavailableReplicas=1 ⇒ 未通过")
    assert mid.stderr.splitlines()[2].endswith("✗（≠ 主 key）⇒ 未通过")
    assert mid.stderr.splitlines()[3] == (
        "判据 3/3（at-rest 没有还要旧 key 才能解开的记录）⇒ 未执行"
        "（判据 2 未过：副本还没拿到当前主 key，此刻在 pod 里扫没有意义，不算通过）"
    )
    assert mid.stderr.splitlines()[-1] == (
        "判据未全过：finalize 会被拒 —— 见上面逐条（Secret 一个字没动）"
    )
    # 这一步同样没写东西：判据不过，Secret 不动。
    assert cluster.secret()[MASTER] == new_primary

    # "全副本滚动"这一拍：新副本起来后，registry 启动时的 _scan_disk/_scan_redis 会把
    # 旧密文重写成主 key 密文 —— 这里用的就是生产代码本身（SecretRegistry），
    # 不是测试里另写的等价物。
    cluster.seed_deploy()
    cluster.seed_pods([new_primary, new_primary])
    SecretRegistry(
        cluster.secrets_dir,
        master_key=new_primary,
        legacy_master_keys=(OLD_MASTER,),
    )

    ready = _run(cluster, "status")

    assert ready.returncode == 0
    assert ready.stderr.splitlines()[-1] == (
        "判据全过：可以 finalize（摘旧 key 是不可逆点，执行前再确认一次）"
    )

    final = _run(cluster, "finalize", _fp(OLD_MASTER))

    assert final.returncode == 0
    assert cluster.secret()[MASTER_LIST] == ""
    assert cluster.secret()[MASTER] == new_primary
    # 摘完之后数据仍然可读（主 key 单独就能解开每条记录），且值逐字不变。
    after = SecretRegistry(cluster.secrets_dir, master_key=new_primary)
    assert {record.name: record.value for record in after.list()} == {
        "api_key": "api_key-value-0123456789abcdef0123456789abcdef",
        "token": "token-value-0123456789abcdef0123456789abcdef",
    }
