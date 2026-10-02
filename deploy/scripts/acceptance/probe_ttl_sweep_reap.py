#!/usr/bin/env python3
"""N61: does the TTL sweep reap what its own candidate set lists?

Measured 2026-10-02: 8 records with ``state: running`` whose ``endAt`` was ~65
minutes in the past were never reaped, and ``_ttl_reapable`` has no exemption
for ``running`` -- it collects a record the moment its deadline passes. Either
the candidates are not what the sweep acts on, or the sweep never runs a
round. Only the live store can tell those apart, so this probe reads it and
prints the three answers side by side:

1. every record, with the record's own deadline test (``is_expired``) next to
   the sweep's exemption test (``registry._ttl_reapable``) -- the pair that
   says "listed" vs "reapable";
2. ``expired_candidates()`` and the set differences against ``list()`` -- the
   "listed but not reapable" (and the reverse) shape;
3. ``e2b:ttl:sweep`` sampled 60 times, one second apart: ``EXISTS`` and ``TTL``
   per sample. A claim held for the whole window is a round that never ended
   (or a holder that never released); a claim free the whole window is a
   sweeper that never even asked.

**This probe is read-only.** It only reads records and the claim key; it never
sets, expires or deletes a key and never tears a sandbox down. It needs
``E2B_REDIS_URL`` (the same one the control plane runs with) and refuses by
name, exit code 2, when it is missing rather than reporting a quiet "no
problems" from an empty store it invented.

Importing this module has no side effects -- ``main`` is only called under
``__main__`` -- so the helpers below can be exercised by a unit test.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


def _repo_root() -> Path | None:
    """The checkout root when this runs as a **file**, else ``None``.

    Documented invocation inside the pods is ``python3 - < <this file>``:
    the interpreter starts in ``/app`` (the repo root) and puts the *cwd* on
    ``sys.path``, so nothing is needed. Running it as ``python3 <path>`` puts
    the script's own directory there instead, which is why the repo root is
    added here. Both shapes have to be handled and neither may raise: a piped
    script has no ``__file__`` on 3.12, and ``<stdin>`` has no parents.
    """
    try:
        here = Path(__file__).resolve()  # noqa: F821 - defined when run as a file
    except NameError:
        return None
    if here.name.startswith("<"):  # ``<stdin>``, ``<string>``, ``-c``
        return None
    try:
        return here.parents[3]
    except IndexError:  # pragma: no cover - a path with no repo above it
        return None


_ROOT = _repo_root()
if _ROOT is not None and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRecord, SandboxRegistry
from control_plane.registry.redis_backend import create_redis_client
from gateway_common.timeutil import utcnow

#: The TTL sweep's fleet-wide claim (``control_plane/app.py``).
CLAIM_KEY = "e2b:ttl:sweep"
CLAIM_SAMPLES = 60
CLAIM_SAMPLE_INTERVAL_S = 1.0

#: The sweeper's INFO line truncates at ten ids; the probe matches it.
MAX_IDS_PER_LINE = 10

#: The refusal is *named*: the diagnostic is "the probe did not run", not
#: "the store looked empty".
NO_REDIS_URL = (
    "probe_ttl_sweep_reap: refusing to run: E2B_REDIS_URL is not set -- this "
    "probe reads the live control-plane store (read-only: nothing was read or "
    "written) and cannot say anything about a store it cannot reach"
)


def candidate_summary(record_ids: Iterable[str]) -> str:
    """First ten ids then ``…(+N more)`` -- the sweep's own INFO shape (N61)."""
    ids = list(record_ids)
    if len(ids) > MAX_IDS_PER_LINE:
        tail = len(ids) - MAX_IDS_PER_LINE
        return ", ".join(ids[:MAX_IDS_PER_LINE]) + f"…(+{tail} more)"
    return ", ".join(ids)


def classify(
    records: Iterable[SandboxRecord],
    *,
    now,
    reapable: Callable[[SandboxRecord, Any], bool],
) -> list[dict[str, Any]]:
    """One row per record: the two verdicts the sweep's silence hides.

    ``is_expired`` is the record's own deadline test; ``ttl_reapable`` is the
    sweep's exemption test (paused / orphaned survive their deadline by
    design). ``is_expired and not ttl_reapable`` is a *designed* divergence;
    ``is_expired and ttl_reapable`` is exactly what a round must collect.
    """
    rows = []
    for record in records:
        rows.append(
            {
                "sandbox_id": record.sandbox_id,
                "state": record.state,
                "end_at": record.end_at.isoformat(),
                "now": now.isoformat(),
                "overdue_s": round((now - record.end_at).total_seconds(), 1),
                "is_expired": record.is_expired(now),
                "ttl_reapable": bool(reapable(record, now)),
            }
        )
    return rows


