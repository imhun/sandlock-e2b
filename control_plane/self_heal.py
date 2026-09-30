"""C3 Task 6: 孤儿回收的决策面 --- agent 巡检 → **CP 决策** → agent 执行.

The agent is the eyes: it mounts the shared workspaces and reports the
sandbox-shaped trees it can see (``c3_agent/scan.py``). This module is
the brain, and it is the *only* place the question "may this tree go?" is
answered -- the agent never decides, and it never acts on its own.

What the decision rests on is exactly what the plan chose option (e) for: the
**records**, which no worker and no agent can forge a way around. A reported
tree is an orphan only when **no record anywhere in the fleet claims its id**;
everything else is ``protected`` -- ``protected_elsewhere``'s semantics, redone
where the authority now lives (C3 §11.1 item 5, §14.5). A lying agent therefore
cannot make the control plane delete a live sandbox's tree; it can only make it
look at orphans that are orphans anyway.

The three legs of the staleness gate, all fail-closed and all named
(``docs/c3-privilege-relocation.md`` §14.5's "⚠ 剩下的依赖性"):

(a) the records must be the **shared** store. A control plane whose records are
    its own memory cannot certify "no record anywhere": after a restart its set
    is empty and every live tree on the shared mount looks ownerless. The sweep
    is **inert** in that shape rather than dangerous;
(b) the store read must have answered **every** entry (``unreadable == 0``).
    The listing path silently skips what it cannot read -- one broken record
    must not take out the TTL sweep -- and a sweep that inherited that habit
    would delete the tree of the one sandbox whose record it could not read;
(c) the id count must match the fleet-wide record count that
    ``GET /internal/fleet/metrics`` reports (Task 4's reviews pinned this
    discipline for the worker's sweep; it is kept here), because the two are
    two *reads* and a record landing between them is a race, not a permission.

A deferred round deletes nothing: it answers with the reason, the agent logs it
and retries on its capped backoff. Convergence is never traded for safety.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from control_plane import file_ops
from control_plane.c3_agent_client import AgentClientError, AgentTarget, C3AgentClient
from control_plane.fleet_view import active_sandbox_count

logger = logging.getLogger(__name__)

#: Leg (a): a process-local record set cannot certify the fleet.
DEFER_NO_SHARED_STORE = (
    "this control plane's records are process-local (no shared record store): "
    "a process-local set cannot certify that no record anywhere claims a tree "
    "-- deferring the sweep"
)


def _defer_unreadable(unreadable: int) -> str:
    """Leg (b): the store held records it would not answer."""
    return (
        f"{unreadable} record(s) in the shared store could not be read: the "
        "fleet view is incomplete -- deferring the sweep"
    )


def _defer_incomplete(enumerated: int, counted: int) -> str:
    """Leg (c): the enumeration did not account for every record."""
    return (
        f"fleet sandbox enumeration is incomplete ({enumerated} of {counted} "
        "records accounted for) -- deferring the sweep"
    )


@dataclass(frozen=True)
class SweepOutcome:
    """One round's decision and what came of it (the agent's greppable answer)."""

    node: str
    scanned: tuple[str, ...]
    protected: tuple[str, ...] = ()
    orphans: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    deferred: str | None = None

    def as_response(self) -> dict[str, Any]:
        """The wire shape the agent logs one line from."""
        return {
            "node": self.node,
            "scanned": len(self.scanned),
            "protected": list(self.protected),
            "orphans": list(self.orphans),
            "removed": list(self.removed),
            "failed": [
                {"sandboxID": sandbox_id, "reason": reason}
                for sandbox_id, reason in self.failed
            ],
            "deferred": self.deferred,
        }


def _deferred(node_id: str, reported: Sequence[str], reason: str) -> SweepOutcome:
    """A round that decided nothing. ``orphans``/``removed`` stay empty on purpose.

    A deferral is not a decision about *some* trees: the whole round is
    unanswered, so saying which ids looked ownerless would only invite a reader
    -- or a later change -- to act on a half-answer.
    """
    logger.warning(
        "c3 self-heal: deferring the sweep for node %s (%d tree(s) reported): %s",
        node_id,
        len(reported),
        reason,
    )
    return SweepOutcome(node=node_id, scanned=tuple(reported), deferred=reason)


def _derive_orphan_removal(
    spec: file_ops.FileOpSpec,
    paths: file_ops.ControlPaths,
    node_id: str,
    sandbox_id: str,
) -> tuple[file_ops.FileOpInstruction | None, str | None]:
    """The removal instruction for one id, or the reason nothing may be sent.

    Runs in a thread (the caller): ``control_paths`` reads the volume registry
    and ``derive`` resolves paths, and both are filesystem work against a shared
    store -- the same reason the file-op endpoint moved its derivation off the
    event loop (a stall here is a stall for every sandbox API on this replica).
    """
    try:
        return (
            file_ops.derive(
                spec,
                {"sandbox_id": sandbox_id},
                paths=paths,
                host_uid=None,
                node_id=node_id,
                worker_gid=None,
            ),
            None,
        )
    except file_ops.FileOpRefusal as exc:
        # The derivation refused this id (a reserved name, a path outside the
        # roots): named, and nothing is sent.
        return None, str(exc)


async def run_sweep(
    state: Any,
    *,
    client: C3AgentClient,
    node_id: str,
    target: AgentTarget,
    reported: Sequence[str],
) -> SweepOutcome:
    """Decide one node's report and instruct that node's agent for each orphan.

    ``node_id`` is the **agent's** own identity (its host, D12) -- the identity
    the report was authenticated against -- and ``target`` is the agent
    address that authentication resolved. Every removal is instructed through
    *that* target, with a path the control plane derived here (never a path the
    agent or a worker named).
    """
    settings = state.settings
    snapshot = state.registry.fleet_id_snapshot()
    if not snapshot.shared:
        return _deferred(node_id, reported, DEFER_NO_SHARED_STORE)
    if snapshot.unreadable:
        return _deferred(node_id, reported, _defer_unreadable(snapshot.unreadable))
    # Leg (c): the *second* read, exactly as the worker's sweep did it. A record
    # created between the two reads is a concurrent create; a record removed
    # between them is a concurrent delete. Either way the view is not one view,
    # so nothing is deleted on it.
    counted = active_sandbox_count(state)
    if len(snapshot.ids) != counted:
        return _deferred(
            node_id, reported, _defer_incomplete(len(snapshot.ids), counted)
        )
    seen = set(reported)
    protected = tuple(sorted(seen & snapshot.ids))
    orphans = tuple(sorted(seen - snapshot.ids))
    if protected:
        # Not an error: it is the shared mount working as designed (every agent
        # sees every node's live trees). Named so an operator reading a quiet
        # node's log can see that the sweep looked and chose to leave them.
        logger.info(
            "c3 self-heal: %d tree(s) reported by node %s are claimed by a "
            "record and are left alone: %s",
            len(protected),
            node_id,
            ",".join(protected),
        )
    paths = await asyncio.to_thread(file_ops.control_paths, state, settings)
    spec = file_ops.spec_for("remove-orphan-workspace", caller="self-heal")
    removed: list[str] = []
    failed: list[tuple[str, str]] = []
    for sandbox_id in orphans:
        instruction, refusal = await asyncio.to_thread(
            _derive_orphan_removal, spec, paths, node_id, sandbox_id
        )
        if instruction is None:
            failed.append((sandbox_id, refusal or "the removal could not be derived"))
            logger.warning(
                "c3 self-heal: refusing to remove orphan %s for node %s: %s",
                sandbox_id,
                node_id,
                refusal,
            )
            continue
        try:
            # The instruction carries **no** worker identity: the removal acts
            # as nobody, and a worker that crashed and never came back has no
            # identity to name (Task 4's identity rule is about steps that act
            # as the worker -- this one does not).
            await client.rm(
                node_id=node_id,
                sandbox_id=sandbox_id,
                path=instruction.path,
                target=target,
            )
        except AgentClientError as exc:
            # Fail-closed: never reported as removed. A second agent racing the
            # same tree lands here too (its ``rm`` meets a path that is gone),
            # which is why the failure is named per id and the next round simply
            # no longer sees the tree.
            failed.append((sandbox_id, str(exc)))
            logger.warning(
                "c3 self-heal: node %s could not remove orphan %s: %s",
                node_id,
                sandbox_id,
                exc,
            )
            continue
        removed.append(sandbox_id)
        logger.warning(
            "c3 self-heal: node %s removed the orphan tree %s (%s)",
            node_id,
            sandbox_id,
            instruction.path,
        )
    logger.info(
        "c3 self-heal summary: node=%s scanned=%d protected=%d orphans=%d "
        "removed=%d failed=%d deferred=[]",
        node_id,
        len(reported),
        len(protected),
        len(orphans),
        len(removed),
        len(failed),
    )
    return SweepOutcome(
        node=node_id,
        scanned=tuple(reported),
        protected=protected,
        orphans=orphans,
        removed=tuple(removed),
        failed=tuple(failed),
    )
