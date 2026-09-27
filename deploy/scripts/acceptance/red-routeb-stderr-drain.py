"""RED/GREEN check for the slot-stderr drain (N35 side quest).

Same writer shape as tests/unit/test_route_b_wiring.py::
test_a_talkative_slot_stderr_cannot_wedge_its_writer, run twice:

* RED   -- the pre-fix shape (a ``SlotStderrDrain`` that never reads). The
           payload is 102 400 bytes into a 65 536-byte pipe, so the writer
           must wedge: that is what makes the real test's
           ``proc.wait(...) == 0`` an assertion instead of a tautology.
* GREEN -- the shipped drain: the same writer exits 0 and the tail is kept.

    sh deploy/scripts/acceptance/n35-lane.sh python3 -u deploy/scripts/acceptance/red-routeb-stderr-drain.py
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import threading

sys.path.insert(0, "/workspace")

import envd_service.route_b as rb

LINE = "x" * 255  # 255 + "\n" = 256 bytes, so the 8 KiB tail is 32 lines
LINES = 400  # 102 400 bytes, past the 65 536-byte pipe
SCRIPT = (
    "i=0; while [ $i -lt %d ]; do printf '%%s\\n' %s >&2; i=$((i+1)); done"
    % (LINES, LINE)
)
EXPECTED_TAIL = ("x" * 255 + "\n") * 31 + "x" * 255


class NoDrain(rb.SlotStderrDrain):
    """The pre-fix behaviour: the pipe exists, nobody reads it."""

    def __init__(self, process) -> None:  # noqa: ARG002 - mutant on purpose
        self._lock = threading.Lock()
        self._tail = bytearray()
        self._fd = None
        self._thread = None


def _writer() -> subprocess.Popen:
    return subprocess.Popen(
        ["/bin/sh", "-c", SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )


def _mutant() -> bool:
    rb.SlotStderrDrain = NoDrain
    proc = _writer()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print("RED ok: without the drain the writer is wedged (5 s, no exit)")
        return True
    finally:
        proc.kill()
        proc.wait(timeout=5)
    print("RED FAILED: the writer exited with nobody reading the pipe")
    return False


def _shipped() -> bool:
    rb = importlib.reload(importlib.import_module("envd_service.route_b"))
    proc = _writer()
    drain = rb.SlotStderrDrain(proc)
    try:
        code = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        print("GREEN FAILED: the shipped drain did not unblock the writer")
        proc.kill()
        proc.wait(timeout=5)
        return False
    drain.join(5)
    kept = drain.text(limit=10**6)
    print(f"GREEN ok: writer exit={code}, tail={len(kept)} chars")
    if kept != EXPECTED_TAIL:
        print("GREEN FAILED: the tail is not the last 8 KiB of the stream")
        return False
    return True


if __name__ == "__main__":
    sys.exit(0 if (_mutant() and _shipped()) else 1)
