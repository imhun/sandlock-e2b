"""The worker's half of a checkpoint: storage, the platform account, the lifecycle.

The decision these pin is ``docs/checkpoint-restore-e2b-half.md`` §2 D1-D6, and
the two alternatives it rejects are both invisible until a fleet hits them:
billing an image to the sandbox's own ``diskMB`` makes ``pause`` -- an action
that is supposed to *free* a node -- take the sandbox's writes away, and not
accounting for it at all makes a checkpoint a way to grow a footprint nobody
bills. So the bytes are measured for the platform, the image is refused (and
removed) rather than quietly spent, and the resume consumes it.

The engine is faked here on purpose: what a real capture costs and which fds it
can bring back is ``test_restore`` / ``test_instance*`` in the fork. This file is
about what the *deployment* does with an image once it exists.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from envd_service.priv_helpers import dir_size
from envd_service.runtime import checkpoint_store
from envd_service.runtime.checkpoint_store import (
    IMAGE_NAME,
    capture_checkpoint_image,
    checkpoint_image_dir,
    checkpoint_status,
    consume_checkpoint_image,
    live_session_present,
    record_restore_outcome,
    restore_outcome_path,
    restore_checkpoint_image,
    resume_sandbox,
)
from envd_service.runtime.platform_disk import measure_platform_disk_bytes
from gateway_common.paths import sandbox_checkpoint_dir, sandbox_runtime_dir

MIB = 1024 * 1024


class _FakeExecutor:
    """The executor's checkpoint verbs, shaped like the real ones.

    A real capture writes the engine's image form into the path it is handed and
    answers with the pid/fd count; a real restore answers with the fds it could
    not bring back. What is reproduced here is the *contract*: the path is the
    worker's, the reply is the engine's, and a refusal comes back as a reason.
    """

    def __init__(
        self,
        *,
        live_session: bool = True,
        capture_reply: dict | None = None,
        restore_reply: dict | None = None,
        image_files: int = 1,
        image_bytes: int = 4096,
    ) -> None:
        self.instance_handle = object() if live_session else None
        self._capture_reply = capture_reply
        self._restore_reply = restore_reply
        self._image_files = image_files
        self._image_bytes = image_bytes
        self.captures: list[str] = []
        self.restores: list[str] = []

    def capture_checkpoint(self, dir: str, name: str | None = None) -> dict:
        self.captures.append(dir)
        if self._capture_reply is not None:
            return dict(self._capture_reply)
        image = Path(dir)
        image.mkdir(parents=True, exist_ok=True)
        for i in range(self._image_files):
            (image / f"part{i}.bin").write_bytes(b"x" * self._image_bytes)
        return {
            "captured": True,
            "reason": "",
            "dir": dir,
            "name": name,
            "pid": 4242,
            "fds": 3,
        }

    def restore_checkpoint(self, dir: str) -> dict:
        self.restores.append(dir)
        if self._restore_reply is not None:
            return dict(self._restore_reply)
        return {
            "restored": True,
            "reason": "",
            "dir": dir,
            "child_id": 7,
            "pid": 31337,
            "restore_skipped": [],
        }


def _ctx(executor) -> SimpleNamespace:
    """A runtime context as far as this module is concerned: it owns an executor."""
    return SimpleNamespace(executor=executor)


def _sandbox_tree(base: Path, sandbox_id: str) -> Path:
    tree = base / sandbox_id
    (tree / "workspace").mkdir(parents=True)
    (tree / "workspace" / "work.txt").write_bytes(b"w" * 4096)
    return tree


def test_a_capture_lands_in_the_platforms_dir_and_is_billed_to_the_platform(
    tmp_path: Path,
) -> None:
    """D1/D2/D3 in one walk: where the image is, and whose number moves."""
    base = tmp_path / "sandboxes"
    tree = _sandbox_tree(base, "sbx_store")
    sandbox_before = dir_size(tree)
    executor = _FakeExecutor()

    reply = capture_checkpoint_image(base, _ctx(executor), "sbx_store")

    expected = sandbox_checkpoint_dir(base, "sbx_store") / IMAGE_NAME
    assert reply["captured"] is True
    assert reply["reason"] == ""
    assert reply["image"] == str(expected)
    assert reply["pid"] == 4242
    assert reply["fds"] == 3
    assert executor.captures == [str(expected)]
    assert expected.is_dir(), "the image must be where the reply says it is"
    assert dir_size(tree) == sandbox_before, (
        "the sandbox's own number must not move: pause is not allowed to spend "
        "the user's budget"
    )
    assert measure_platform_disk_bytes(base) > 0, (
        "and the platform's number must see it, or the image is a footprint "
        "nobody bills"
    )
    assert expected.parent.parent.parent == base / "_runtime", (
        f"the image belongs to the platform's runtime dir, got {expected}"
    )
    assert tree not in expected.parents, (
        "and never inside the tree the sandbox owns (it could read it there)"
    )


def test_the_image_directory_is_closed_to_the_sandbox(tmp_path: Path) -> None:
    """D2: each ``<id>`` is 0700 -- the image is memory, and no other sandbox reads it.

    The *gate* above it is deliberately listable (0755) instead: the
    platform-disk account measures the store one child at a time, so the worker
    must be able to enumerate it. The old 0711 gate made that enumeration
    impossible, which made the account unmeasurable from the first capture
    onwards, which refused every later capture while the pause silently kept the
    previous image (see `_prepare_image_parent`).
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")

    capture_checkpoint_image(base, _ctx(_FakeExecutor()), "sbx_store")

    parent = checkpoint_image_dir(base, "sbx_store").parent
    mode = stat.S_IMODE(parent.stat().st_mode)
    assert mode == 0o700, f"the checkpoint dir must be 0700, got {oct(mode)}"
    assert parent.stat().st_uid == os.geteuid(), (
        "with no pooled uid to hand it to, it stays the worker's"
    )
    # The store gate is traversable *and listable*: measureable beats unlistable.
    root = parent.parent
    root_mode = stat.S_IMODE(root.stat().st_mode)
    assert root_mode == 0o755, f"the store gate must be listable, got {oct(root_mode)}"


