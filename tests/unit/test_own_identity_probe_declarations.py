"""Task 13 item 3: a hand-built pool probe must declare the box it builds.

``W1SlotPool.acquire`` treats an undeclared ``memory_mb``/``max_processes`` as
"the caller did not say", and the cgroup module then writes the per-sandbox
**ceiling** -- the deployment's ``E2B_MAX_SANDBOX_*``, 4096 MiB / 1024 tasks
on the k0s overlay. Two hand-built pools are read as *probes* rather than as
unit doubles:

* ``deploy/scripts/acceptance/rb_token_probe.py`` -- what leaks when the
  channel token travels in ``supervise``'s argv (run on a live lane);
* ``tests/contract/test_own_identity_slot_pool.py`` -- the two-uid
  sticky-directory case (run in the privileged lane),

and neither builds its pool with a cgroup handle, so nothing in either file
applies a quota at all. A reader who saw ``acquire(...)`` with no sizes would
reasonably assume the default box -- which, since Task 3, is the ceiling's
box: the biggest one the node sells. These cases are about argv tokens, sticky
directories and uid reuse, not about sizes, so they declare exactly what the
undeclared case resolves to, and this pin keeps them saying it: every
``acquire`` call in those two files must carry both numbers as literals, and a
new call that declares nothing (or only half) fails here.

This is a source pin, deliberately: the values are only a *declaration* in
both files (no cgroup handle is wired into either pool), so there is no
behaviour to observe -- what has to hold is that the call sites stay honest
about the box they ask for. A declared size is read either as an integer
literal or as a module-level integer constant, so a probe may name its own
numbers (``memory_mb=DECLARED_MEMORY_MB``) and still be pinned to the value
the deployment actually sells. Other hand-built pools in the unit suites
(``tests/unit/test_own_identity_wiring.py`` and friends) are doubles for this
module's own wiring, and are not part of this batch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The two probes whose calls this pin covers, spelled out so the coverage
#: cannot silently shrink to zero files.
_PROBE_FILES = (
    _REPO_ROOT / "deploy/scripts/acceptance/rb_token_probe.py",
    _REPO_ROOT / "tests/contract/test_own_identity_slot_pool.py",
)

#: The k0s ceiling -- ``E2B_MAX_SANDBOX_MEMORY_MB`` / ``_PROCESSES`` on that
#: overlay, and what an undeclared call used to resolve to silently.
_CEILING_MEMORY_MB = 4096
_CEILING_MAX_PROCESSES = 1024


def _acquire_calls(path: Path) -> list[ast.Call]:
    """Every ``<something>.acquire(...)`` call in one source file."""
    return _acquire_calls_in(_parse(path))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _acquire_calls_in(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "acquire"
    ]


def _module_int_constants(tree: ast.Module) -> dict[str, int]:
    """The module-level ``NAME = <int literal>`` assignments, by name."""
    constants: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not _is_int_literal(value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _is_int_literal(node: ast.expr | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    )


def _resolved_int(node: ast.expr | None, constants: dict[str, int]) -> int | None:
    """The integer a keyword's value stands for: a literal, or a named constant."""
    if _is_int_literal(node):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


@pytest.mark.parametrize("path", _PROBE_FILES, ids=lambda path: path.name)
def test_every_hand_built_pool_acquire_declares_its_box_sizes(path: Path) -> None:
    tree = _parse(path)
    constants = _module_int_constants(tree)
    calls = _acquire_calls_in(tree)
    # The pin must not pass by covering nothing: both files acquire at least
    # once, and the acceptance probe acquires exactly once.
    assert len(calls) >= 1
    for call in calls:
        keywords = {
            keyword.arg: keyword.value
            for keyword in call.keywords
            if keyword.arg is not None
        }
        assert {
            name: _resolved_int(keywords.get(name), constants)
            for name in ("memory_mb", "max_processes")
        } == {
            "memory_mb": _CEILING_MEMORY_MB,
            "max_processes": _CEILING_MAX_PROCESSES,
        }


def test_the_acceptance_probe_acquires_exactly_once() -> None:
    """One probe, one box: the count above is the whole call site."""
    assert len(_acquire_calls(_PROBE_FILES[0])) == 1
