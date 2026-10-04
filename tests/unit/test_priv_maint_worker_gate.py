"""Audit STATIC-5 (2026-10-04): ``e2b-maint chown --worker`` must not bypass the gate.

The defect
----------
``maint.c``'s two chown arms disagreed about what may be written into a
privileged tree::

    --uid U    ->  priv_validate_uid(U)          # uid-pool membership
    --worker   ->  uid = priv_worker_uid();      # ...no gate at all

``priv_worker_uid()`` reads ``E2B_BROKER_WORKER_UID``, which ``c3_agent`` fills
from the request body, so the second arm wrote whatever a caller named.
Reproduced against the production translation unit before the fix: with the
worker uid set to 1 (outside the pool) and ``--recursive``, the call reached
``lchown``.

What is asserted here
---------------------
The real production sources are compiled -- ``maint.c`` + ``priv_common.c``, not
a reimplementation -- and driven as the agent image drives them. Every case below
is decided **before** any privileged syscall, so the whole file runs as an
unprivileged user and none of it depends on being able to chown anything. That
is the point: a refusal is a pure decision, and a test that needed root to
observe a refusal would be a worse test.

The two gates:

* ``--worker`` may not name an identity inside the sandbox uid pool. A caller can
  act as a pooled uid (it is that sandbox's own identity), so a privileged tree
  owned by one is a tree the matching sandbox can read and write. The legitimate
  deployments are outside the pool by construction: the k8s worker is 65534, the
  compose/test root worker is 0.
* ``--worker`` may not be combined with ``--recursive``. The only caller that
  uses this form is the slot-document scope (``control_plane/file_ops.py``),
  which is never recursive; recursing it turned one document operation into a
  node-wide ownership change.

Traversal and symlink escapes are re-asserted here as a regression guard: those
were verified by hand during the audit, and a gate added next to them should not
be the thing that quietly breaks them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIV_DIR = PROJECT_ROOT / "c3_agent" / "priv"

# Spelled out rather than inherited from the environment, because every
# expectation below is about where the pool boundary sits.
POOL_START = 10000
POOL_SIZE = 1000
POOL_END = POOL_START + POOL_SIZE - 1

EXIT_USAGE = 2
EXIT_REFUSED = 77

#: A uid inside the pool -- what the gate must refuse as a worker identity.
POOLED_UID = 10042
#: The production k8s worker identity -- what the gate must let through.
WORKER_UID = 65534


@pytest.fixture(scope="module")
def maint_binary(tmp_path_factory) -> Path:
    """Compile the production translation unit the agent image installs."""
    if shutil.which("cc") is None and shutil.which("gcc") is None:
        pytest.fail("no C compiler: the gate under test is C")
    out = tmp_path_factory.mktemp("maint") / "e2b-maint"
    subprocess.run(
        ["cc", "-O0", "-o", str(out), str(PRIV_DIR / "maint.c"), str(PRIV_DIR / "priv_common.c")],
        check=True,
        capture_output=True,
    )
    return out


@pytest.fixture()
def roots(tmp_path) -> dict[str, Path]:
    """A fake privileged-roots tree with the layout the whitelist expects."""
    base = tmp_path / "roots"
    workspace = base / "workspaces" / "sbx-victim"
    workspace.mkdir(parents=True)
    (workspace / "sub").mkdir()
    (workspace / "sub" / "file.txt").write_text("payload\n")
    state = base / "state" / "_runtime"
    state.mkdir(parents=True)
    return {
        "base": base,
        "workspace": workspace,
        "state": state,
    }


def _run(maint_binary: Path, roots: dict[str, Path], *args: str, worker_uid: int | None = None):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "E2B_WORKSPACE_BASE": str(roots["base"] / "workspaces"),
        "E2B_STATE_BASE": str(roots["base"] / "state"),
        "E2B_NODE_STATE_BASE": str(roots["base"] / "state"),
        "E2B_SHARED_VOLUME_ROOT": str(roots["base"]),
        "E2B_IMAGE_CACHE_DIR": str(roots["base"] / "images"),
        "E2B_UID_POOL_START": str(POOL_START),
        "E2B_UID_POOL_SIZE": str(POOL_SIZE),
    }
    if worker_uid is not None:
        env["E2B_BROKER_WORKER_UID"] = str(worker_uid)
        env["E2B_BROKER_WORKER_GID"] = str(WORKER_UID)
    return subprocess.run(
        [str(maint_binary), *args], capture_output=True, text=True, env=env, check=False
    )


# --------------------------------------------------------------------------
# Gate 1: --worker may not name a pooled uid
# --------------------------------------------------------------------------


def test_worker_chown_refuses_a_pooled_uid(maint_binary, roots):
    """The hole: a caller names a uid it can act as, and the tree becomes its own."""
    result = _run(
        maint_binary,
        roots,
        "chown",
        "--worker",
        "--path",
        str(roots["workspace"]),
        worker_uid=POOLED_UID,
    )
    assert result.returncode == EXIT_REFUSED
    assert result.stderr == (
        f"e2b-maint: refused: --worker would hand a privileged tree to uid "
        f"{POOLED_UID}, which is inside the sandbox uid pool: refusing "
        f"(uid {POOLED_UID} is inside the sandbox uid pool {POOL_START}..{POOL_END})\n"
    )


def test_worker_chown_refuses_the_first_pooled_uid(maint_binary, roots):
    """The boundary is inclusive: the pool's own first uid is still a sandbox uid."""
    result = _run(
        maint_binary,
        roots,
        "chown",
        "--worker",
        "--path",
        str(roots["workspace"]),
        worker_uid=POOL_START,
    )
    assert result.returncode == EXIT_REFUSED
    assert result.stderr == (
        f"e2b-maint: refused: --worker would hand a privileged tree to uid "
        f"{POOL_START}, which is inside the sandbox uid pool: refusing "
        f"(uid {POOL_START} is inside the sandbox uid pool {POOL_START}..{POOL_END})\n"
    )


