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

import os
import stat
from pathlib import Path
from types import SimpleNamespace

from envd_service.priv_helpers import dir_size
from envd_service.runtime import checkpoint_store
from envd_service.runtime.checkpoint_store import (
    IMAGE_NAME,
    capture_checkpoint_image,
    checkpoint_image_dir,
    consume_checkpoint_image,
    live_session_present,
    restore_checkpoint_image,
    resume_sandbox,
)
from envd_service.runtime.platform_disk import measure_platform_disk_bytes
from gateway_common.paths import sandbox_checkpoint_dir

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
    """D2: 0700, and no other sandbox can reach it -- the image is memory."""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")

    capture_checkpoint_image(base, _ctx(_FakeExecutor()), "sbx_store")

    parent = checkpoint_image_dir(base, "sbx_store").parent
    mode = stat.S_IMODE(parent.stat().st_mode)
    assert mode == 0o700, f"the checkpoint dir must be 0700, got {oct(mode)}"
    assert parent.stat().st_uid == os.geteuid(), (
        "with no pooled uid to hand it to, it stays the worker's"
    )
    # The store around it is the *only* thing the slot has to reach through:
    # traverse, no listing, so one sandbox cannot even enumerate the others.
    root = parent.parent
    root_mode = stat.S_IMODE(root.stat().st_mode)
    assert root_mode == 0o711, f"the store must be 0711, got {oct(root_mode)}"


def test_the_image_directory_is_handed_to_the_slot_that_will_write_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The capture runs *as the sandbox*: a root-owned dir makes the save EACCES.

    Measured on the cluster (2026-09-25): the slot is started as the sandbox's
    pooled uid, so the engine captured the workload and then died writing it
    ("checkpoint save failed: ... Permission denied"). The worker therefore
    hands the directory over -- directly as root, through ``e2b-maint``
    otherwise -- and this pins which uid it names.
    """
    from envd_service import priv_helpers

    handed: list[tuple[int, str, bool]] = []

    def record(uid, path, *, recursive=True, gid=None):
        handed.append((uid, str(path), recursive))

    monkeypatch.setattr(priv_helpers, "broker_chown", record)
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_store")
    executor = _FakeExecutor()

    reply = capture_checkpoint_image(
        base, _ctx(executor), "sbx_store", owner_uid=20001
    )

    image = checkpoint_image_dir(base, "sbx_store")
    if os.geteuid() == 0:
        # A root worker hands the directory over itself -- the broker is the
        # non-root mechanism (`e2b-maint` with CAP_CHOWN). What both paths have
        # to agree on is *which uid* ends up owning it, because that uid is who
        # writes the image inside the slot; so the direct case pins the effect
        # rather than a call the broker would have recorded.
        assert handed == []
        assert image.parent.stat().st_uid == 20001
    else:
        assert handed == [(20001, str(image.parent), False)]
    assert reply["captured"] is True, f"a hand-off must not block the capture: {reply}"


def test_a_directory_that_cannot_be_handed_over_is_not_captured(
    tmp_path: Path, monkeypatch
) -> None:
    """No hand-off, no capture: the save would fail inside the slot anyway."""
    from envd_service import priv_helpers

    def refuse(uid, path, *, recursive=True, gid=None):
        raise priv_helpers.PrivHelperError("no brokers on this worker")

    monkeypatch.setattr(priv_helpers, "broker_chown", refuse)
    if os.geteuid() == 0:
        # As root the hand-off is the worker's own chown, so the broker above is
        # never consulted: make the mechanism this privilege actually selects
        # fail the same way. The contract under test is the caller's ("a
        # hand-off that raises is reported, never a capture that dies inside the
        # slot"), and it must not depend on who is running the test.
        def broken(path, uid, *, recursive=False):
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
