"""``gateway_common.create_trace``: the create path's stage switch.

The switch exists so a create's stages can be measured **on the live fleet**
rather than argued about from the code -- each NFS op on the deployment's NAS
costs a few to tens of milliseconds and the mix moves with the NAS's load, so
an estimate from reading the call graph is not evidence.

What this lane pins is the switch's own contract, because everything else
depends on it being trustworthy:

* off (the default, and every spelling of "no") logs **nothing** -- a trace
  left on by accident must not become the worker's steady-state log volume;
* on logs one line per stage, naming the stage and the sandbox, with a
  duration that is a real measurement of the block (asserted against a
  synthetic block of known length, not just "some number").
"""

from __future__ import annotations

import logging
import time

import pytest

from gateway_common import create_trace

LOGGER = "gateway_common.create_trace"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "OFF"])
def test_every_spelling_of_no_keeps_the_trace_silent(monkeypatch, caplog, value):
    monkeypatch.setenv(create_trace.TRACE_ENV, value)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        create_trace.stage("record", "sbx_deadbeef", time.monotonic())
    assert caplog.text == ""


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_every_spelling_of_yes_turns_it_on(monkeypatch, caplog, value):
    monkeypatch.setenv(create_trace.TRACE_ENV, value)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        create_trace.stage("record", "sbx_deadbeef", time.monotonic())
    assert caplog.text != ""


def test_an_enabled_stage_names_itself_the_sandbox_and_a_real_duration(
    monkeypatch, caplog
):
    monkeypatch.setenv(create_trace.TRACE_ENV, "1")
    started = time.monotonic()
    time.sleep(0.05)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        create_trace.stage("fileop:chown-workspace", "sbx_0123456789abcdef", started)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert message.startswith(
        "create trace: stage=fileop:chown-workspace sandbox=sbx_0123456789abcdef ms="
    )
    measured = float(message.rsplit("ms=", 1)[1])
    # The number is the block's own duration, not a constant and not a
    # timestamp: a 50 ms sleep has to read as at least 50 ms.
    assert measured >= 50.0


def test_a_stage_without_a_sandbox_says_so_instead_of_printing_none(
    monkeypatch, caplog
):
    monkeypatch.setenv(create_trace.TRACE_ENV, "1")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        create_trace.stage("total", None, time.monotonic())
    assert "sandbox=-" in caplog.records[0].getMessage()