def test_an_old_store_gate_is_repaired_before_the_account_is_measured(
    tmp_path: Path,
) -> None:
    """A store an earlier build created (0711) must become listable on use.

    The account is measured by walking the store's children, so on a running
    deployment -- whose store already exists at 0711 -- every capture after the
    first stayed refused until the volume was recreated. Repairing the mode when
    the account is measured is what makes the fix land without a fresh volume.
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    root = base / "_runtime" / ".checkpoints"
    root.mkdir(parents=True)
    os.chmod(root, 0o711)

    capture_checkpoint_image(base, _ctx(_FakeExecutor()), "sbx_store")

    assert stat.S_IMODE(root.stat().st_mode) == 0o755, (
        "the gate must be listable after the account was measured"
    )


def test_a_second_capture_survives_a_chmod_it_may_not_do(
    tmp_path: Path, monkeypatch
) -> None:
    """A re-pause must not die on a `chmod` of a directory that is not ours.

    Measured 2026-10-05 on the k0s acceptance: the *second* pause of a sandbox
    always failed with "the checkpoint directory could not be handed to the
    sandbox's uid 10000: PermissionError" -- the capture chmods `<store>/<id>`
    unconditionally, and after the first pause that directory belongs to the
    sandbox's uid, which an unprivileged worker may not chmod. The pause then
    kept the *previous* image for the resume to pick up.
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    first = capture_checkpoint_image(base, _ctx(_FakeExecutor()), "sbx_store")
    assert first["captured"] is True, first

    parent = checkpoint_image_dir(base, "sbx_store").parent
    real_chmod = os.chmod

    def deny_parent(path, mode, *args, **kwargs):
        if os.fspath(path) == os.fspath(parent):
            raise PermissionError(1, "Operation not permitted")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", deny_parent)

    second = capture_checkpoint_image(base, _ctx(_FakeExecutor()), "sbx_store")

    assert second["captured"] is True, (
        f"the second capture must tolerate the chmod it cannot do: {second}"
    )


