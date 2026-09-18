"""The autoscaler must scale the workload *kind* the manifest actually deploys.

The baseline runs the worker as a StatefulSet (stable pod names = stable node ids,
the N20 fix), while a cluster still on the older Deployment manifests has to keep
working. Getting the kind wrong is not a silent no-op: the collection URL simply
404s on every tick, so the fleet never grows. These pin both paths and the
rejection of a typo.
"""

from __future__ import annotations

import httpx
import pytest

from autoscaler.backends.k8s import KubernetesBackend


def _backend(seen: list[httpx.Request], *, kind: str | None = None) -> KubernetesBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
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
        ("PUT", "/apis/apps/v1/namespaces/sandlock/statefulsets/e2b-worker/scale"),
    ]


def test_the_default_kind_is_the_pre_n20_deployment() -> None:
    """A cluster that has not switched yet must keep scaling what it has."""
    seen: list[httpx.Request] = []
    backend = _backend(seen)

    assert backend.current() == 2
    backend.scale_to(3)

    urls = [(request.method, request.url.path) for request in seen]
    assert urls == [
        ("GET", "/apis/apps/v1/namespaces/sandlock/deployments/e2b-worker"),
        ("PUT", "/apis/apps/v1/namespaces/sandlock/deployments/e2b-worker/scale"),
    ]


def test_an_unknown_kind_is_rejected_at_construction() -> None:
    """A typo must fail loudly here, not 404 once per tick in production."""
    seen: list[httpx.Request] = []
    with pytest.raises(ValueError, match="unsupported workload kind"):
        _backend(seen, kind="statefulsets")
