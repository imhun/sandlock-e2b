"""N25: the executor's two new seams, called the way the app calls them.

Both are one-line delegations, and both are the kind that fail *silently* when
they break: `drain_dirty_dirs` returning `None` means "walk the whole tree"
instead of "the ledger answered", so an accounting that stops using its ledger
looks exactly like an accounting that never had one -- only slower. That is
worth pinning by name.
"""

from __future__ import annotations

import pytest

from envd_service.executors.sandlock import SandlockExecutor


class _Instance:
    def __init__(self, *, dirs=("/tree/a",), overflow=False, raises=None):
        self._dirs = list(dirs)
        self._overflow = overflow
        self._raises = raises
        self.tightened: list[int] = []

    def drain_dirty_dirs(self):
        if self._raises is not None:
            raise self._raises
        return self._dirs, self._overflow

    def set_file_size_limit(self, bytes_):
        if self._raises is not None:
            raise self._raises
        self.tightened.append(int(bytes_))
        return {"applied_bytes": int(bytes_), "tightened": 1}


def _executor(instance) -> SandlockExecutor:
    """The real methods, bound to the smallest object they need.

    The constructor decides own-identity shape, allocates uid pools and touches the
    filesystem; none of that is what these tests are about, and a stub keeps
    them honest about which object's behaviour is under test.
    """
    stub = SandlockExecutor.__new__(SandlockExecutor)
    stub._instance = instance
    stub._sandbox_id = "sbx_stub"
    stub._append_sink = None
    return stub


def test_drain_dirty_dirs_returns_what_the_instance_said():
    instance = _Instance(dirs=("/tree/a", "/tree/b"), overflow=True)
    assert _executor(instance).drain_dirty_dirs() == (["/tree/a", "/tree/b"], True)


def test_drain_dirty_dirs_is_none_without_an_instance():
    assert _executor(None).drain_dirty_dirs() is None


def test_drain_dirty_dirs_turns_a_failure_into_a_capability_answer():
    """An older slot, a dead generation: "cannot answer", never an exception.

    The accounting treats `None` as "walk the tree", which is the safe answer;
    raising here would take a scan round down instead.
    """
    instance = _Instance(raises=RuntimeError("slot gone"))
    assert _executor(instance).drain_dirty_dirs() is None


def test_set_file_size_limit_delegates():
    instance = _Instance()
    applied = _executor(instance).set_file_size_limit(4096)
    assert instance.tightened == [4096]
    assert applied == {"applied_bytes": 4096, "tightened": 1}


def test_set_file_size_limit_is_none_without_an_instance():
    assert _executor(None).set_file_size_limit(4096) is None


def test_set_file_size_limit_survives_a_refusal():
    instance = _Instance(raises=RuntimeError("wider than the ceiling"))
    assert _executor(instance).set_file_size_limit(4096) is None


def test_the_append_sink_only_sees_append_events():
    executor = _executor(_Instance())
    seen: list[int] = []
    executor.set_append_sink(seen.append)

    executor._on_slot_event({"v": 1, "event": "append", "bytes": 4096})
    executor._on_slot_event({"v": 1, "event": "something-else", "bytes": 1})
    executor._on_slot_event({"event": "append", "bytes": "12"})
    executor._on_slot_event({"event": "append"})
    executor._on_slot_event({"event": "append", "bytes": 0})

    assert seen == [4096, 12]


def test_an_event_with_no_sink_is_dropped_not_raised():
    executor = _executor(_Instance())
    # No `set_append_sink` call: a slot that pushes before the wiring is a
    # race, not a fault, and it must not kill the pump thread.
    executor._on_slot_event({"event": "append", "bytes": 4096})


def test_an_unparsable_byte_count_is_ignored():
    executor = _executor(_Instance())
    seen: list[int] = []
    executor.set_append_sink(seen.append)
    executor._on_slot_event({"event": "append", "bytes": "not-a-number"})
    assert seen == []


def test_a_sink_failure_is_left_to_the_pump_that_owns_the_thread():
    executor = _executor(_Instance())

    def explode(bytes_):
        raise RuntimeError("accounting hiccup")

    executor.set_append_sink(explode)
    with pytest.raises(RuntimeError):
        # The *pump* catches consumer failures (`OwnIdentityInstance.start_event_pump`);
        # this seam is deliberately transparent so that protection stays in one
        # place rather than being half-implemented twice.
        executor._on_slot_event({"event": "append", "bytes": 1})
