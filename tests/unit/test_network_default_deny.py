"""namespace 级 ingress 默认拒绝：谁进谁、以及为什么只有 ingress。

这份清单是在集群上核对过流量之后写的，apply 之后当场验过四条通道；这里的用例钉住
那个结论的形状，防止它被改回去。
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
POLICIES = REPO / "deploy" / "k8s" / "default-deny.yaml"
KUSTOMIZATION = REPO / "deploy" / "k8s" / "kustomization.yaml"


def _docs() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(POLICIES.read_text(encoding="utf-8"))
        if doc
    ]


def _only(name: str) -> dict:
    matches = [doc for doc in _docs() if doc["metadata"]["name"] == name]
    assert len(matches) == 1, (name, [doc["metadata"]["name"] for doc in _docs()])
    return matches[0]


def test_the_namespace_denies_all_ingress_by_default() -> None:
    """The floor: every pod, nothing allowed.

    `ingress: []` and not a missing key -- an empty list is the explicit
    "nothing gets in", which is what a reader has to see here.
    """
    deny = _only("sandlock-default-deny")
    assert deny["kind"] == "NetworkPolicy"
    assert deny["spec"]["podSelector"] == {}
    assert deny["spec"]["policyTypes"] == ["Ingress"]
    assert deny["spec"]["ingress"] == []


def test_egress_is_out_of_scope_for_a_named_reason() -> None:
    """No policy here may claim Egress -- the argument is in the manifest header.

    An egress half would have to blanket-allow the worker (it carries sandbox
    egress: `fd_inject_connect` builds the connection in the worker's netns, so
    cutting it takes every sandbox off the internet) and the control plane (its
    buildkit sidecar pulls public images). Two blanket allows out of five
    workloads buys nothing, and it would put kube-apiserver reachability -- a
    host process in k0s, reached at `10.96.0.1:443` -- behind Calico's
    pre/post-DNAT behaviour. If someone adds one, this fails and they have to
    argue with the header first.
    """
    for doc in _docs():
        name = doc["metadata"]["name"]
        assert "Egress" not in (doc["spec"].get("policyTypes") or []), name
        assert not doc["spec"].get("egress"), name


def test_only_the_named_sources_reach_the_named_ports() -> None:
    """Each exception names its source and its port, exactly.

    `e2b-control-plane` is the one with no `from`, and it has to stay that way:
    it is the public entry point, and the kubelet's `httpGet` probes arrive from
    the node (measured: the live Deployment has `/healthz` on 3000), which no
    pod selector can name. The other two are control-plane-only.
    """
    entry = _only("e2b-control-plane")["spec"]["ingress"]
    assert entry == [
        {
            "ports": [
                {"protocol": "TCP", "port": 3000},
                {"protocol": "TCP", "port": 49983},
            ]
        }
    ], "the public entry point must accept the node's probes too"

    for name, selector, port in (
        ("e2b-redis", {"app": "redis"}, 6379),
        ("e2b-worker", {"app": "e2b-worker"}, 49983),
    ):
        policy = _only(name)
        assert policy["spec"]["podSelector"]["matchLabels"] == selector, name
        assert policy["spec"]["ingress"] == [
            {
                "from": [
                    {"podSelector": {"matchLabels": {"app": "control-plane"}}}
                ],
                "ports": [{"protocol": "TCP", "port": port}],
            }
        ], name


def test_the_default_deny_ships_in_the_baseline_kustomization() -> None:
    """It has to be in the set `apply.sh` renders, not just on disk."""
    resources = [
        line.strip().lstrip("- ")
        for line in KUSTOMIZATION.read_text(encoding="utf-8").splitlines()
    ]
    assert "default-deny.yaml" in resources