def test_the_image_directory_is_handed_to_the_slot_that_will_write_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The capture runs *as the sandbox*: a root-owned dir makes the save EACCES.

    Measured on the cluster (2026-09-25): the slot is started as the sandbox's
    pooled uid, so the engine captured the workload and then died writing it
    ("checkpoint save failed: ... Permission denied"). The hand-off is therefore
    a step of its own, and which uid it names is what this pins -- in the shape
    that ships, the agent performs it, asked for by the sandbox id (no path
    leaves the worker: hard rule 3).
    """
    from envd_service import agent_fileops

    handed: list[tuple[str, bool]] = []

    class _Client:
        def chown_checkpoint(self, sandbox_id: str, *, recursive: bool = True):
            handed.append((sandbox_id, recursive))

        def checkpoint_bytes(self, sandbox_id: str) -> int:
            return 0

    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    executor = _FakeExecutor()

    monkeypatch.setattr(agent_fileops, "_ACTIVE", [_Client()])
    reply = capture_checkpoint_image(
        base, _ctx(executor), "sbx_store", owner_uid=20001
    )

    # The sandbox id, not the path: the control plane derives
    # `<state base>/_runtime/.checkpoints/<id>` from it.
    assert handed == [("sbx_store", False)]
    assert reply["captured"] is True, f"a hand-off must not block the capture: {reply}"


def test_a_directory_that_cannot_be_handed_over_is_not_captured(
    tmp_path: Path, monkeypatch
) -> None:
    """No hand-off, no capture: the save would fail inside the slot anyway."""
    from envd_service import priv_helpers

    def broken(path, uid, *, recursive=False, sandbox_id=None):
        raise priv_helpers.PrivHelperError("no brokers on this worker")

    monkeypatch.setattr(checkpoint_store, "_hand_to_sandbox", broken)
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    executor = _FakeExecutor()

    reply = capture_checkpoint_image(
        base, _ctx(executor), "sbx_store", owner_uid=20001
    )

    assert reply["captured"] is False
    assert reply["reason"] == (
        "the checkpoint directory could not be handed to the sandbox's uid "
        "20001: PrivHelperError: no brokers on this worker"
    )
    assert executor.captures == [], "nothing may be captured into a dir it cannot write"


def test_a_full_platform_account_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch
) -> None:
    """The pre-check: with no room at all, a capture would only spend disk."""
    monkeypatch.setenv("E2B_PLATFORM_DISK_MB", "1")
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    image = checkpoint_image_dir(base, "sbx_store")
    image.mkdir(parents=True)
    (image / "held.bin").write_bytes(b"x" * MIB)
    executor = _FakeExecutor()

    reply = capture_checkpoint_image(base, _ctx(executor), "sbx_store")

    assert reply["captured"] is False
    assert reply["reason"] == (
        "the platform's checkpoint account is already full: 1 MiB held of its "
        "1 MiB budget, so no image can be taken; the sandbox is left paused in "
        "place instead of spending the user's disk"
    )
    assert executor.captures == [], (
        "nothing may be captured once the account is full: the capture is the "
        "largest thing this worker writes"
    )


def test_an_image_that_does_not_fit_is_removed_again(
    tmp_path: Path, monkeypatch
) -> None:
    """The real admission: the size is only knowable by writing it."""
    monkeypatch.setenv("E2B_PLATFORM_DISK_MB", "4")
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    # 8 MiB against a 4 MiB account: an order of magnitude clear of the block
    # arithmetic below, so the sentence the refusal produces is exact.
    executor = _FakeExecutor(image_files=1, image_bytes=8 * MIB)

    reply = capture_checkpoint_image(base, _ctx(executor), "sbx_store")

    image = checkpoint_image_dir(base, "sbx_store")
    assert executor.captures == [str(image)], "the capture itself did happen"
    assert reply["captured"] is False
    assert reply["image"] is None
    assert reply["reason"] == (
        "checkpoint of 8 MiB would put the platform's checkpoint account at 8 MiB, "
        "over its 4 MiB budget (0 MiB already held); the sandbox is left paused in "
        "place instead of spending the user's disk"
    )
    assert not image.exists(), (
        "an image that does not fit must be gone: the account is enforced on "
        "what was actually written, and leaving it would spend the bytes anyway"
    )


def test_a_refused_capture_is_a_reason_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """A slot that refuses (more than one live child, an older binary) is normal."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    executor = _FakeExecutor(
        capture_reply={
            "captured": False,
            "reason": "checkpoint requires exactly one live child, found 2",
        }
    )

    reply = capture_checkpoint_image(base, _ctx(executor), "sbx_store")

    assert reply["captured"] is False
    assert reply["reason"] == "checkpoint requires exactly one live child, found 2"
    assert not checkpoint_image_dir(base, "sbx_store").exists()
    assert reply["platformDiskUsedMB"] == 0


