"""P6 probe: does the pause→restore chain actually *resume* under this root?

Why this file exists at all
---------------------------
`tmp/k0s/probe-pure-restore-synthroot.sh` (the brief's Step 1) runs
`tests/contract/test_pause_resume_sandlock.py`, and that file never reaches a
restore: `E2B_PAUSE_CHECKPOINT` defaults off (so `pause` writes no image), and
`Sandbox.connect()` resumes a sandbox whose session is still on the worker, so
`resume_sandbox` takes its **thaw** branch and the restore verb is never called.
Measured: `tmp/k0s/pure-rootfs-restore-{0,1}.log` contain zero mentions of
`checkpoint` or the stub. A green run there says "SIGSTOP/SIGCONT works in both
root states" -- real, but not the question.

The question is the one `docs/chroot-workspace-exec.md` §11.6.1 answers: the
restore stub is a **host build artifact**, and under a root the exec route that
names it by path can no longer resolve it; the fd route
(`execveat(AT_EMPTY_PATH)` + one `EXECUTE|READ_FILE` grant on that host file) is
what has to carry it. If that route did not cover a shape, the engine answers,
after 10 s, `restore stub never signalled READY within 10000ms: ...`.

So this probe drives the deployment's own two halves, through the same entry
points the worker uses (no reimplementation):

* **pause half** -- `checkpoint_store.capture_checkpoint_image`, which is what
  `_checkpoint_before_pause` calls: it hands the image directory to the pooled
  uid and asks the slot for the `checkpoint` verb.
* **resume half, restore branch** -- `checkpoint_store.restore_checkpoint_image`
  on a *fresh* executor (a new slot, no session yet). That is the worker-restart
  shape the whole feature exists for, and it is the branch that runs the stub.

Two rounds per state, because the first one alone cannot tell *why* it worked:

1. **default stub path** -- the wheel's own file,
   `/usr/local/lib/python3.14/site-packages/sandlock/bin/restore-stub`.
2. **stub relocated outside the sandbox's tree** -- a byte-identical copy under
   the run's own scratch base, selected with the fork's `SANDLOCK_RESTORE_STUB`
   knob. This is the shape §11.6.1 is about, and the probe *proves* the guest
   cannot name it there (`ls -l` inside the sandbox) before crediting the fd
   route with the restore. Measured 2026-09-26: the synthesized skeleton binds
   the host's `/usr`, so round 1's stub is guest-visible in **both** states --
   round 1 is the end-to-end answer, round 2 is the §11.6.1 answer.

The verdict is a sentence, not a boolean (the brief's "Produce"): either
``恢复成立`` with the pid/child id and the evidence, or ``被显式拒绝`` with the
engine's own reason verbatim.

Why the counter is written through an **absolute** path (``d=$(pwd)`` at
launch): the workload's own cwd after a restore is the engine's business (the
restored process's cwd measures as `/` on this engine), and a relative ``tick``
would make "the file did not move" say nothing about whether the *process* came
back. Resolving the directory once, at launch, keeps the question on the
process. `/proc/<pid>` evidence is recorded beside it so a restored-but-stopped
process cannot be mistaken for a restored-and-running one.

Driven by ``tmp/k0s/task12/probe-restore-synthroot.sh`` (both states, one lane
run each, image pinned to a same-source tag), which also folds the verdict
files into the summary log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import sandlock  # noqa: E402
from envd_service.executors.base import ExecConfig  # noqa: E402
from envd_service.runtime.checkpoint_store import (  # noqa: E402
    capture_checkpoint_image,
    checkpoint_image_dir,
    restore_checkpoint_image,
)
from tests.security.conftest import (  # noqa: E402
    route_b_sandbox,
    sandbox_tmpdir,
    sandlock_ready,
)

#: Exactly the file the fork's `resume::stub_path()` hands a restore when
#: `SANDLOCK_RESTORE_STUB` is unset: the wheel's own build artifact.
STUB_HOST_PATH = Path(sandlock.__file__).resolve().parent / "bin" / "restore-stub"

#: A path that exists nowhere, handed to the same knob. The engine's `stub_path()`
#: returns the override verbatim (`crates/sandlock-core/src/checkpoint/resume.rs:87`)
#: and the restore then refuses **by name** -- which is what makes "the override
#: was honoured" measurable instead of assumed. Fixed rather than run-specific so
#: the refusal's sentence is the same string on every run.
ABSENT_STUB_PATH = REPO_ROOT / "tmp" / "k0s" / "task12" / "absent-restore-stub"

#: Counts up in the sandbox until it is killed. ``mv`` (rename) so a reader
#: never sees a half-written number -- the production acceptance learned that
#: one the hard way (a truncate landing inside the freeze window reads as "the
#: counter vanished").
WORKLOAD = (
    'd=$(pwd); i=0; while true; do i=$((i+1)); printf "%s" "$i" > "$d/tick.tmp"; '
    'mv "$d/tick.tmp" "$d/tick"; sleep 0.2; done'
)


def _root_state() -> tuple[str, str, dict[str, str]]:
    """This run's shape, spelled the way the lane spells it."""
    switches = {
        "E2B_PURE_ROOTFS": os.environ.get("E2B_PURE_ROOTFS", ""),
        "E2B_REAL_ROOT": os.environ.get("E2B_REAL_ROOT", ""),
    }
    if switches["E2B_REAL_ROOT"] == "0" and not switches["E2B_PURE_ROOTFS"]:
        return "0 (identity: no synthesized root)", "identity", switches
    if switches["E2B_REAL_ROOT"] == "1" and switches["E2B_PURE_ROOTFS"] == "synth":
        return (
            "1 (synthesized root + real root)",
            "synth-realroot1",
            switches,
        )
    raise AssertionError(
        "this lane run is neither of the two states the 2026-09-26 ruling leaves "
        f"standing ({switches}); refusing to measure a third configuration"
    )


