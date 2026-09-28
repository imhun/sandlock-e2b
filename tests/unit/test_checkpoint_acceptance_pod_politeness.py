"""The acceptance script may not delete a worker pod that hosts somebody else's sandbox.

``deploy/scripts/checkpoint_acceptance.py`` replaces the worker hosting its own
sandbox with ``kubectl delete pod`` -- that is the only way to test "a pause
survives its worker". Every *other* sandbox that happens to live on that worker
dies with it, and the worker's runtime registry is in memory, so those sandboxes
become unresumable orphans. Until now that was left to a human look at the node
(Task 2's report calls its own ``ps``-based review a non-answer: the worker image
has no ``ps``), which is exactly the kind of check that cannot be run in CI or by
two people at once.

So the check lives in the script now: before the pod goes away, the node's
*control-plane* sandbox list must name nobody but ours, and ``--force`` is the
explicit way to say "they may die with it". These cases pin that refusal -- and,
just as important, that the refusal happens *before* any ``kubectl`` that could
delete the pod.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "checkpoint_acceptance.py"


def _load_script():
    """Import the script without needing a cluster.

    It reads ``KUBECONFIG`` at import time (fail closed: it must never fall back
    to whatever the local kubectl context happens to be), so the value has to
    exist for the import and nothing more -- these cases never shell out.
    """
    previous = os.environ.get("KUBECONFIG")
    os.environ["KUBECONFIG"] = str(REPO_ROOT / "tmp" / "k0s" / "kubeconfig")
    try:
        spec = importlib.util.spec_from_file_location("checkpoint_acceptance", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop("KUBECONFIG", None)
        else:
            os.environ["KUBECONFIG"] = previous
    return module


acceptance = _load_script()


class _Kubectl:
    """Records every `kubectl` the script would run; runs none of them."""

    def __init__(self, uid: str = "uid-before") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.uid = uid

    def __call__(self, *args: str, check: bool = True) -> SimpleNamespace:
        self.calls.append(args)
        stdout = f"{self.uid}\n" if args[:2] == ("get", "pod") else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _only_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acceptance, "node_sandbox_ids", lambda node_id: ["sbx_ours"])


def test_a_foreign_sandbox_refuses_and_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    kubectl = _Kubectl()
    monkeypatch.setattr(acceptance, "kubectl", kubectl)
    monkeypatch.setattr(
        acceptance,
        "node_sandbox_ids",
        lambda node_id: ["sbx_ours", "sbx_theirs"],
    )

    with pytest.raises(SystemExit) as exited:
        acceptance.delete_worker_pod("e2b-worker-0", "sbx_ours")

    assert exited.value.code == 2
    assert kubectl.calls == []
    assert capsys.readouterr().err == (
        "拒删 worker pod e2b-worker-0：它上面有 1 个不属于本次验收的沙箱：\n"
        "  - sbx_theirs\n"
        "删 pod 会把别人的沙箱一起打死（worker 的注册表是内存态，它们无法再 resume）。\n"
        "出路：先把它们迁走或杀掉（带 API key 的 `POST /sandboxes/<id>/migrate`，或 `Sandbox.kill(id)`）再重跑；\n"
        "      如果你确认它们可以和这个 pod 一起死，重跑时加 `--force` 显式承担。\n"
    )


def test_only_our_own_sandbox_lets_the_pod_go(monkeypatch: pytest.MonkeyPatch) -> None:
    kubectl = _Kubectl(uid="uid-42")
    monkeypatch.setattr(acceptance, "kubectl", kubectl)
    _only_ours(monkeypatch)

    uid = acceptance.delete_worker_pod("e2b-worker-0", "sbx_ours")

    assert uid == "uid-42"
    assert kubectl.calls == [
        ("get", "pod", "e2b-worker-0", "-o", "jsonpath={.metadata.uid}"),
        ("delete", "pod", "e2b-worker-0"),
    ]


def test_force_takes_the_deletion_on_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    kubectl = _Kubectl()
    monkeypatch.setattr(acceptance, "kubectl", kubectl)

    def unreadable(node_id: str) -> list[str]:
        raise AssertionError("--force must not consult the node's sandbox list")

    monkeypatch.setattr(acceptance, "node_sandbox_ids", unreadable)

    acceptance.delete_worker_pod("e2b-worker-0", "sbx_ours", force=True)

    assert [call for call in kubectl.calls if call[:2] == ("delete", "pod")] == [
        ("delete", "pod", "e2b-worker-0")
    ]


def test_an_unreadable_list_refuses_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    kubectl = _Kubectl()
    monkeypatch.setattr(acceptance, "kubectl", kubectl)

    def unreadable(node_id: str) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(acceptance, "node_sandbox_ids", unreadable)

    with pytest.raises(SystemExit) as exited:
        acceptance.delete_worker_pod("e2b-worker-0", "sbx_ours")

    assert exited.value.code == 2
    assert kubectl.calls == []
    assert capsys.readouterr().err == (
        "拒删 worker pod e2b-worker-0：读不到控制面的舰队沙箱归属名单"
        "（GET /internal/fleet/sandboxes）：RuntimeError: boom\n"
        "那张名单是「这台 worker 上没有别人的沙箱」的唯一证据；拿不到就不动 pod。\n"
        "出路：先把通道/控制面修好（deploy/scripts/open-cluster-tunnel.sh），"
        "并确认用的内部 key 能读 /internal/fleet/sandboxes（舰队作用域、需要内部 key，"
        "不需要是那个节点）再重跑；\n"
        "      确实要不看这张名单就删，用 `--force` 显式承担。\n"
    )


def test_the_politeness_list_comes_from_the_attributed_fleet_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One fleet-scope read, then this node's slice of the attribution.

    The old shape asked the **node-scoped** endpoint, which after C3 Task 2
    refuses an out-of-cluster caller holding the shared key (403) -- so this
    script would have hard-failed every run, in exactly the deploy window where
    it is used. The fleet view answers the same question without impersonation.
    """
    seen: list[str] = []

    def fake_get(url, *, headers, timeout):
        seen.append(url)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "sandboxes": {
                    "node_a": ["sbx_a1", "sbx_a2"],
                    "node_b": ["sbx_b"],
                }
            },
        )

    monkeypatch.setattr(acceptance.httpx, "get", fake_get)
    assert acceptance.node_sandbox_ids("node_b") == ["sbx_b"]
    assert acceptance.node_sandbox_ids("node_a") == ["sbx_a1", "sbx_a2"]
    # Absent from the attribution = the control plane attributes it nothing.
    assert acceptance.node_sandbox_ids("node_absent") == []
    assert seen == [
        f"{acceptance.API}/internal/fleet/sandboxes",
        f"{acceptance.API}/internal/fleet/sandboxes",
        f"{acceptance.API}/internal/fleet/sandboxes",
    ]
    # The node-scoped endpoint (identity + source IP) is deliberately not used.
    assert all("/internal/nodes/" not in url for url in seen)