def test_no_live_session_is_said_out_loud(tmp_path: Path) -> None:
    """A worker with nothing running has nothing to capture -- and says so."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")

    reply = capture_checkpoint_image(base, None, "sbx_store")

    assert reply["captured"] is False
    assert reply["reason"] == "no live session on this worker to capture"
    assert reply["image"] is None


def test_feature_is_unlimited_until_a_fleet_opts_in(tmp_path: Path) -> None:
    """``E2B_PLATFORM_DISK_MB`` unset: the capture is taken, no budget reported."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")

    reply = capture_checkpoint_image(base, _ctx(_FakeExecutor()), "sbx_store")

    assert reply["captured"] is True
    assert reply["platformDiskBudgetMB"] == 0, "0 is the honest encoding of unlimited"


def test_a_restore_reports_what_could_not_come_back_and_consumes_the_image(
    tmp_path: Path,
) -> None:
    """S4/D6: sockets, pipes and memfds do not return, and the caller is told."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    image = checkpoint_image_dir(base, "sbx_store")
    image.mkdir(parents=True)
    (image / "meta.json").write_text("{}", encoding="utf-8")
    executor = _FakeExecutor(
        live_session=False,
        restore_reply={
            "restored": True,
            "reason": "",
            "dir": str(image),
            "child_id": 3,
            "pid": 99,
            "restore_skipped": [
                {"fd": 5, "path": "socket:[12345]"},
                {"fd": 6, "path": "pipe:[6789]"},
            ],
        },
    )

    reply = restore_checkpoint_image(base, _ctx(executor), "sbx_store")

    assert executor.restores == [str(image)]
    assert reply["restored"] is True
    assert reply["child_id"] == 3
    assert reply["pid"] == 99
    assert reply["unrecoveredFds"] == [
        {"fd": 5, "path": "socket:[12345]"},
        {"fd": 6, "path": "pipe:[6789]"},
    ]
    assert reply["unrecoveredFdCount"] == 2
    assert not image.exists(), (
        "a resumed image is consumed: leaving it would let the next resume rewind "
        "the sandbox to an older process"
    )


def test_a_restore_logs_the_connections_that_do_not_return(
    tmp_path: Path, caplog
) -> None:
    """D6 again, on the log side: the operator reads the same list."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    image = checkpoint_image_dir(base, "sbx_store")
    image.mkdir(parents=True)
    executor = _FakeExecutor(
        live_session=False,
        restore_reply={
            "restored": True,
            "reason": "",
            "dir": str(image),
            "child_id": 3,
            "pid": 99,
            "restore_skipped": [{"fd": 5, "path": "socket:[12345]"}],
        },
    )

    with caplog.at_level("INFO", logger="envd_service.runtime.checkpoint_store"):
        restore_checkpoint_image(base, _ctx(executor), "sbx_store")

    assert [r.getMessage() for r in caplog.records] == [
        f"sandbox sbx_store: resumed {image} into the session (child 3, pid 99); "
        "1 fd(s) could not come back (sockets/pipes/memfds): "
        "[{'fd': 5, 'path': 'socket:[12345]'}]"
    ]