def _read_counter(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


async def _wait_for(predicate, timeout_s: float, interval_s: float = 0.1):
    """Poll ``predicate`` until it is true; returns ``(ok, last_value)``."""
    deadline = time.monotonic() + timeout_s
    value = None
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return True, value
        await asyncio.sleep(interval_s)
    return False, value


def _proc_facts(pid: int | None) -> dict:
    """What ``/proc`` says about a pid: state, cwd, and whether it is there."""
    if pid is None:
        return {"pid": None, "exists": False}
    base = Path(f"/proc/{pid}")
    facts: dict = {"pid": pid, "exists": base.exists()}
    try:
        # The second parenthesised field is the comm; the state is right after.
        stat = (base / "stat").read_text(encoding="utf-8")
        facts["state"] = stat.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        facts["state"] = None
    try:
        facts["cwd"] = os.readlink(base / "cwd")
    except OSError:
        facts["cwd"] = None
    return facts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _Ctx:
    """What ``checkpoint_store`` needs of a runtime context: its executor."""

    def __init__(self, executor) -> None:
        self.executor = executor


@pytest.fixture()
def verdict_path() -> Path:
    """Where the sentence lands; the wrapper script folds it into the log.

    The lane runs pytest with ``-q``, which captures whatever the test prints,
    so a verdict smuggled into stdout would only ever show up if the test
    failed. The path is repo-relative on purpose: in the lane the repo is the
    `/workspace` bind mount, so the file lands in the *host* project's own
    ``tmp/`` where the wrapper script can fold it into the summary log.
    """
    explicit = os.environ.get("TASK12_VERDICT_FILE")
    if explicit:
        return Path(explicit)
    _state, slug, _switches = _root_state()
    return REPO_ROOT / "tmp" / "k0s" / "task12" / f"verdict-{slug}.json"


def test_the_pause_image_resumes_into_a_fresh_session(verdict_path) -> None:
    asyncio.run(_probe(verdict_path))


async def _probe(verdict_path: Path) -> None:
    state, slug, switches = _root_state()
    assert sandlock_ready(), (
        "this probe needs Linux + sandlock (Landlock ABI >= 6); run it through "
        "tmp/k0s/task12/probe-restore-synthroot.sh so the lane container "
        "provides both"
    )

    # The e2b-side log lines (image written / process resumed / fds lost) are
    # evidence too, and pytest captures them: keep a copy beside the verdict.
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(verdict_path.with_suffix(".eb-log")), "w")
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    # One base for the sandbox tree, the image store and the synthesized root,
    # pinned so the two executors (before and after the "restart") agree on it
    # -- the deployment sets it to `<workspace_base>/_pure_rootfs` the same way.
    base = sandbox_tmpdir(suffix="-task12-base")
    os.environ["E2B_PURE_ROOTFS_DIR"] = str(base / "_pure_rootfs")

    record: dict = {
        "state": state,
        "switches": switches,
        "stubHostPath": str(STUB_HOST_PATH),
        "stubHostSha256": _sha256(STUB_HOST_PATH),
        "rounds": [],
        "negativeControl": None,
        "verdict": None,
    }
    failure: AssertionError | None = None
    try:
        record["rounds"].append(
            await _round(base=base, label="default-stub-path", stub=None)
        )
        relocated = base / "outside-the-sandbox-tree" / "restore-stub"
        relocated.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(STUB_HOST_PATH, relocated)
        relocated.chmod(0o755)
        assert _sha256(relocated) == record["stubHostSha256"], (
            f"the relocated stub ({relocated}) is not the wheel's own bytes"
        )
        record["rounds"].append(
            await _round(
                base=base,
                label="stub-relocated-outside-the-tree",
                stub=relocated,
            )
        )
        # Negative control: same knob, a path that is not there. The restore must
        # refuse *by name* -- otherwise round 2's success could have been the
        # default stub doing the work and this probe would be crediting the fd
        # route for nothing.
        record["negativeControl"] = await _round(
            base=base,
            label="absent-stub-path",
            stub=ABSENT_STUB_PATH,
            expect_restored=False,
        )
        record["verdict"] = _sentence(
            state, record["rounds"], record["negativeControl"]
        )
    except AssertionError as exc:
        # The brief's other accepted answer: an explicit refusal, quoted
        # verbatim. Keep the record and the reason, then re-raise so the lane's
        # exit status (and the pytest line) can never read as success.
        failure = exc
        record["verdict"] = f"被显式拒绝 / 不成立：{exc}"
    finally:
        _clear_stub_override()
        root_logger.removeHandler(handler)
        handler.close()
        verdict_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if failure is not None:
        raise failure


