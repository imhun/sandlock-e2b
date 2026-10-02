"""Task 7: `deploy/scripts/lib/cluster-guard.sh` gates every write-side script.

Why this file exists: 2026-10-02 an `apply.sh -h` (the script has no `-h`
branch) went through the normal path **without `KUBECONFIG`** and applied the
whole stack to the ACK cluster that this machine's default context points at
(`docs/deploy-clusters.md` §7.34.1). The two root causes were "the script
assumes the caller exported KUBECONFIG" and "no write-side script asserts the
target cluster's shape". `require_target_cluster` closes both, and this file is
the nail on its *judgement*: five refusal shapes plus the passing shape, each
driven through a stub `kubectl` prepended to `PATH`, each asserted with **exact
string equality** on the whole stderr (no `in`-style matching: the refusal must
name the value it saw and the value it wanted, character for character).

The stub answers only `config current-context`, `version -o json` and
`get nodes -o json` -- the only calls the guard is allowed to make -- so a guard
that grew an extra cluster call would fail here instead of silently running it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
GUARD = REPO / "deploy" / "scripts" / "lib" / "cluster-guard.sh"

#: A `kubectl` that records its argv and answers exactly the three calls the
#: guard makes. Anything else is a refusal, so the call set is pinned here.
STUB_KUBECTL = '''#!/usr/bin/env python3
"""Fake kubectl: current-context / version -o json / get nodes -o json."""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

if argv[:2] == ["config", "current-context"]:
    print(os.environ.get("STUB_CONTEXT", "main"))
    raise SystemExit(0)
if argv[:2] == ["version", "-o"] and argv[2:] == ["json"]:
    print(os.environ["STUB_VERSION_JSON"])
    raise SystemExit(int(os.environ.get("STUB_VERSION_RC", "0")))
if argv[:2] == ["get", "nodes"] and argv[2:] == ["-o", "json"]:
    print(os.environ["STUB_NODES_JSON"])
    raise SystemExit(int(os.environ.get("STUB_NODES_RC", "0")))
raise SystemExit("stub kubectl: unhandled argv %r" % (argv,))
'''

#: The shape of the cluster this machine's default context points at.
ACK_SERVER = {
    "clientVersion": {"gitVersion": "v1.37.1"},
    "serverVersion": {"gitVersion": "v1.34.3-aliyun.1"},
}
K0S_SERVER = {
    "clientVersion": {"gitVersion": "v1.37.1"},
    "serverVersion": {"gitVersion": "v1.36.4+k0s"},
}


def _node(name: str, arch: str, kubelet: str) -> dict:
    return {
        "metadata": {"name": name},
        "status": {"nodeInfo": {"architecture": arch, "kubeletVersion": kubelet}},
    }


K0S_NODES = [
    _node("izuf697v12g31dyz4uvsjlz", "arm64", "v1.36.4+k0s"),
    _node("izuf6d1usviqv6x9qk1hpcz", "arm64", "v1.36.4+k0s"),
]

#: 3 nodes, mixed arch, no `+k0s` anywhere: the ACK shape, wrong on the count
#: *and* on the arch, so one refusal has to name both.
ACK_NODES = [
    _node("cn-shanghai.172.18.93.1", "amd64", "v1.34.3-aliyun.1"),
    _node("cn-shanghai.172.18.94.2", "arm64", "v1.34.3-aliyun.1"),
    _node("cn-shanghai.172.18.94.3", "amd64", "v1.34.3-aliyun.1"),
]

#: Right count, one wrong architecture: pins that the arch check is not the
#: count check wearing a hat.
WRONG_ARCH_NODES = [
    _node("izuf697v12g31dyz4uvsjlz", "amd64", "v1.36.4+k0s"),
    _node("izuf6d1usviqv6x9qk1hpcz", "arm64", "v1.36.4+k0s"),
]

#: The 期望 line shared by the shape refusals (kept literal, not computed).
EXPECTATION = (
    "cluster-guard:   期望：节点数 == 2；每台 architecture=arm64、kubeletVersion 含 +k0s；"
    "serverVersion.gitVersion 含 +k0s"
)


def _stub(
    tmp_path: Path, *, version: dict, nodes: list[dict], context: str = "main"
) -> tuple[dict, Path]:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "kubectl"
    stub.write_text(STUB_KUBECTL, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "kubectl-argv.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_LOG"] = str(log)
    env["STUB_CONTEXT"] = context
    env["STUB_VERSION_JSON"] = json.dumps(version)
    env["STUB_NODES_JSON"] = json.dumps({"items": nodes})
    return env, log


def _run(env: dict, *, kubeconfig: object, shell: str = "bash") -> subprocess.CompletedProcess:
    """Source the guard and call it, exactly the way the deploy scripts do.

    ``kubeconfig`` is either a path or ``None`` for "the operator never set
    KUBECONFIG" -- the accident's actual shape.
    """
    env = dict(env)
    if kubeconfig is None:
        env.pop("KUBECONFIG", None)
    else:
        env["KUBECONFIG"] = str(kubeconfig)
    return subprocess.run(
        [shell, "-c", f'. "{GUARD}"; require_target_cluster'],
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


def _kubeconfig(tmp_path: Path) -> Path:
    path = tmp_path / "kubeconfig"
    path.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    return path


# --- refusals that never touch a cluster ------------------------------------


def test_unset_kubeconfig_is_refused_without_calling_kubectl(tmp_path: Path) -> None:
    env, log = _stub(tmp_path, version=K0S_SERVER, nodes=K0S_NODES)
    result = _run(env, kubeconfig=None)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "cluster-guard: refusing to run kubectl against the default context: KUBECONFIG is not set\n"
        "cluster-guard:   没设 KUBECONFIG 时 kubectl 会用 ~/.kube/config 的 current-context —— 本机默认指的\n"
        "cluster-guard:   是另一套阿里云 ACK 集群（7 节点 / v1.34.3-aliyun.1 / 没有 sandlock namespace），\n"
        "cluster-guard:   不是本项目的 k0s 集群（2 节点 arm64 / +k0s）。先显式指向目标集群：\n"
        'cluster-guard:     export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"\n'
        "cluster-guard:   没有这份文件就先跑 deploy/scripts/open-cluster-tunnel.sh（它建通道并写它）\n"
    )
    # The refusal happens before kubectl: nothing was dialled at all.
    assert _recorded(log) == []


def test_kubeconfig_pointing_at_a_missing_file_is_refused(tmp_path: Path) -> None:
    env, log = _stub(tmp_path, version=K0S_SERVER, nodes=K0S_NODES)
    missing = tmp_path / "no-such-kubeconfig"
    result = _run(env, kubeconfig=missing)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        f"cluster-guard: refusing to run kubectl: KUBECONFIG is set to {missing} but that file does not exist\n"
        "cluster-guard:   读不到 kubeconfig 就不去猜目标集群。修正路径，或用\n"
        "cluster-guard:   deploy/scripts/open-cluster-tunnel.sh 重新取一份（写 tmp/k0s/kubeconfig）；\n"
        "cluster-guard:   绝不要退回去用 ~/.kube/config —— 那是另一套阿里云 ACK 集群。\n"
    )
    assert _recorded(log) == []


def test_kubeconfig_as_a_path_list_is_refused(tmp_path: Path) -> None:
    env, log = _stub(tmp_path, version=K0S_SERVER, nodes=K0S_NODES)
    result = _run(env, kubeconfig=f"{_kubeconfig(tmp_path)}:/etc/kubernetes/admin.conf")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "cluster-guard: refusing to run kubectl: KUBECONFIG 是一个路径列表（含 ':'），不是一个目标集群\n"
        "cluster-guard:   闸门只接受单一目标集群的一份 kubeconfig（见 docs/deploy-clusters.md §0）：\n"
        'cluster-guard:     export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"\n'
    )
    assert _recorded(log) == []


# --- refusals decided from the cluster's answers ----------------------------


def test_the_ack_server_version_is_refused_and_named(tmp_path: Path) -> None:
    env, log = _stub(tmp_path, version=ACK_SERVER, nodes=ACK_NODES)
    result = _run(env, kubeconfig=_kubeconfig(tmp_path))
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "cluster-guard: ✗ cluster-guard refused this run: 连到的不是本项目的 k0s 集群"
        "（refusing to run kubectl against this cluster）\n"
        "cluster-guard:   context=main  server=v1.34.3-aliyun.1\n"
        f"{EXPECTATION}\n"
        "cluster-guard:   实际：serverVersion.gitVersion=v1.34.3-aliyun.1 不含 +k0s —— 这形状就是本机默认 context 指的\n"
        "cluster-guard:   阿里云 ACK 集群（7 节点 / v1.34.3-aliyun.1 / 没有 sandlock namespace）\n"
        'cluster-guard:   核对与修法见 docs/deploy-clusters.md §1/§2；对准目标集群：export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"\n'
    )
    # The server check is enough: no node listing was asked for.
    assert _recorded(log) == [["config", "current-context"], ["version", "-o", "json"]]


def test_a_wrong_node_shape_is_refused_and_every_node_is_named(tmp_path: Path) -> None:
    env, _ = _stub(tmp_path, version=K0S_SERVER, nodes=ACK_NODES)
    result = _run(env, kubeconfig=_kubeconfig(tmp_path))
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "cluster-guard: ✗ cluster-guard refused this run: 连到的不是本项目的 k0s 集群"
        "（refusing to run kubectl against this cluster）\n"
        "cluster-guard:   context=main  server=v1.36.4+k0s  nodes=3\n"
        f"{EXPECTATION}\n"
        "cluster-guard:   实际（每台）：\n"
        "cluster-guard:     cn-shanghai.172.18.93.1  architecture=amd64  kubeletVersion=v1.34.3-aliyun.1\n"
        "cluster-guard:     cn-shanghai.172.18.94.2  architecture=arm64  kubeletVersion=v1.34.3-aliyun.1\n"
        "cluster-guard:     cn-shanghai.172.18.94.3  architecture=amd64  kubeletVersion=v1.34.3-aliyun.1\n"
        "cluster-guard:   不符项：\n"
        "cluster-guard:     - 节点数 3 != 2\n"
        "cluster-guard:     - cn-shanghai.172.18.93.1: architecture=amd64 != arm64\n"
        "cluster-guard:     - cn-shanghai.172.18.93.1: kubeletVersion=v1.34.3-aliyun.1 不含 +k0s\n"
        "cluster-guard:     - cn-shanghai.172.18.94.2: kubeletVersion=v1.34.3-aliyun.1 不含 +k0s\n"
        "cluster-guard:     - cn-shanghai.172.18.94.3: architecture=amd64 != arm64\n"
        "cluster-guard:     - cn-shanghai.172.18.94.3: kubeletVersion=v1.34.3-aliyun.1 不含 +k0s\n"
        "cluster-guard:   核对与修法见 docs/deploy-clusters.md §1/§2\n"
    )


def test_the_right_node_count_with_a_wrong_arch_is_still_refused(tmp_path: Path) -> None:
    env, _ = _stub(tmp_path, version=K0S_SERVER, nodes=WRONG_ARCH_NODES)
    result = _run(env, kubeconfig=_kubeconfig(tmp_path))
    assert result.returncode == 2
    assert result.stderr == (
        "cluster-guard: ✗ cluster-guard refused this run: 连到的不是本项目的 k0s 集群"
        "（refusing to run kubectl against this cluster）\n"
        "cluster-guard:   context=main  server=v1.36.4+k0s  nodes=2\n"
        f"{EXPECTATION}\n"
        "cluster-guard:   实际（每台）：\n"
        "cluster-guard:     izuf697v12g31dyz4uvsjlz  architecture=amd64  kubeletVersion=v1.36.4+k0s\n"
        "cluster-guard:     izuf6d1usviqv6x9qk1hpcz  architecture=arm64  kubeletVersion=v1.36.4+k0s\n"
        "cluster-guard:   不符项：\n"
        "cluster-guard:     - izuf697v12g31dyz4uvsjlz: architecture=amd64 != arm64\n"
        "cluster-guard:   核对与修法见 docs/deploy-clusters.md §1/§2\n"
    )


def test_an_unreachable_server_is_named_with_the_kubectl_stderr(tmp_path: Path) -> None:
    env, _ = _stub(tmp_path, version={"clientVersion": {"gitVersion": "v1.37.1"}}, nodes=[])
    env["STUB_VERSION_RC"] = "1"
    result = _run(env, kubeconfig=_kubeconfig(tmp_path))
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "cluster-guard: ✗ cluster-guard refused this run: 读不到 server 版本（kubectl version -o json 退出码 1）\n"
        "cluster-guard:   context=main\n"
        f"{EXPECTATION}\n"
        "cluster-guard:   kubectl stderr: <空>\n"
        "cluster-guard:   通道没开就先跑 deploy/scripts/open-cluster-tunnel.sh（server=https://127.0.0.1:16443）\n"
    )


# --- the passing shape ------------------------------------------------------


def test_the_k0s_shape_passes_and_prints_one_confirmation_line(tmp_path: Path) -> None:
    env, _ = _stub(tmp_path, version=K0S_SERVER, nodes=K0S_NODES, context="Default")
    result = _run(env, kubeconfig=_kubeconfig(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == (
        "✓ target cluster: context=Default server=v1.36.4+k0s nodes=2"
        " (izuf697v12g31dyz4uvsjlz izuf6d1usviqv6x9qk1hpcz)\n"
    )


def test_the_confirmation_is_on_stderr_under_sh_as_well(tmp_path: Path) -> None:
    """The helper is sourced by `sh` callers too, and its one line is stderr.

    stderr matters: `apply.sh`'s DRY_RUN stdout is a data stream fed to kubectl
    (`deploy/k8s-k0s/apply.sh` header), so a confirmation on stdout would be
    parsed as YAML by the next command in that pipe.
    """
    env, _ = _stub(tmp_path, version=K0S_SERVER, nodes=K0S_NODES, context="Default")
    result = _run(env, kubeconfig=_kubeconfig(tmp_path), shell="sh")
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == (
        "✓ target cluster: context=Default server=v1.36.4+k0s nodes=2"
        " (izuf697v12g31dyz4uvsjlz izuf6d1usviqv6x9qk1hpcz)\n"
    )
