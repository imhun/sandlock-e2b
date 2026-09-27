"""N28 acceptance on the live fleet: A (pause gates), B (one writer), C, D.

Run against the deployed entry point. Every claim below is measured, not
inferred, and each check prints the evidence it is based on so a failure can be
read without re-running anything.
"""
import os
import time

import httpx
from e2b import Sandbox, SandboxException

API = os.environ["E2B_API_URL"]
KEY = os.environ["E2B_API_KEY"]
INTERNAL = os.environ["E2B_INTERNAL_API_KEY"]

MIB = 1024 * 1024
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def refusal(fn):
    """Run ``fn`` and return the exception text, or ``None`` if it succeeded."""
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - the point of the probe
        return f"{type(exc).__name__}: {exc}"
    return None


sb = Sandbox.create(timeout=1800)
try:
    print(f"sandbox {sb.sandbox_id}")

    # ---------------------------------------------------------------- B: identity
    print("\nB. the sandbox is the only writer")
    sb.files.write("/identity.txt", "hello")
    # Who created an entry is readable off its gid: the tree is not setgid, so a
    # new file carries its creator's gid -- the sandbox's own (10000) or the
    # worker's (65534). Comparing an *upload* against the same bytes written by
    # a sandbox command is the whole assertion: one writer identity.
    sb.commands.run("sh -c 'echo hello > /home/user/by-command.txt'")
    measured = sb.commands.run(
        "echo uploaded=$(stat -c %u:%g /home/user/identity.txt); "
        "echo by_command=$(stat -c %u:%g /home/user/by-command.txt); "
        "echo tree_root=$(stat -c %u:%g /home/user)"
    ).stdout
    uids = dict(
        line.split("=") for line in measured.strip().splitlines() if "=" in line
    )
    check(
        "an upload is written by the same identity as a sandbox command",
        uids.get("uploaded") == uids.get("by_command"),
        f"uploaded={uids.get('uploaded')} by_command={uids.get('by_command')}",
    )
    check(
        "and that identity is the sandbox's, not the worker's",
        uids.get("uploaded") == "10000:10000",
        f"worker-created entries in this tree carry {uids.get('tree_root')}",
    )

    # A user command holds the per-sandbox command gate (concurrency 1). A
    # worker-side write used to be gated by nothing; a write that took the gate
    # would sit in its queue and 429 after 30s.
    held = sb.commands.run("sleep 25", background=True)
    started = time.perf_counter()
    sb.files.write("/during.txt", "x")
    took = time.perf_counter() - started
    check("a write does not queue behind a running command", took < 5, f"{took:.2f}s")
    held.kill()

    # ---------------------------------------------------------------- A: gating
    print("\nA. a paused sandbox refuses writes and commands, and still reads")
    assert sb.pause() is True
    time.sleep(1)
    write_refusal = refusal(lambda: sb.files.write("/blocked.txt", "x"))
    check(
        "an upload is refused as a state problem",
        write_refusal is not None and write_refusal.startswith("SandboxException: 409: "),
        write_refusal or "the write went through",
    )
    command_refusal = refusal(lambda: sb.commands.run("echo hi"))
    check(
        "a new command is refused as a state problem",
        command_refusal is not None and "FAILED_PRECONDITION" in command_refusal,
        command_refusal or "the command ran",
    )
    check("a read still works", sb.files.read("/identity.txt") == "hello")
    check(
        "the listing still works",
        any(e.path.endswith("identity.txt") for e in sb.files.list("/", depth=1)),
    )
    Sandbox.connect(
        sb.sandbox_id, api_url=API, sandbox_url=API, api_key=KEY
    )
    time.sleep(1)
    sb.files.write("/after-resume.txt", "y")
    check(
        "resuming lets the same write through",
        sb.files.read("/after-resume.txt") == "y",
    )

    # ---------------------------------------------------------------- C: one file
    print("\nC. a single file cannot cross the sold budget (RLIMIT_FSIZE)")
    # Everything in ONE command: the enforcer's pause can land on any later
    # command, and a race here would be measuring the pause, not the limit.
    out = sb.commands.run(
        "dd if=/dev/zero of=/home/user/huge.bin bs=1M count=1200 "
        "2>/home/user/dd.err; echo rc=$?; "
        "grep -m1 'File too large' /home/user/dd.err; "
        "stat -c 'size=%s' /home/user/huge.bin; echo alive; "
        "python3 -c \"import os,shutil;os.remove('/home/user/huge.bin')\"",
        timeout=600,
    )
    text = out.stdout
    check(
        "dd fails on the file, not on the sandbox",
        "rc=1" in text and "File too large" in text,
        text.strip().replace("\n", " | "),
    )
    sizes = [
        int(line.split("=", 1)[1])
        for line in text.splitlines()
        if line.startswith("size=")
    ]
    check(
        "the file stopped at the budget",
        sizes == [1024 * 1024 * 1024],
        f"{sizes[0] / MIB:.1f} MiB written of a 1024 MiB budget",
    )
    check("the sandbox itself survived the refusal", "alive" in text)

    # The same file is gone again, so D measures its own writes.
    Sandbox.connect(sb.sandbox_id, api_url=API, sandbox_url=API, api_key=KEY)

    # ---------------------------------------------------------------- D: account
    print("\nD. the measurement is the accounting")
    # The worker's first scan may not have happened yet (`diskUsed` reads 0
    # until a measurement exists, which is the documented "unknown", not
    # "empty"). Wait for the scan instead of racing it.
    def usage() -> int:
        metric = sb.get_metrics()
        metric = metric[0] if isinstance(metric, list) else metric
        return int(metric.disk_used)

    first = 0
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        first = usage()
        if first > 0:
            break
        time.sleep(3)
    check("the first scan lands in the API", first > 0, f"diskUsed={first} B")

    # Stay under the per-file ceiling but cross the tree budget (1024 MiB): two
    # files, neither of which RLIMIT_FSIZE can object to.
    sb.commands.run(
        "dd if=/dev/zero of=/home/user/a.bin bs=1M count=700 status=none; "
        "dd if=/dev/zero of=/home/user/b.bin bs=1M count=500 status=none",
        timeout=900,
    )
    reason = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        text = refusal(lambda: sb.files.write("/over.txt", "x"))
        if text is not None:
            reason = text
            break
        time.sleep(3)
    check(
        "the enforcer pauses an over-budget tree",
        reason is not None and "grew past its budget" in reason,
        reason or "still running after 90s",
    )
    if reason:
        print(f"       refusal text: {reason}")
    after = usage()
    check(
        "metrics show the grown tree",
        after > 1024 * MIB,
        f"{after / MIB:.1f} MiB",
    )

    logs = httpx.get(
        f"{API}/sandboxes/{sb.sandbox_id}/logs",
        headers={"X-API-Key": KEY},
        timeout=30,
    ).json()
    pause_lines = [entry["line"] for entry in logs if "paused:" in entry["line"]]
    check(
        "the sandbox's own log says why it was paused",
        any("grew past its budget" in line for line in pause_lines),
        repr(pause_lines[-1]) if pause_lines else "no pause line",
    )

    # The fleet counter is the sibling view of the same fleet.
    fleet = httpx.get(
        f"{API}/internal/fleet/metrics",
        headers={"X-Internal-Key": INTERNAL},
        timeout=30,
    ).json()
    print(f"       fleet workspaceDisk: {fleet.get('workspaceDisk')}")
finally:
    sb.kill()

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    raise SystemExit(1)
print("all N28 acceptance checks passed")