def test_a_restore_without_an_image_says_so(tmp_path: Path) -> None:
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    executor = _FakeExecutor(live_session=False)

    reply = restore_checkpoint_image(base, _ctx(executor), "sbx_store")

    assert reply["restored"] is False
    assert reply["reason"] == "no checkpoint image for this sandbox"
    assert executor.restores == []


def test_the_store_is_measured_one_sandbox_at_a_time(tmp_path: Path) -> None:
    """The store is not one walkable tree: measure it child by child.

    ``<state>/_runtime/.checkpoints`` is listable (the gate) but every ``<id>``
    inside belongs to that sandbox's uid, so one ``dir_size`` of the whole store
    stops at the first unreadable child and reports *everything* as unknown --
    which refused every capture after the first (measured 2026-10-05). The
    fallback answers per sandbox id, which is exactly the directory name.
    """
    base = tmp_path / "sandboxes"
    store = base / "_runtime" / ".checkpoints"
    (store / "sbx_a" / "latest").mkdir(parents=True)
    (store / "sbx_a" / "latest" / "meta.json").write_text("{}", encoding="utf-8")
    (store / "sbx_b").mkdir()

    asked: list[str] = []

    def via_agent(name: str) -> int | None:
        asked.append(name)
        return 1024

    total = measure_platform_disk_bytes(base, child_bytes=via_agent)

    assert sorted(asked) == ["sbx_a", "sbx_b"], "one call per store child"
    assert total is not None and total >= 2048, (
        f"both children must land in the account, got {total!r}"
    )


def test_the_platform_account_uses_the_agent_for_a_child_out_of_reach(
    tmp_path: Path, monkeypatch
) -> None:
    """I-3 with the agent as the reader: an unreadable child does not sink the account.

    ``<state>/_runtime`` holds per-sandbox ``0700`` dirs the worker cannot read,
    so without a fallback the *whole* account came back unmeasurable -- and from
    the first capture onwards every later capture was refused while the pause
    silently kept the previous image (measured 2026-10-05 on the k0s
    acceptance). The agent measures exactly that directory, keyed by the
    sandbox id that is the directory's name.
    """
    from envd_service import priv_helpers

    base = tmp_path / "sandboxes"
    runtime = base / "_runtime"
    (runtime / "sbx_a").mkdir(parents=True)
    (runtime / "sbx_a" / "cmd.log").write_text("x" * 16, encoding="utf-8")

    real = priv_helpers.dir_size
    monkeypatch.setattr(
        priv_helpers,
        "dir_size",
        lambda path: None if str(path).endswith("sbx_a") else real(path),
    )

    assert measure_platform_disk_bytes(base) is None, (
        "the worker alone cannot measure a child it cannot read"
    )

    asked: list[str] = []

    def via_agent(name: str) -> int | None:
        asked.append(name)
        return 4096

    total = measure_platform_disk_bytes(base, child_bytes=via_agent)

    assert asked == ["sbx_a"], "the fallback is asked for exactly the unreadable child"
    assert total is not None and total >= 4096, (
        f"the fallback's number must land in the account, got {total!r}"
    )