def _sentence(state: str, rounds: list[dict], control: dict) -> str:
    """The brief's deliverable: one sentence, 恢复成立 or 被显式拒绝."""
    parts = []
    for round_ in rounds:
        capture = round_["capture"]
        restore = round_["restore"]
        parts.append(
            f"[{round_['label']}] image={capture['imageMB']} MiB pid {capture['pid']} "
            f"exe {capture['exe']!r} argv {capture['argv']} → 新 session pid "
            f"{restore['pid']} (child {restore['child_id']})，恢复后 exec rc="
            f"{round_['execAfterRestore'][0]}，计数器 {round_['counterAtCapture']} → "
            f"{round_['counterAfterRestore']}，未恢复 fd {restore['unrecoveredFdCount']} 个；"
            f"stub 在沙箱里{'看得见' if round_['stubVisibleInGuest'][0] == 0 else '看不见'}"
            f"（rc={round_['stubVisibleInGuest'][0]}）"
        )
    return (
        f"恢复成立（{state}）："
        + "；".join(parts)
        + "；负对照（把 SANDLOCK_RESTORE_STUB 指到一个不存在的路径）被显式拒绝："
        + repr(control["restore"]["reason"])
    )


def _clear_stub_override() -> None:
    os.environ.pop("SANDLOCK_RESTORE_STUB", None)