def test_the_politeness_lookup_refuses_a_malformed_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything that is not the attributed view is a refusal, not a guess."""
    for payload, message in (
        ([], "fleet sandbox view is not an object"),
        (
            {"sandboxIDs": ["sbx_a"]},
            "fleet sandbox view carries no `sandboxes` attribution",
        ),
        (
            {"sandboxes": []},
            "fleet sandbox view carries no `sandboxes` attribution",
        ),
        (
            {"sandboxes": {"node_a": "sbx_a"}},
            "fleet sandbox view for node node_a is not a list",
        ),
    ):
        monkeypatch.setattr(
            acceptance.httpx,
            "get",
            lambda *args, payload=payload, **kwargs: SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: payload
            ),
        )
        with pytest.raises(ValueError) as excinfo:
            acceptance.node_sandbox_ids("node_a")
        assert str(excinfo.value) == message


def test_a_list_without_our_own_sandbox_is_not_believed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    kubectl = _Kubectl()
    monkeypatch.setattr(acceptance, "kubectl", kubectl)
    monkeypatch.setattr(acceptance, "node_sandbox_ids", lambda node_id: [])

    with pytest.raises(SystemExit) as exited:
        acceptance.delete_worker_pod("e2b-worker-0", "sbx_ours")

    assert exited.value.code == 2
    assert kubectl.calls == []
    assert capsys.readouterr().err == (
        "拒删 worker pod e2b-worker-0：控制面的按节点名单里没有本次验收的沙箱 sbx_ours（名单：空）。\n"
        "名单连自己都不认的时候，「这上面没有别人」这句话不算数——所以不动 pod。\n"
        "出路：确认这条沙箱的记录还在（Sandbox.create 之后 `/internal/routes/<id>` 能查到）再重跑；\n"
        "      确实要不看这张名单就删，用 `--force` 显式承担。\n"
    )


def test_the_force_flag_is_off_unless_asked_for() -> None:
    assert acceptance.parse_args([]).force is False
    assert acceptance.parse_args(["--force"]).force is True