def test_a_restore_still_sees_an_image_the_worker_uid_cannot_look_into(
    tmp_path: Path, monkeypatch
) -> None:
    """The store is the sandbox's 0700 directory, so "worker cannot look" is not
    "there is no image".

    Measured on the k0s cluster (2026-10-05): pause a sandbox, replace the worker
    pod that hosts it, resume -- and the new worker answers "no checkpoint image
    for this sandbox" while the image sits on the shared volume the whole time.
    `Path.is_dir()` reports EACCES as False, and the per-sandbox directory is
    0700 owned by the *sandbox's* uid, so the worker's own uid can never see
    into it. The slot that performs the restore runs as that uid; it is the one
    that should decide.
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    image = checkpoint_image_dir(base, "sbx_store")
    image.mkdir(parents=True)

    real_stat = os.stat

    def blind_stat(path, *args, **kwargs):
        # Everything at or below `<id>/latest` is invisible, exactly as it is for
        # uid 65534; the store root keeps working so the fixture still resolves.
        if str(path).startswith(str(image.parent)) and str(path) != str(image.parent):
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", blind_stat)
    executor = _FakeExecutor(
        live_session=False,
        restore_reply={
            "restored": True,
            "reason": "",
            "dir": str(image),
            "child_id": 1,
            "pid": 2,
        },
    )

    reply = restore_checkpoint_image(base, _ctx(executor), "sbx_store")

    assert reply["restored"] is True, reply
    assert executor.restores == [str(image)], (
        "the slot must get the chance to try: it is the uid that can read the image"
    )


def test_a_restore_refuses_to_add_a_process_next_to_a_live_session(
    tmp_path: Path,
) -> None:
    """Two processes for one sandbox is not what "resume" means."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    image = checkpoint_image_dir(base, "sbx_store")
    image.mkdir(parents=True)
    executor = _FakeExecutor(live_session=True)

    reply = restore_checkpoint_image(base, _ctx(executor), "sbx_store")

    assert reply["restored"] is False
    assert reply["reason"] == (
        "this worker already holds a live session for the sandbox; a resume "
        "thaws that session instead of adding the image's process next to it"
    )
    assert executor.restores == []
    assert image.is_dir(), "a refusal must not consume the image"


def test_resume_thaws_the_session_that_is_here_and_drops_the_stale_image(
    tmp_path: Path,
) -> None:
    """D5: the fast path. The image is stale the moment the sandbox runs again."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    image = checkpoint_image_dir(base, "sbx_store")
    image.mkdir(parents=True)
    executor = _FakeExecutor(live_session=True)

    reply = resume_sandbox(base, _ctx(executor), "sbx_store")

    assert reply["resumed"] is True
    assert reply["restored"] is False
    assert reply["staleImageRemoved"] is True
    assert executor.restores == [], "a live session is thawed, never rebuilt"
    assert not image.exists()


def test_resume_after_the_worker_is_gone_restores_the_image(tmp_path: Path) -> None:
    """D5: the whole point -- the worker that holds the sandbox is gone."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    image = checkpoint_image_dir(base, "sbx_store")
    image.mkdir(parents=True)
    executor = _FakeExecutor(live_session=False)

    reply = resume_sandbox(base, _ctx(executor), "sbx_store")

    assert reply["resumed"] is True
    assert reply["restored"] is True
    assert reply["pid"] == 31337
    assert executor.restores == [str(image)]
    assert not image.exists(), "and the image is consumed by the resume"


def test_resume_of_a_sandbox_that_was_never_checkpointed_is_quiet(
    tmp_path: Path,
) -> None:
    """Nothing was running, nothing was captured: today's behaviour, not an error."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    executor = _FakeExecutor(live_session=False)

    reply = resume_sandbox(base, _ctx(executor), "sbx_store")

    assert reply["resumed"] is True
    assert reply["restored"] is False
    assert reply["reason"] == "no checkpoint image for this sandbox"
    assert executor.restores == []


def test_consuming_an_image_that_is_not_there_says_nothing_happened(
    tmp_path: Path,
) -> None:
    base = tmp_path / "sandboxes"
    assert consume_checkpoint_image(base, "sbx_store") is False


def test_a_live_session_is_read_from_the_executor_not_from_state(
    tmp_path: Path,
) -> None:
    """D5's question, pinned: the executor answers it, not a record field."""
    live = _ctx(_FakeExecutor(live_session=True))
    dead = _ctx(_FakeExecutor(live_session=False))
    assert live_session_present(live) is True
    assert live_session_present(dead) is False
    assert live_session_present(None) is False