def test_worker_chown_refuses_the_last_pooled_uid(maint_binary, roots):
    result = _run(
        maint_binary,
        roots,
        "chown",
        "--worker",
        "--path",
        str(roots["workspace"]),
        worker_uid=POOL_END,
    )
    assert result.returncode == EXIT_REFUSED
    assert result.stderr == (
        f"e2b-maint: refused: --worker would hand a privileged tree to uid "
        f"{POOL_END}, which is inside the sandbox uid pool: refusing "
        f"(uid {POOL_END} is inside the sandbox uid pool {POOL_START}..{POOL_END})\n"
    )


@pytest.mark.parametrize("worker_uid", [WORKER_UID, 0, 1])
def test_worker_chown_lets_a_non_pooled_identity_through_the_gate(maint_binary, roots, worker_uid):
    """The gate must not become "only 65534" -- 0 is the compose root worker.

    This test cannot assert that the chown *succeeded*: as an unprivileged user
    ``lchown`` fails with EPERM, which is the whole reason every refusal above is
    observable here and a success is not. What it asserts precisely is that the
    failure is the syscall's and not the gate's -- i.e. the run got past the
    ``--worker`` identity check and reached ``lchown``.
    """
    result = _run(
        maint_binary,
        roots,
        "chown",
        "--worker",
        "--path",
        str(roots["workspace"]),
        worker_uid=worker_uid,
    )
    # What is under test is the `--worker` *identity* gate, not `lchown`. Whether
    # the chown itself then succeeds depends on the caller: as root it always
    # does, and even unprivileged a chown to the caller's own uid:gid is a
    # no-op that succeeds. Note both outcomes exit 77 when the syscall refuses,
    # so the discriminator is the message, not the code.
    assert "inside the sandbox uid pool" not in result.stderr, (
        f"uid {worker_uid} is outside the pool and must not be refused by the "
        f"pool gate; stderr was {result.stderr!r}"
    )
    if result.returncode != 0:
        assert re.fullmatch(
            r"e2b-maint: refused: chown .* to \d+:\d+ failed: .+\n", result.stderr
        ), result.stderr


# --------------------------------------------------------------------------
# Gate 2: --worker may not be recursive
# --------------------------------------------------------------------------


def test_worker_chown_refuses_recursive(maint_binary, roots):
    result = _run(
        maint_binary,
        roots,
        "chown",
        "--worker",
        "--recursive",
        "--path",
        str(roots["workspace"]),
        worker_uid=WORKER_UID,
    )
    assert result.returncode == EXIT_USAGE
    assert result.stderr == (
        "e2b-maint: usage: --worker cannot be combined with --recursive: the "
        "worker identity is only ever scoped to a single document\n"
    )


def test_worker_chown_refuses_recursive_even_for_a_pooled_uid(maint_binary, roots):
    """Both gates armed: the usage error is decided first and stays stable.

    Order matters for a caller reading the message. Whichever gate the binary
    reports, the run must not reach ``lchown``.
    """
    result = _run(
        maint_binary,
        roots,
        "chown",
        "--worker",
        "--recursive",
        "--path",
        str(roots["workspace"]),
        worker_uid=POOLED_UID,
    )
    assert result.returncode in (EXIT_USAGE, EXIT_REFUSED)
    assert re.fullmatch(
        r"e2b-maint: (usage: --worker cannot be combined with --recursive.*"
        r"|refused: --worker would hand a privileged tree to uid \d+.*)\n",
        result.stderr,
    ), result.stderr


