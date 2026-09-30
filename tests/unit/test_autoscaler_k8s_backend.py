"""The autoscaler scales the worker StatefulSet -- the only thing this repo deploys.

It used to take an ``E2B_AS_K8S_KIND`` knob so a cluster still on the pre-N20
Deployment manifests kept working; that knob went on 2026-09-30 (open-issues
N52), together with the Deployment branch. The worker has been a StatefulSet
since N20 -- stable pod names are stable node ids -- so there is one kind to get
right, and getting it wrong is not a silent no-op: the URL 404s on every tick
and the fleet never grows.

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
from autoscaler.backends.k8s import KubernetesBackend


def _pod(name: str, *, owner: str = "e2b-worker", kind: str = "StatefulSet") -> dict:
    """One pod as the API answers it, owned by the workload under test."""
    return {
        "metadata": {
            "name": name,
            "ownerReferences": [{"kind": kind, "name": owner}],
        }
    }


def _backend(seen: list[httpx.Request], *, pods: tuple[dict, ...] = ()) -> KubernetesBackend:
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
    return KubernetesBackend(
        namespace="sandlock", deployment="e2b-worker", client=client
    )


def test_it_reads_and_scales_the_worker_statefulset() -> None:
    seen: list[httpx.Request] = []
    backend = _backend(seen)

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


def test_the_pre_n20_deployment_path_is_gone() -> None:
    """No kind argument, and no ``deployments`` URL to drift back to (N52)."""

    seen: list[httpx.Request] = []
    backend = _backend(seen)
    backend.scale_to(3)

    paths = [request.url.path for request in seen]
    assert paths == [
        "/apis/apps/v1/namespaces/sandlock/statefulsets/e2b-worker/scale",
    ]
    assert not [path for path in paths if "/deployments" in path]


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
        pods=(_pod("e2b-worker-0"), _pod("e2b-worker-1"), _pod("e2b-worker-2")),
    )

    assert backend.retire_victim(["e2b-worker-1"]) is None
    assert backend.retire_victim(["e2b-worker-0", "e2b-worker-1"]) is None


def test_pods_owned_by_another_workload_are_not_the_victim() -> None:
    """The list is the namespace's; only this workload's pods count."""
    seen: list[httpx.Request] = []
    backend = _backend(
        seen,
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
    backend = _backend(seen, pods=())

    assert backend.retire_victim(["e2b-worker-0"]) is None


def test_no_candidates_is_no_victim() -> None:
    seen: list[httpx.Request] = []
    assert _backend(seen).retire_victim([]) is None