def test_the_capture_log_names_the_program_it_captured(
    tmp_path: Path, caplog
) -> None:
    """FUP-30 的教训落在日志上：抓到了谁要写在那一行里，而不是让读者去比内存大小。"""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_named")
    # 假执行器按给定的回复作答 ⇒ 它不写盘，`image_bytes` 因此是 0（日志里的两个 MiB 数
    # 就是 0）；这一条要钉的是"名字"，不是尺寸。
    executor = _FakeExecutor(
        capture_reply={
            "captured": True,
            "reason": "",
            "dir": str(checkpoint_image_dir(base, "sbx_named")),
            "pid": 4242,
            "fds": 3,
            "exe": "/usr/bin/dash",
            "argv": ["/bin/sh", "-c", "sh -c 'exec python3 -c pass'"],
        }
    )
    with caplog.at_level("INFO", logger="envd_service.runtime.checkpoint_store"):
        reply = capture_checkpoint_image(base, _ctx(executor), "sbx_named")

    assert reply["exe"] == "/usr/bin/dash"
    assert reply["argv"] == ["/bin/sh", "-c", "sh -c 'exec python3 -c pass'"]
    # 整行相等（不做子串判据）：日志里点名 dash，读者一眼就知道"抓错对象"了
    assert caplog.messages[-1] == (
        "sandbox sbx_named: checkpoint image written to "
        f"{checkpoint_image_dir(base, 'sbx_named')} (0 MiB, pid 4242, 3 fd(s), "
        "captured /usr/bin/dash ['/bin/sh', '-c', \"sh -c 'exec python3 -c pass'\"]); "
        "the platform account now holds 0 MiB of an unlimited budget"
    )


def test_checkpoint_status_says_there_is_no_image(tmp_path: Path) -> None:
    """A sandbox that was never paused answers with the shape, not with an error.

    一个从没 checkpoint 过的沙箱和一次也没恢复过的沙箱都是正常状态：读的人要能
    一眼拿到"没有图 / 没有恢复记录"，而不是去区分 404 和空值。
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_status")

    assert checkpoint_status(base, "sbx_status") == {
        "sandboxID": "sbx_status",
        "hasImage": False,
        "imageMB": 0,
        "capturedAt": None,
        "lastRestore": None,
    }


def test_checkpoint_status_reports_the_image_and_the_last_restore(
    tmp_path: Path,
) -> None:
    """有图 / 图多大 / 上次恢复丢了几个 fd —— 三个问题一次答完。"""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_status")
    image = checkpoint_image_dir(base, "sbx_status")
    image.mkdir(parents=True)
    (image / "meta.json").write_bytes(b"x" * (2 * MIB))
    record_restore_outcome(
        base,
        "sbx_status",
        {
            "restored": True,
            "reason": "",
            "pid": 31337,
            "unrecoveredFdCount": 2,
        },
    )

    status = checkpoint_status(base, "sbx_status")

    assert status["sandboxID"] == "sbx_status"
    assert status["hasImage"] is True
    assert status["imageMB"] == 2
    assert isinstance(status["capturedAt"], int)
    # 整份相等（不做子串判据）：键集就是契约，`at` 之后由实现从盘上读回。
    assert status["lastRestore"] == {
        "restored": True,
        "reason": "",
        "pid": 31337,
        "unrecoveredFdCount": 2,
        "at": status["lastRestore"]["at"],
    }


def test_the_restore_outcome_is_recorded_beside_the_runtime_record(
    tmp_path: Path,
) -> None:
    """恢复结果落在 worker 自己的运行时目录里，与 ``sandbox.json`` 并列。

    不往控制面的沙箱记录里加字段：那次恢复是某个 worker 做过的事，记录里再存一份
    就成了第二个真相来源（见模块 docstring 的 D3/D5）。
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_status")

    record_restore_outcome(
        base,
        "sbx_status",
        {"restored": False, "reason": "no checkpoint image for this sandbox"},
    )

    path = restore_outcome_path(base, "sbx_status")
    assert path == sandbox_runtime_dir(base, "sbx_status") / "last-restore.json"
    mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert mode == 0o700, (
        f"the runtime dir holds the record too and must stay 0700, got {oct(mode)}"
    )
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["restored"] is False
    assert written["reason"] == "no checkpoint image for this sandbox"
    assert written["pid"] is None
    assert written["unrecoveredFdCount"] == 0
    # 秒级、UTC：一处诊断读数不该给出它没有的分辨率，也不该让读者去猜时区。
    stamp = datetime.fromisoformat(written["at"])
    assert stamp.tzinfo is timezone.utc
    assert written["at"] == stamp.isoformat(timespec="seconds")


