"""Test helper: a control-plane app whose *fleet record view* is short.

D6 (`docs/open-issues.md` N49, the C3 Task 2 fix) moved the worker's fleet
enumeration onto a fleet-scope endpoint, so it no longer comes up short because
some registered node is unresolvable. The M1 discipline it feeds -- "if the
fleet's record set cannot be established, skip the sweep and schedule a retry"
-- still guards the remaining cause: a record created *after* the list was read
(a concurrent create, or a replica that had not caught up), where the metrics
count and the list disagree.

These lanes synthesise exactly that inconsistency instead of a node-row one:
the wrapper answers ``/internal/fleet/sandboxes`` with the ids the test says the
list carried, and delegates everything else to the real app (so the metrics
count, the reconcile POST and the per-node endpoints stay real).
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse


class ShortFleetView:
    """ASGI wrapper: canned ``/internal/fleet/sandboxes``, real everything else."""

    def __init__(self, app: Any, sandbox_ids: list[str]) -> None:
        self._app = app
        #: Mutable on purpose: a lane can make the view complete again mid-run
        #: ("the missing record has since been seen"), which is how the retry
        #: tests reach the recovered state deterministically.
        self.sandbox_ids = list(sandbox_ids)

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("path") == "/internal/fleet/sandboxes"
        ):
            response = JSONResponse({"sandboxIDs": list(self.sandbox_ids)})
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)
