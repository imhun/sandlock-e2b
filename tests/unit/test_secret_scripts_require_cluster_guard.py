"""N71: the two write-side Secret scripts go through the cluster-identity gate.

The 2026-10-02 accident (`deploy/k8s-k0s/apply.sh -h` with no `KUBECONFIG`
applied the whole stack to the ACK cluster behind this machine's default
context) produced the gate in `deploy/scripts/lib/cluster-guard.sh` and the nail
`tests/unit/test_apply_requires_cluster_guard.py`. Two scripts that write the
`e2b-secrets` Secret were left out:
`deploy/k8s-k0s/secrets.sh` and `deploy/k8s-k0s/rotate-secret-master.sh`. Every
mode has to pass the gate -- including the read-only `--fingerprint` and
`status`: the gate answers "is this the right cluster", not "will this run
write".

The property, one case per script and per mode:

* `KUBECONFIG` unset -> non-zero exit, **no write verb recorded at all** (that is
  the accident's own shape: no `KUBECONFIG`, and the script wrote anyway);
* the stub answers `v1.34.3-aliyun.1` -> non-zero exit, the refusal names that
  exact version, **no write verb recorded**;
* the right shape (2 x arm64 x `+k0s`, `gitVersion` with `+k0s`) -> the script
  walks its original path exactly as the sibling cases of
  `tests/unit/test_{k0s_secrets,rotate_secret_master}_script.py` expect.

The stub kubectl is prepended to `PATH` and answers exactly the calls the gate
and the two scripts make; anything else is a refusal, so a missing verb goes red
instead of being silently agreed to. `_stub_is_the_kubectl` asserts the stub
*is* the `kubectl` on `PATH` in every case -- a real one is never reachable,
which is also why the RED evidence for this file could be taken by just running
it before the scripts were wired (the write verb showed up in the log).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SECRETS = REPO / "deploy" / "k8s-k0s" / "secrets.sh"
ROTATE = REPO / "deploy" / "k8s-k0s" / "rotate-secret-master.sh"

#: The verbs a write-side script must never reach without passing the gate.
WRITE_VERBS = (
    "apply",
    "create",
    "delete",
    "patch",
    "rollout",
    "replace",
    "scale",
    "edit",
    "set",
)

#: What the gate asks, and what the two scripts ask. Anything else is a stub gap
#: and has to fail (never widen this into "answer everything").
STUB_KUBECTL = '''#!/usr/bin/env python3
"""Fake kubectl for the two write-side Secret scripts.