def test_an_image_with_no_record_is_reported_as_an_orphan(tmp_path: Path) -> None:
    """图有自己的 store，而 store 从前没有扫描入口。

    ``.checkpoints/*`` 的候选集只来自内存注册表与顶层沙箱树，所以"记录没了、图还在"
    的孤儿会一直占着平台账 —— 图是平台为这个沙箱持有的最大东西，账满了就拒新捕获。
    这一条钉的是**只列**与**双判据**：只要有任何一处还认领它，就原样留着。
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_orphan")
    image = checkpoint_image_dir(base, "sbx_orphan")
    image.mkdir(parents=True)
    (image / "meta.json").write_bytes(b"x")

    assert checkpoint_store.list_checkpoint_stores(base) == ["sbx_orphan"]

    # keep 里有它 ⇒ 一个字都不动（"CP 不认识它"不是唯一的判据）。
    assert (
        checkpoint_store.remove_orphan_checkpoint_stores(base, keep={"sbx_orphan"})
        == []
    )
    assert image.is_dir()
    assert checkpoint_store.checkpoint_store_is_empty(image) is False

    # 没有任何一处认领它 ⇒ 整棵 store 走，连同那张图。
    assert checkpoint_store.remove_orphan_checkpoint_stores(base, keep=set()) == [
        "sbx_orphan"
    ]
    assert not image.is_dir()
    assert not image.parent.exists()
    assert checkpoint_store.list_checkpoint_stores(base) == []


def test_a_refused_capture_leaves_no_empty_directory(tmp_path: Path) -> None:
    """拒绝不是"半个动作"：调用前后目录树必须逐字节相同。

    ``_prepare_image_parent`` 是先建目录、再让 slot 抓的，所以一个被 slot 拒掉的
    捕获从前会留下一个空 ``<id>/``：账上量得到它、没人认领它，下一次捕获还会以为
    "目录已经就绪"。
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_noroom")
    executor = _FakeExecutor(capture_reply={"captured": False, "reason": "1 live child"})

    reply = capture_checkpoint_image(base, _ctx(executor), "sbx_noroom")

    assert reply == {
        "sandbox_id": "sbx_noroom",
        "captured": False,
        "reason": "1 live child",
        "image": None,
        "imageMB": 0,
        "platformDiskUsedMB": 0,
        "platformDiskBudgetMB": 0,
    }
    assert not checkpoint_image_dir(base, "sbx_noroom").parent.exists()


def test_an_image_refused_by_the_account_is_removed_with_its_store(
    tmp_path: Path, monkeypatch
) -> None:
    """记账拒绝的那条路：图删了，装它的空目录也要一起走。

    这条路径连 ``latest`` 都没留下（引擎的 save 是 rename，只有成功才落名），
    留下来的只有 ``_prepare_image_parent`` 建的那层目录 —— 而它就是平台账上那
    个"没人认领、也装不下任何东西"的 4 KiB。
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_over")
    monkeypatch.setenv("E2B_PLATFORM_DISK_MB", "1")
    executor = _FakeExecutor(image_bytes=2 * MIB)

    reply = capture_checkpoint_image(base, _ctx(executor), "sbx_over")

    assert reply["captured"] is False
    assert reply["imageMB"] == 0
    assert not checkpoint_image_dir(base, "sbx_over").parent.exists()