def divergence(
    *, all_ids: Sequence[str], candidate_ids: Sequence[str], reapable_ids: Sequence[str]
) -> dict[str, list[str]]:
    """Where the two listings disagree -- the N61 question in set form."""
    candidates = set(candidate_ids)
    reapable = set(reapable_ids)
    return {
        # Listed by the sweep's own predicate but absent from its result: a
        # record the sweep should be able to collect and does not.
        "reapable_but_not_listed": sorted(reapable - candidates),
        # Returned without the predicate agreeing: a listing and a predicate
        # built from different records (a stale read, a shape mismatch).
        "listed_but_not_reapable": sorted(candidates - reapable),
        # Neither expired nor reapable: not overdue, or exempt on purpose.
        "not_reapable_this_round": sorted(set(all_ids) - reapable),
    }


def sample_claim(
    client,
    key: str,
    *,
    samples: int,
    interval_s: float,
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[int, int]]:
    """``(EXISTS, TTL)`` per sample. Read-only: only ``EXISTS`` and ``TTL``.

    ``TTL`` is ``-2`` when the key is gone and ``-1`` when it has no expiry,
    so ``EXISTS=0`` is the only "free" reading -- the pair also catches a
    claim key that exists *without* a TTL, which would be held forever.
    """
    out: list[tuple[int, int]] = []
    for _ in range(samples):
        out.append((int(client.exists(key)), int(client.ttl(key))))
        sleep(interval_s)
    return out


def _print_rows(rows: Sequence[dict[str, Any]]) -> None:
    for row in rows:
        print(
            "  sandbox_id={sandbox_id} state={state} end_at={end_at} "
            "now={now} overdue_s={overdue_s} is_expired={is_expired} "
            "ttl_reapable={ttl_reapable}".format(**row)
        )


def _parse_overrides(argv: Sequence[str]) -> tuple[int, float]:
    samples, interval = CLAIM_SAMPLES, CLAIM_SAMPLE_INTERVAL_S
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--samples" and rest:
            samples = int(rest.pop(0))
        elif flag == "--interval" and rest:
            interval = float(rest.pop(0))
        else:
            raise SystemExit(f"probe_ttl_sweep_reap: unknown argument {flag!r}")
    return samples, interval


def main(argv: Sequence[str] | None = None) -> int:
    """Print the read-only report; ``2`` when the store cannot be reached."""
    samples, interval_s = _parse_overrides(list(sys.argv[1:] if argv is None else argv))
    if not os.getenv("E2B_REDIS_URL"):
        print(NO_REDIS_URL, file=sys.stderr)
        return 2

    settings = Settings()
    client = create_redis_client(settings.redis_url)
    if client is None:
        print(
            "probe_ttl_sweep_reap: refusing to run: E2B_REDIS_URL is set but no "
            "Redis client could be built (is the `redis` package installed?)",
            file=sys.stderr,
        )
        return 2

    print(
        "probe_ttl_sweep_reap: READ-ONLY -- reads the shared records and the "
        "claim key; no SET/EXPIRE/DEL, no teardown, no quota change."
    )
    registry = SandboxRegistry(settings, redis_client=client)
    now = utcnow()

    records = registry.list()
    rows = classify(records, now=now, reapable=registry._ttl_reapable)
    print(f"\n-- records ({len(rows)}) --")
    _print_rows(rows)

    candidates = registry.expired_candidates(now)
    candidate_ids = [record.sandbox_id for record in candidates]
    reapable_ids = [row["sandbox_id"] for row in rows if row["ttl_reapable"]]
    print("\n-- candidate set --")
    print(
        f"  expired_candidates() = {len(candidate_ids)}: "
        f"{candidate_summary(candidate_ids)}"
    )
    print(
        f"  _ttl_reapable records = {len(reapable_ids)}: "
        f"{candidate_summary(reapable_ids)}"
    )
    for name, ids in divergence(
        all_ids=[row["sandbox_id"] for row in rows],
        candidate_ids=candidate_ids,
        reapable_ids=reapable_ids,
    ).items():
        print(f"  {name} = {len(ids)}: {candidate_summary(ids)}")

    print(f"\n-- claim {CLAIM_KEY}, {samples} x {interval_s}s (EXISTS / TTL) --")
    claim_samples = sample_claim(
        client, CLAIM_KEY, samples=samples, interval_s=interval_s
    )
    for index, (exists, ttl) in enumerate(claim_samples, start=1):
        print(f"  sample {index}: EXISTS={exists} TTL={ttl}")
    held = sum(1 for exists, _ in claim_samples if exists)
    print(
        f"  held in {held}/{len(claim_samples)} samples; "
        "all-held = a round that never ended (or a claim never released); "
        "never-held = the sweeper never even asked"
    )
    print(
        "\nprobe_ttl_sweep_reap: done; nothing was written (read-only by "
        "construction: the store's own KEYS/GET reads plus EXISTS/TTL only)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - the probe runs on the cluster
    raise SystemExit(main())