async def _round(
    *, base: Path, label: str, stub: Path | None, expect_restored: bool = True
) -> dict:
    """One pause→(worker gone)→restore cycle; returns its raw observations.

    ``expect_restored=False`` is the negative control: the cycle is run the same
    way, but the resume half must refuse **by name** (``stub`` is the path it
    must name). The reply is kept verbatim either way.
    """
    sandbox_id = f"sbx_task12_{label.replace('-', '_')}"
    workspace = sandbox_tmpdir(suffix=f"-task12-{sandbox_id}")
    round_: dict = {
        "label": label,
        "sandboxId": sandbox_id,
        "stubOverride": str(stub) if stub is not None else None,
        "image": str(checkpoint_image_dir(base, sandbox_id, state_base=base)),
        "capture": None,
        "counterAtCapture": None,
        "restore": None,
        "restoredProcBefore": None,
        "execAfterRestore": None,
        "counterAfterRestore": None,
        "restoredProcAfter": None,
        "stubVisibleInGuest": None,
    }

    executor_a = running = executor_b = None
    try:
        # ---- pause half: capture, exactly like `_checkpoint_before_pause` ----
        executor_a, workspace = route_b_sandbox(
            None, None, workspace=workspace, sandbox_id=sandbox_id
        )
        owner_uid = executor_a._run_as_identity()[0]
        running = await executor_a.start(
            ExecConfig(
                cmd=["/bin/sh", "-c", WORKLOAD],
                env={},
                cwd=str(workspace),
                stdin_enabled=False,
            )
        )
        tick = workspace / "tick"
        started, _value = await _wait_for(lambda: _read_counter(tick), timeout_s=20.0)
        assert started, (
            f"[{label}] the workload never wrote its counter, so there is nothing "
            f"to capture a running process from (workspace={workspace})"
        )

        capture = capture_checkpoint_image(
            base,
            _Ctx(executor_a),
            sandbox_id,
            owner_uid=owner_uid,
            state_base=base,
        )
        round_["capture"] = {
            key: capture.get(key)
            for key in (
                "captured",
                "reason",
                "image",
                "imageMB",
                "pid",
                "fds",
                "exe",
                "argv",
            )
        }
        assert capture.get("captured") is True, (
            f"[{label}] the pause half wrote no image: {capture.get('reason')!r} "
            f"(capture reply: {capture!r})"
        )
        at_capture = _read_counter(tick)
        round_["counterAtCapture"] = at_capture
        captured_pid = capture.get("pid")

        # ---- the worker goes away: kill the child, drop the session ----------
        running.kill(signal.SIGKILL)
        executor_a.close()
        executor_a = None
        gone, _ = await _wait_for(
            lambda: (
                not Path(f"/proc/{captured_pid}").exists() if captured_pid else True
            ),
            timeout_s=10.0,
        )
        assert gone, (
            f"[{label}] captured pid {captured_pid} is still in /proc after the "
            "teardown"
        )

        # ---- resume half, restore branch: a *fresh* slot, no session ---------
        if stub is not None:
            os.environ["SANDLOCK_RESTORE_STUB"] = str(stub)
        else:
            _clear_stub_override()
        executor_b, _ws = route_b_sandbox(
            None, None, workspace=workspace, sandbox_id=sandbox_id
        )
        assert executor_b.instance_handle is None, (
            f"[{label}] a fresh executor already holds a session; this probe would "
            "measure the thaw branch and say nothing about the restore stub"
        )
        restore = restore_checkpoint_image(
            base,
            _Ctx(executor_b),
            sandbox_id,
            owner_uid=owner_uid,
            state_base=base,
        )
        round_["restore"] = {
            key: restore.get(key)
            for key in ("restored", "reason", "pid", "child_id", "unrecoveredFdCount")
        }
        if not expect_restored:
            # The refusal is the observation: the engine must name the path it
            # was told to use, which is what proves the override reached it.
            # Pinned verbatim (observed 2026-09-26, both root states, on
            # `e2b-sandlock-test:task12cur`); the path is fixed so the sentence is
            # the same string on every run.
            expected_reason = (
                "instance restore failed: process error: child process error: "
                f"restore-stub was not built ({ABSENT_STUB_PATH}); a C compiler is "
                "required to build sandlock with checkpoint restore"
            )
            assert restore.get("restored") is False, (
                f"[{label}] a restore with a stub path that exists nowhere "
                f"succeeded: {restore!r}; the `SANDLOCK_RESTORE_STUB` override did "
                "not reach the engine, so round 2 cannot be credited either"
            )
            assert restore.get("reason") == expected_reason, (
                f"[{label}] the refusal does not name the override: "
                f"{restore.get('reason')!r}"
            )
            return round_
        assert restore.get("restored") is True, (
            f"[{label}] the resume half could not put the image back: "
            f"{restore.get('reason')!r} (restore reply: {restore!r})"
        )
        round_["restoredProcBefore"] = _proc_facts(restore.get("pid"))

        # ---- the session still serves exec, and the workload kept counting ----
        code, out, err = await _run(executor_b, workspace)
        round_["execAfterRestore"] = [code, out.decode(), err.decode()]
        assert (code, out, err) == (0, b"restored-ok\n", b""), (
            f"[{label}] the restored session did not serve `exec` the way the "
            f"deployment needs it to: rc={code} stdout={out!r} stderr={err!r}"
        )
        advanced, _value = await _wait_for(
            lambda: (v := _read_counter(tick)) is not None and v > (at_capture or 0),
            timeout_s=20.0,
        )
        round_["counterAfterRestore"] = _read_counter(tick)
        round_["restoredProcAfter"] = _proc_facts(restore.get("pid"))
        assert advanced, (
            f"[{label}] the counter did not advance past its value at the capture "
            f"({at_capture} -> {round_['counterAfterRestore']}); the restored "
            f"process reports {round_['restoredProcAfter']}"
        )

        # ---- can the *guest* name the stub this restore actually used? -------
        # If it can, this round cannot tell "the fd route carried it" apart from
        # "the path happened to resolve", and the report must say so instead of
        # crediting §11.6.1.
        used = stub if stub is not None else STUB_HOST_PATH
        code2, out2, err2 = await _run(executor_b, workspace, f"ls -l {used}")
        round_["stubVisibleInGuest"] = [code2, out2.decode(), err2.decode()]
        return round_
    finally:
        if running is not None:
            try:
                running.kill(signal.SIGKILL)
            except Exception:  # noqa: BLE001 - teardown must not mask the verdict
                pass
        for executor in (executor_a, executor_b):
            if executor is not None:
                executor.close()
        _clear_stub_override()


async def _run(
    executor, cwd, sh: str = "echo restored-ok"
) -> tuple[int, bytes, bytes]:
    """``start()`` + drain, the way ``tests/security/conftest.run_sh`` does."""
    running = await executor.start(
        ExecConfig(
            cmd=["/bin/sh", "-c", sh],
            env={},
            cwd=str(cwd),
            stdin_enabled=False,
        )
    )
    out: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    async for kind, chunk in running.output():
        if kind in out:
            out[kind].append(chunk)
    code = await running.exit_code()
    return code, b"".join(out["stdout"]), b"".join(out["stderr"])