Records every argv, answers exactly the cluster-identity gate
(`config current-context`, `version -o json`, `get nodes -o json`) plus the
calls `secrets.sh` / `rotate-secret-master.sh` make, and fails on anything else.
"""
import base64
import json
import os
import re
import sys
from pathlib import Path

root = Path(os.environ["STUB_ROOT"])
argv = sys.argv[1:]
with (root / "calls.log").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")


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

verb = args[0] if args else ""

# --- the cluster-identity gate (deploy/scripts/lib/cluster-guard.sh) ---------
# Cluster-scoped, so deliberately no `-n`: the gate asks these before anything
# namespaced, and they are the only calls it may make.
if verb == "config" and args[1:] == ["current-context"]:
    print(os.environ.get("STUB_CONTEXT", "k0s-sandlock"))
    raise SystemExit(0)

if verb == "version":
    if args[1:] != ["-o", "json"]:
        fail("version only supports -o json")
    print(os.environ["STUB_VERSION_JSON"])
    raise SystemExit(0)

if verb == "get" and args[1:2] == ["nodes"]:
    if args[2:] != ["-o", "json"]:
        fail("get nodes only supports -o json")
    print(os.environ["STUB_NODES_JSON"])
    raise SystemExit(0)

# --- the scripts' own calls (all namespaced except `apply -f -`) --------------
if verb != "apply" and ns != "sandlock":
    fail("expected -n sandlock, got %r in %r" % (ns, argv))

if verb == "get" and args[1:2] == ["secret"]:
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
    if template == r'{{range $k, $v := .data}}{{$k}}{{"\\n"}}{{end}}':
        for key in data:
            print(key)
        raise SystemExit(0)
    if template == "{{len .data}}":
        print(len(data), end="")
        raise SystemExit(0)
    match = re.fullmatch(r'\\{\\{index \\.data "([^"]+)" \\| base64decode\\}\\}', template)
    if not match or match.group(1) not in data:
        fail("unexpected template %r" % (template,))
    print(data[match.group(1)], end="")
    raise SystemExit(0)

if verb == "get" and args[1:2] == ["deploy"]:
    if args[2] != "control-plane":
        fail("unexpected deployment %r" % (args[2],))
    obj = load("deploy.json")
    if obj is None:
        fail('Error from server (NotFound): deployments.apps "control-plane" not found')
    print(json.dumps(obj))
    raise SystemExit(0)

if verb == "get" and args[1:2] == ["pods"]:
    if args[2:4] != ["-l", "app=control-plane"]:
        fail("unexpected pod selector %r" % (args[2:],))
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
    if "\\ndata:" not in text:
        fail("the manifest carries no data block")
    block = text.split("\\ndata:", 1)[1].split("\\nkind:", 1)[0]
    payload = {}
    for key, encoded in re.findall(r"^  ([A-Za-z0-9_]+): (.*)$", block, re.M):
        payload[key] = "" if encoded == '""' else base64.b64decode(encoded).decode()
    (root / "secret.json").write_text(json.dumps(payload), encoding="utf-8")
    print("secret/e2b-secrets configured")
    raise SystemExit(0)

if verb == "rollout":
    if args[1] not in ("restart", "status") or args[2] != "deploy/control-plane":
        fail("unexpected rollout %r" % (args[1:],))
    if args[1] == "restart":
        print("deployment.apps/control-plane restarted")
    else:
        print('deployment "control-plane" successfully rolled out')
    raise SystemExit(0)

if verb == "exec":
    rest = args[1:]
    pod = None
    command = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--":
            command = rest[i + 1:]
            break
        if token == "-c":
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        pod = token
        i += 1
    pods = {p["metadata"]["name"]: p for p in load("pods.json", [])}
    if pod not in pods or not command:
        fail("exec only supports <pod> -c control-plane -- <cmd>")
    env = dict(os.environ)
    env.update(pods[pod].get("env") or {})
    if command[0] == "printenv":
        value = env.get(command[1])
        if value is None:
            raise SystemExit(1)
        print(value)
        raise SystemExit(0)
    if command[0] == "python3" and command[1:] == ["-"]:
        sys.stdin.read()
        print(
            "扫到 0 条 at-rest 记录（磁盘 0 + redis 0）："
            "全部 encrypted:true 且主 key 单独可解"
        )
        raise SystemExit(0)
    fail("unsupported exec command %r" % (command,))

fail("unsupported verb %r" % (verb,))
'''

#: The two clusters this machine can be pointed at. The ACK shape is the one the
#: default context reaches (`docs/deploy-clusters.md` §1).
ACK_SERVER = {
    "clientVersion": {"gitVersion": "v1.37.1"},
    "serverVersion": {"gitVersion": "v1.34.3-aliyun.1"},
}
K0S_SERVER = {
    "clientVersion": {"gitVersion": "v1.37.1"},
    "serverVersion": {"gitVersion": "v1.36.4+k0s"},
}
K0S_NODES = {
    "items": [
        {
            "metadata": {"name": "izuf697v12g31dyz4uvsjlz"},
            "status": {
                "nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}
            },
        },
        {
            "metadata": {"name": "izuf6d1usviqv6x9qk1hpcz"},
            "status": {
                "nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}
            },
        },
    ]
}

#: The gate's one-line confirmation (stderr). The stub's `current-context` is
#: `k0s-sandlock`, and `config current-context` is the first call it answers.
TARGET_CLUSTER_LINE = (
    "✓ target cluster: context=k0s-sandlock server=v1.36.4+k0s nodes=2"
    " (izuf697v12g31dyz4uvsjlz izuf6d1usviqv6x9qk1hpcz)"
)

MANAGED_KEYS = (
    "E2B_API_KEYS",
    "E2B_INTERNAL_API_KEY",
    "E2B_REDIS_PASSWORD",
    "E2B_SECRET_MASTER_KEY",
    "E2B_C3_AGENT_TOKEN",
)

MASTER = "E2B_SECRET_MASTER_KEY"
MASTER_VALUE = "master-0123456789abcdef0123456789abcdef"


def _fingerprint_table(values: dict[str, str]) -> str:
    lines = [
        "# sandlock/e2b-secrets 指纹（sha256 前 16 位；值不出机器；未轮换的键指纹逐字不变）"
    ]
    for key, value in values.items():
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        lines.append(f"{key} sha256:{digest} len:{len(value)}")
    return "\n".join(lines) + "\n"


def _stub_env(
    tmp_path: Path,
    *,
    server: dict,
    nodes: dict,
    kubeconfig: Path | None,
) -> tuple[dict, Path]:
    """A stub kubectl on `PATH` (the only one reachable) + the argv log.

    ``kubeconfig=None`` is the accident's shape: the operator never set
    `KUBECONFIG` at all.
    """
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "kubectl"
    stub.write_text(STUB_KUBECTL, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_ROOT"] = str(tmp_path)
    env["STUB_CONTEXT"] = "k0s-sandlock"
    env["STUB_VERSION_JSON"] = json.dumps(server)
    env["STUB_NODES_JSON"] = json.dumps(nodes)
    if kubeconfig is None:
        env.pop("KUBECONFIG", None)
    else:
        env["KUBECONFIG"] = str(kubeconfig)
    return env, log


def _stub_is_the_kubectl(env: dict) -> None:
    """The stub must be the `kubectl` that resolves on this `PATH`."""
    assert shutil.which("kubectl", path=env["PATH"]) == str(
        Path(env["PATH"].split(os.pathsep)[0]) / "kubectl"
    )


def _kubeconfig(tmp_path: Path) -> Path:
    """A kubeconfig that really exists -- the gate checks existence first."""
    path = tmp_path / "kubeconfig"
    path.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    return path


def _run(env: dict, script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _recorded(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _writes(recorded: list[list[str]]) -> list[list[str]]:
    return [call for call in recorded if any(arg in WRITE_VERBS for arg in call)]


def _seed_rolled_cluster(tmp_path: Path) -> dict[str, str]:
    """The `status` shape: a Secret plus a fully-rolled control plane."""
    secret = {
        "E2B_API_KEYS": "api-key-0123456789abcdef0123456789abcdef",
        "E2B_INTERNAL_API_KEY": "internal-key-0123456789abcdef0123456789abcdef",
        "E2B_REDIS_PASSWORD": "redis-password-0123456789abcdef",
        MASTER: MASTER_VALUE,
    }
    (tmp_path / "secret.json").write_text(json.dumps(secret), encoding="utf-8")
    (tmp_path / "deploy.json").write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )
    pods = [
        {
            "metadata": {"name": f"control-plane-{index}", "deletionTimestamp": None},
            "status": {"phase": "Running"},
            "env": {MASTER: MASTER_VALUE},
        }
        for index in range(2)
    ]
    (tmp_path / "pods.json").write_text(json.dumps(pods), encoding="utf-8")
    return secret


#: One case per script *and* per mode. The two read-only modes are the trap this
#: task exists for: `--fingerprint` and `status` never write, so it would be easy
#: to argue they do not need the gate -- but the gate's question is "am I even
#: talking to the right cluster", and a read-only run against the ACK cluster
#: prints the *ACK* Secret's fingerprints while looking exactly like a good run.
CASES = [
    pytest.param(SECRETS, ["--fingerprint"], id="secrets-fingerprint"),
    pytest.param(SECRETS, [], id="secrets-write"),
    pytest.param(ROTATE, ["status"], id="rotate-status"),
    pytest.param(ROTATE, ["rotate"], id="rotate-write"),
]


# --- (a) no KUBECONFIG: nothing is dialled at all ---------------------------


@pytest.mark.parametrize("script,args", CASES)
def test_unset_kubeconfig_is_refused_before_any_kubectl_call(
    tmp_path: Path, script: Path, args: list[str]
) -> None:
    # Seed the shape that makes each script reach its real path (so a missing
    # gate shows up as the recorded write, not as a "nothing to do" early exit).
    _seed_rolled_cluster(tmp_path)
    env, log = _stub_env(tmp_path, server=K0S_SERVER, nodes=K0S_NODES, kubeconfig=None)
    _stub_is_the_kubectl(env)

    result = _run(env, script, *args)

    # Asserted first so a regression reports the recorded write, not a symptom.
    assert _writes(_recorded(log)) == []
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.splitlines()[0] == (
        "cluster-guard: refusing to run kubectl against the default context:"
        " KUBECONFIG is not set"
    )
    # The accident's shape, exactly: no KUBECONFIG and the script still wrote.
    # Now the gate refuses *before* kubectl, so nothing was dialled at all --
    # the strongest form of "the write never happened".
    assert _recorded(log) == []


# --- (b) the ACK cluster: refused, and the version is named ------------------


@pytest.mark.parametrize("script,args", CASES)
def test_the_ack_cluster_is_refused_before_any_write(
    tmp_path: Path, script: Path, args: list[str]
) -> None:
    _seed_rolled_cluster(tmp_path)
    env, log = _stub_env(
        tmp_path, server=ACK_SERVER, nodes=K0S_NODES, kubeconfig=_kubeconfig(tmp_path)
    )
    _stub_is_the_kubectl(env)

    result = _run(env, script, *args)

    assert _writes(_recorded(log)) == []
    assert result.returncode == 2
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0] == (
        "cluster-guard: ✗ cluster-guard refused this run: 连到的不是本项目的 k0s 集群"
        "（refusing to run kubectl against this cluster）"
    )
    # The refusal names the version it saw, character for character -- that is
    # what makes "which cluster did I just talk to" answerable from the output.
    assert lines[1] == "cluster-guard:   context=k0s-sandlock  server=v1.34.3-aliyun.1"
    assert lines[3] == (
        "cluster-guard:   实际：serverVersion.gitVersion=v1.34.3-aliyun.1 不含 +k0s"
        " —— 这形状就是本机默认 context 指的"
    )
    # The stub *was* called (the identity probe), and only for the identity: no
    # `get secret`, no `create`, no `apply`.
    assert _recorded(log) == [["config", "current-context"], ["version", "-o", "json"]]


# --- (c) the right shape: the original path runs as before ------------------


def test_secrets_writes_normally_once_the_gate_passes(tmp_path: Path) -> None:
    """The write path is reachable -- so (a)/(b) are not green by doing nothing."""
    env, log = _stub_env(
        tmp_path, server=K0S_SERVER, nodes=K0S_NODES, kubeconfig=_kubeconfig(tmp_path)
    )
    _stub_is_the_kubectl(env)

    result = _run(env, SECRETS)

    assert result.returncode == 0
    recorded = _recorded(log)
    assert recorded[:3] == [
        ["config", "current-context"],
        ["version", "-o", "json"],
        ["get", "nodes", "-o", "json"],
    ]
    stored = json.loads((tmp_path / "secret.json").read_text(encoding="utf-8"))
    assert list(stored) == list(MANAGED_KEYS)
    # The gate's line is stderr-only; stdout stays the fingerprint table.
    assert result.stdout == _fingerprint_table(stored)
    assert result.stderr.splitlines()[0] == TARGET_CLUSTER_LINE
    # And the write really happened through the stub (not a dry run).
    assert ["-n", "sandlock", "create", "secret", "generic", "e2b-secrets", *[
        f"--from-literal={key}={value}" for key, value in stored.items()
    ], "--dry-run=client", "-o", "yaml"] in recorded
    assert ["apply", "-f", "-"] in recorded


def test_rotate_status_runs_normally_once_the_gate_passes(tmp_path: Path) -> None:
    env, log = _stub_env(
        tmp_path, server=K0S_SERVER, nodes=K0S_NODES, kubeconfig=_kubeconfig(tmp_path)
    )
    _stub_is_the_kubectl(env)
    secret = _seed_rolled_cluster(tmp_path)

    result = _run(env, ROTATE, "status")

    assert result.returncode == 0
    lines = result.stderr.splitlines()
    assert lines[0] == TARGET_CLUSTER_LINE
    assert lines[-1] == "判据全过：可以 finalize（摘旧 key 是不可逆点，执行前再确认一次）"
    assert result.stdout == _fingerprint_table(secret)
    # `status` is read-only, and that is *proven* by the recorded argv again --
    # the gate ran, the judgement ran, nothing wrote.
    recorded = _recorded(log)
    assert recorded[:3] == [
        ["config", "current-context"],
        ["version", "-o", "json"],
        ["get", "nodes", "-o", "json"],
    ]
    assert _writes(recorded) == []
    assert ["-n", "sandlock", "get", "deploy", "control-plane", "-o", "json"] in recorded
    assert [
        "-n",
        "sandlock",
        "get",
        "pods",
        "-l",
        "app=control-plane",
        "-o",
        "json",
    ] in recorded
