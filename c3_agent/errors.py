"""The agent's named refusals (C3 Task 2/4).

One class for both faces: a refusal is a *named, fail-closed* answer the
control plane forwards verbatim. It lives in its own module because face A
(:mod:`c3_agent.app`) and face B
(:mod:`c3_agent.fileops`) both raise it, and neither may import the
other's service module to get at it.
"""

from __future__ import annotations


class AgentRefusal(Exception):
    """A named, fail-closed refusal from the agent's privileged operation."""
