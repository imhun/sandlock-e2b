"""Task 7: the regression nail for the 2026-10-02 accident.

`deploy/k8s-k0s/apply.sh -h` (no `-h` branch) with **no `KUBECONFIG`** applied
the whole stack to the ACK cluster behind this machine's default context. The
nail is the property that was missing: when the cluster-identity gate refuses,
`apply.sh` performs **no write-side kubectl call at all** -- `apply`, `delete`,
`patch`, `scale`, `rollout`, `create`, `label`, ... The recorded argv is the
witness, and the two shapes are the accident's own (`KUBECONFIG` unset, and
`KUBECONFIG` pointing at the ACK cluster), each run twice: plain and
`DRY_RUN=1` (the accident ran the write path; DRY_RUN must be refused too).

The stub kubectl is prepended to `PATH` and answers every call, so a real
cluster cannot be reached even if the script under test is the broken version
-- which is exactly how the RED evidence for this nail was taken (the broken
run recorded a `kubectl apply -f -`).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
APPLY = REPO / "deploy" / "k8s-k0s" / "apply.sh"
#: `deploy/stack/.version` is gitignored (written by build-and-push.sh), so it is
#: read where it is used rather than at import time.
VERSION_FILE = REPO / "deploy" / "stack" / ".version"

#: The verbs a write-side script must never reach without passing the gate.
WRITE_VERBS = (
    "apply",
    "delete",
    "patch",
    "scale",
    "rollout",
    "create",
    "label",
    "annotate",
    "replace",
    "edit",
    "set",
    "cordon",
    "drain",
    "expose",
)

#: What `kubectl kustomize` would print for this overlay: one `e2b-sandlock-*`
#: image tag that `apply.sh` pins to `deploy/stack/.version`.
RENDER = """apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: e2b-worker
spec:
  template:
    spec:
      containers:
        - name: worker
          image: registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:0.1.0
"""

STUB_KUBECTL = '''#!/usr/bin/env python3
"""A kubectl that records its argv and answers every call apply.sh makes."""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

if argv[:1] == ["kustomize"]:
    sys.stdout.write(os.environ["STUB_RENDER"])
    raise SystemExit(0)
if argv[:2] == ["config", "current-context"]:
    print(os.environ.get("STUB_CONTEXT", "main"))
    raise SystemExit(0)
if argv[:2] == ["version", "-o"]:
    print(os.environ["STUB_VERSION_JSON"])
    raise SystemExit(0)
if argv[:2] == ["get", "nodes"]:
    print(os.environ["STUB_NODES_JSON"])
    raise SystemExit(0)
# Anything else the script asks for (rollout status, exec, get ...) succeeds
# silently: the point of this stub is the recorded argv, not the answers.
raise SystemExit(0)
'''

K0S_SERVER = {
    "clientVersion": {"gitVersion": "v1.37.1"},
    "serverVersion": {"gitVersion": "v1.36.4+k0s"},
}
ACK_SERVER = {
    "clientVersion": {"gitVersion": "v1.37.1"},
    "serverVersion": {"gitVersion": "v1.34.3-aliyun.1"},
}
K0S_NODES = {
    "items": [
        {
            "metadata": {"name": "izuf697v12g31dyz4uvsjlz"},
            "status": {"nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}},
        },
        {
            "metadata": {"name": "izuf6d1usviqv6x9qk1hpcz"},
            "status": {"nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}},
        },
    ]
}


def _stub_env(
    tmp_path: Path, *, server: dict, nodes: dict, kubeconfig: Path | None, dry_run: bool
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
    env["STUB_RENDER"] = RENDER
    env["STUB_VERSION_JSON"] = json.dumps(server)
    env["STUB_NODES_JSON"] = json.dumps(nodes)
    if kubeconfig is None:
        env.pop("KUBECONFIG", None)
    else:
        env["KUBECONFIG"] = str(kubeconfig)
    if dry_run:
        env["DRY_RUN"] = "1"
    else:
        env.pop("DRY_RUN", None)
    return env, log


def _recorded(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _writes(recorded: list[list[str]]) -> list[list[str]]:
    return [call for call in recorded if any(arg in WRITE_VERBS for arg in call)]


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply-path", "dry-run-path"])
def test_unset_kubeconfig_is_refused_before_any_kubectl_call(
    tmp_path: Path, dry_run: bool
) -> None:
    env, log = _stub_env(tmp_path, server=K0S_SERVER, nodes=K0S_NODES, kubeconfig=None, dry_run=dry_run)
    result = subprocess.run(
        [str(APPLY)], cwd=REPO, env=env, capture_output=True, text=True, check=False
    )
    # Asserted first so a regression reports the recorded write, not a later
    # symptom: this is the accident's own property.
    assert _writes(_recorded(log)) == []
    assert result.returncode == 2
    assert result.stderr.splitlines()[0] == (
        "cluster-guard: refusing to run kubectl against the default context: KUBECONFIG is not set"
    )
    # The accident: no KUBECONFIG, and the script still wrote. Now nothing runs.
    assert _recorded(log) == []


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply-path", "dry-run-path"])
def test_the_ack_cluster_is_refused_before_any_write(tmp_path: Path, dry_run: bool) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    env, log = _stub_env(
        tmp_path, server=ACK_SERVER, nodes=K0S_NODES, kubeconfig=kubeconfig, dry_run=dry_run
    )
    result = subprocess.run(
        [str(APPLY)], cwd=REPO, env=env, capture_output=True, text=True, check=False
    )
    assert _writes(_recorded(log)) == []
    assert result.returncode == 2
    assert result.stdout == ""
    # Exactly the identity probe ran: no `kustomize`, and no write verb.
    assert _recorded(log) == [["config", "current-context"], ["version", "-o", "json"]]


def test_dry_run_through_the_gate_keeps_stdout_a_clean_data_stream(tmp_path: Path) -> None:
    """Green path: the gate passes, and its confirmation line stays on stderr."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    env, log = _stub_env(
        tmp_path, server=K0S_SERVER, nodes=K0S_NODES, kubeconfig=kubeconfig, dry_run=True
    )
    result = subprocess.run(
        [str(APPLY)], cwd=REPO, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    # stdout is the rendered manifest, byte for byte -- the ✓ line went to stderr.
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert result.stdout == RENDER.replace(
        "byteplan/e2b-sandlock-worker:0.1.0", f"byteplan/e2b-sandlock-worker:{version}"
    )
    assert result.stderr.splitlines()[0] == (
        "✓ target cluster: context=main server=v1.36.4+k0s nodes=2"
        " (izuf697v12g31dyz4uvsjlz izuf6d1usviqv6x9qk1hpcz)"
    )
    assert _writes(_recorded(log)) == []
