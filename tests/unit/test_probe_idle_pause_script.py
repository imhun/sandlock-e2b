"""The idle->pause acceptance probe's decision points, pinned without a cluster.

The probe's live check is "wait for ``paused``, then resume" -- a run that
takes minutes and needs the cluster. What can be pinned here is the part that
decides the verdict: the poll loop's samples and deadline, the defaults that
have to outlive the shipped 300 s threshold, and the named refusal when the
credential is missing (exit 2, never a quiet "no problems").
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROBE = REPO_ROOT / "deploy" / "scripts" / "acceptance" / "probe_idle_pause.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_idle_pause", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE_MODULE = _load_probe()


def _fake_clock():
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return clock, sleep


def test_wait_for_state_returns_every_sample_it_saw():
    states = iter(["running", "paused"])
    clock, sleep = _fake_clock()

    reached, samples = PROBE_MODULE.wait_for_state(
        lambda: {"state": next(states)},
        "paused",
        wait_s=10,
        poll_s=1,
        clock=clock,
        sleep=sleep,
    )

    assert reached is True
    assert samples == [(0.0, "running"), (1.0, "paused")]


def test_wait_for_state_gives_up_at_the_deadline():
    clock, sleep = _fake_clock()

    reached, samples = PROBE_MODULE.wait_for_state(
        lambda: {"state": "running"},
        "paused",
        wait_s=3,
        poll_s=1,
        clock=clock,
        sleep=sleep,
    )

    assert reached is False
    assert samples == [
        (0.0, "running"),
        (1.0, "running"),
        (2.0, "running"),
        (3.0, "running"),
    ]


def test_the_defaults_outlive_the_shipped_threshold():
    args = PROBE_MODULE.parse_args([])

    assert (args.wait_s, args.poll_s, args.timeout) == (420.0, 5.0, 900)


def test_the_probe_refuses_by_name_without_an_api_key(monkeypatch, capsys):
    monkeypatch.delenv("E2B_API_KEY", raising=False)

    assert PROBE_MODULE.main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "probe_idle_pause: refusing to run -- E2B_API_KEY is not set "
        "(it is the only credential this probe uses)\n"
    )
