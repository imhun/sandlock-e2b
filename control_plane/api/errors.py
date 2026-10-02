"""Official error type for the control plane."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

#: Task 3's named refusal. Once the sandbox trees are node-local, the export
#: endpoint lives **on the source node** -- so a source that does not answer
#: means the tree is unreachable *and* cannot be moved afterwards. That is a
#: different fact from "some node was unavailable" and an operator has to be
#: able to see it by name (the drain order that avoids the state is in
#: ``docs/create-local-first-design.md`` §8: move the sandbox off a node
#: *before* taking the node down).
SOURCE_NODE_UNREACHABLE = "source-node-unreachable"

#: Task 3's other named state: the record names a node that has no tree for
#: the sandbox. The repair action is named in the same document
#: (``retire-stale-tree-record``); this is the string an operator greps for in
#: the control plane's answer.
TREE_MISSING_ON_RECORDED_NODE = "tree-missing-on-recorded-node"


class OfficialError(Exception):
    """An error carrying the official ``{"code": int, "message": str}`` body."""

    def __init__(
        self,
        code: int,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # E9.3: optional response headers (e.g. x-e2b-eviction-reason on the
        # evicted-sandbox 404). ``None`` keeps every existing call site byte
        # for byte identical to before.
        self.headers = dict(headers) if headers else None


def official_error_handler(_: Request, exc: OfficialError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.code,
        content={"code": exc.code, "message": exc.message},
        headers=exc.headers,
    )


def source_node_unreachable(node_id: str, detail: object) -> OfficialError:
    """The one error name every "the source node's tree is not reachable" path uses."""
    return OfficialError(
        502,
        f"{SOURCE_NODE_UNREACHABLE}: node {node_id} is not answering, so the "
        f"sandbox tree it holds cannot be reached or moved ({detail})",
    )
