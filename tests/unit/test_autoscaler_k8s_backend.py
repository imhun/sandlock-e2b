"""The autoscaler must scale the workload *kind* the manifest actually deploys.

The baseline runs the worker as a StatefulSet (stable pod names = stable node ids,
the N20 fix), while a cluster still on the older Deployment manifests has to keep
working. Getting the kind wrong is not a silent no-op: the collection URL simply
404s on every tick, so the fleet never grows. These pin both paths and the
rejection of a typo.

The *verb* is pinned here too, and for the same reason (measured on k0s,
2026-09-30, when the merged autoscaler first tried to grow a real fleet): a
``PUT`` against the scale subresource with only ``{"spec": {"replicas": N}}``
is answered ``400 BadRequest`` -- *"the name of the object (e2b-worker based on
URL) was undeterminable: name must be provided"*. The replace form wants the
object to carry its own ``metadata.name``; a merge patch does not, because the
URL names it. The failure mode is quiet in the worst way: the loop logged
``autoscaler tick failed`` once per interval and the fleet never grew (the
warm-pool floor is only enforced when it is *below* the current count, so an
idle 2/2 fleet hid it for as long as nobody raised the floor).
"""

from __future__ import annotations

import json

import httpx
import pytest

from autoscaler.backends.k8s import KubernetesBackend


def _pod(name: str, *, owner: str = "e2b-worker", kind: str = "StatefulSet") -> dict:
    """One pod as the API answers it, owned by the workload under test."""
    return {
        "metadata": {
            "name": name,
            "ownerReferences": [{"kind": kind, "name": owner}],
        }
    }


def _backend(
    seen: list[httpx.Request],
    *,
    kind: str | None = None,
    pods: tuple[dict, ...] = (),
) -> KubernetesBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": list(pods)})
        if request.method == "GET":
            return httpx.Response(200, json={"spec": {"replicas": 2}})
        return httpx.Response(200, json={})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://kube.test"
    )
    kwargs = {} if kind is None else {"kind": kind}
    return KubernetesBackend(
        namespace="sandlock", deployment="e2b-worker", client=client, **kwargs
    )


def test_statefulset_kind_reads_and_scales_the_statefulset() -> None:
    seen: list[httpx.Request] = []
    backend = _backend(seen, kind="statefulset")

    assert backend.current() == 2
    backend.scale_to(4)

    urls = [(request.method, request.url.path) for request in seen]
    assert urls == [
        ("GET", "/apis/apps/v1/namespaces/sandlock/statefulsets/e2b-worker"),
        ("PATCH", "/apis/apps/v1/namespaces/sandlock/statefulsets/e2b-worker/scale"),
    ]
    scale = seen[-1]
    assert scale.headers["content-type"] == "application/merge-patch+json"
    # No `metadata`: the URL names the object, which is exactly what the
    # replace form could not do (see the module docstring).
    assert json.loads(scale.content) == {"spec": {"replicas": 4}}


def test_the_default_kind_is_the_pre_n20_deployment() -> None:
    """A cluster that has not switched yet must keep scaling what it has."""
    seen: list[httpx.Request] = []
    backend = _backend(seen)

    assert backend.current() == 2
    backend.scale_to(3)

    urls = [(request.method, request.url.path) for request in seen]
    assert urls == [
        ("GET", "/apis/apps/v1/namespaces/sandlock/deployments/e2b-worker"),
        ("PATCH", "/apis/apps/v1/namespaces/sandlock/deployments/e2b-worker/scale"),
    ]
    assert seen[-1].headers["content-type"] == "application/merge-patch+json"


def test_an_unknown_kind_is_rejected_at_construction() -> None:
    """A typo must fail loudly here, not 404 once per tick in production."""
    seen: list[httpx.Request] = []
    with pytest.raises(ValueError, match="unsupported workload kind"):
        _backend(seen, kind="statefulsets")


# --------------------------------------------------------------------------
# N51: which pod a scale-down is actually allowed to take.
#
# `remove_node` annotates the drained pod with `pod-deletion-cost` and then
# shrinks the workload by one. The ReplicaSet controller reads that annotation;
# the *StatefulSet* controller does not (it always deletes the highest
# ordinal). So on the baseline kind, "the node I drained" and "the pod that
# died" are only the same pod when the drained one happens to be the top -- and
# when they differ, the fleet loses a pod the loop never chose, which may be
# holding live sandboxes.
# --------------------------------------------------------------------------


def test_a_statefulset_can_only_retire_its_highest_ordinal() -> None:
    """The victim is the top ordinal, even when the loop preferred another."""
    seen: list[httpx.Request] = []
    backend = _backend(
        seen,
        kind="statefulset",
        pods=(_pod("e2b-worker-0"), _pod("e2b-worker-1"), _pod("e2b-worker-2")),
    )

    assert backend.retire_victim(["e2b-worker-0", "e2b-worker-2"]) == "e2b-worker-2"


def test_a_statefulset_refuses_when_the_top_ordinal_is_not_a_candidate() -> None:
    """Refusing here is the fix, not a nuisance.

    With pods 0/1/2 alive and only `e2b-worker-1` idle, a scale-down deletes
    `e2b-worker-2` -- a pod the loop did not choose, possibly running live
    sandboxes. The backend answers "not this tick", and the loop waits for the
    fleet to become shrinkable instead.
    """
    seen: list[httpx.Request] = []
    backend = _backend(
        seen,
        kind="statefulset",
        pods=(_pod("e2b-worker-0"), _pod("e2b-worker-1"), _pod("e2b-worker-2")),
    )

    assert backend.retire_victim(["e2b-worker-1"]) is None
    assert backend.retire_victim(["e2b-worker-0", "e2b-worker-1"]) is None


def test_a_deployment_retires_the_node_the_loop_chose() -> None:
    """The other kind keeps the old contract, and pays nothing for this guard."""
    seen: list[httpx.Request] = []
    backend = _backend(seen, kind="deployment")

    assert backend.retire_victim(["e2b-worker-1", "e2b-worker-0"]) == "e2b-worker-1"
    # No pod list: a ReplicaSet honours the deletion cost, so the loop's own
    # choice is the one that goes.
    assert [request.url.path for request in seen] == []


def test_pods_owned_by_another_workload_are_not_the_victim() -> None:
    """The list is the namespace's; only this workload's pods count."""
    seen: list[httpx.Request] = []
    backend = _backend(
        seen,
        kind="statefulset",
        pods=(
            _pod("e2b-worker-0"),
            _pod("somebody-else-9", owner="somebody-else"),
            _pod("not-a-statefulset-3", kind="Deployment"),
        ),
    )

    assert backend.retire_victim(["e2b-worker-0"]) == "e2b-worker-0"


def test_an_unreadable_pod_list_refuses_instead_of_guessing() -> None:
    """No pods (or an error) means "I cannot say what a scale-down would take"."""
    seen: list[httpx.Request] = []
    backend = _backend(seen, kind="statefulset", pods=())

    assert backend.retire_victim(["e2b-worker-0"]) is None


def test_no_candidates_is_no_victim() -> None:
    seen: list[httpx.Request] = []
    assert _backend(seen, kind="statefulset").retire_victim([]) is None