# --------------------------------------------------------------------------
# Controls: the pre-existing gate is untouched
# --------------------------------------------------------------------------


def test_uid_chown_outside_the_pool_is_still_refused(maint_binary, roots):
    """The ``--uid`` arm's pool gate is unchanged by this fix."""
    result = _run(
        maint_binary, roots, "chown", "--uid", "1", "--path", str(roots["workspace"])
    )
    assert result.returncode == EXIT_REFUSED
    assert result.stderr == (
        f"e2b-maint: refused: uid 1 is outside the privileged helper uid pool "
        f"{POOL_START}..{POOL_END}\n"
    )


def test_uid_chown_inside_the_pool_reaches_the_syscall(maint_binary, roots):
    """The ``--uid`` arm still accepts a pooled uid -- that is its whole job."""
    result = _run(
        maint_binary, roots, "chown", "--uid", str(POOLED_UID), "--path", str(roots["workspace"])
    )
    # See `test_worker_chown_lets_a_non_pooled_identity_through_the_gate`: the
    # outcome to pin is that the pooled uid passed the pool gate, not that the
    # subsequent `lchown` failed.
    assert "outside the privileged helper uid pool" not in result.stderr, (
        f"uid {POOLED_UID} is in the pool and must not be refused for being "
        f"outside it: {result.stderr!r}"
    )
    if result.returncode != 0:
        assert re.fullmatch(
            r"e2b-maint: refused: chown .* to \d+:\d+ failed: .+\n", result.stderr
        ), result.stderr


def test_uid_and_worker_are_still_mutually_exclusive(maint_binary, roots):
    result = _run(
        maint_binary,
        roots,
        "chown",
        "--worker",
        "--uid",
        str(POOL_START),
        "--path",
        str(roots["workspace"]),
    )
    assert result.returncode == EXIT_USAGE
    assert result.stderr == "e2b-maint: usage: --worker cannot be combined with --uid\n"


# --------------------------------------------------------------------------
# Regression guard: the containment this gate sits next to
# --------------------------------------------------------------------------


#: ``PRIV_ERR_LEN`` from priv_common.h. Every refusal message the binary prints
#: is ``snprintf``-ed into a buffer of this size and therefore truncated to
#: ``PRIV_ERR_LEN - 1`` characters, NUL-terminated. The two containment
#: expectations below reproduce that truncation rather than loosening their
#: pattern to tolerate it -- a loose pattern here would also tolerate the gate
#: printing a *different* path.
PRIV_ERR_LEN = 512


def _outside_roots_stderr(path: str, roots: dict[str, Path]) -> str:
    """The exact refusal ``priv_resolve_allowed_path`` prints for ``path``.

    Reproduces ``priv_common.c`` exactly: the roots are joined by ``priv_roots_text``
    into a ``PRIV_ERR_LEN`` buffer, then the whole message is ``snprintf``-ed into
    another one, and ``priv_fail`` prefixes it. Two truncation stages, so the
    message is cut twice on a long path.
    """
    ordered = [
        str(roots["base"] / "workspaces"),
        str(roots["base"] / "state"),
        str(roots["base"] / "state"),
        str(roots["base"]),
        str(roots["base"] / "images"),
    ]
    roots_text = ""
    for index, root in enumerate(ordered):
        roots_text += ("" if index == 0 else ", ") + root
    roots_text = roots_text[: PRIV_ERR_LEN - 1]
    message = f"path {path} is outside the privileged helper roots ({roots_text})"
    message = message[: PRIV_ERR_LEN - 1]
    return f"e2b-maint: refused: {message}\n"


def test_traversal_out_of_the_roots_is_refused(maint_binary, roots):
    result = _run(
        maint_binary,
        roots,
        "chown",
        "--uid",
        str(POOL_START),
        "--path",
        str(roots["workspace"] / "sub" / ".." / ".." / ".."),
    )
    assert result.returncode == EXIT_REFUSED
    assert result.stderr == _outside_roots_stderr(
        str(roots["workspace"] / "sub" / ".." / ".." / ".."), roots
    )


def test_symlink_escape_is_refused(maint_binary, roots, tmp_path):
    """``realpath`` resolves the link before the whitelist compares, so a link
    pointing outside the roots names its *target* and is refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = roots["base"] / "workspaces" / "escape"
    link.symlink_to(outside)
    result = _run(
        maint_binary, roots, "chown", "--uid", str(POOL_START), "--path", str(link)
    )
    assert result.returncode == EXIT_REFUSED
    # The refusal names the *requested* path, which realpath() rejected before
    # it could be rewritten -- asserted literally so a future change that starts
    # reporting the resolved target has to be a deliberate edit here.
    assert result.stderr == _outside_roots_stderr(str(link), roots)