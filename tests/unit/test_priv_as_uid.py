"""C3 Task 1 (面 A): ``as_uid`` -- the identity-granting primitive, off-Linux.

``as_uid`` is the ONE privileged thing face A of the C3 per-node agent ships
(``c3_agent/priv/as_uid.c``, later driven by Task 2/3 from the control plane). It
writes one identity mapping ``X X 1`` into the ``uid_map``/``gid_map`` of a pid
the worker has just ``unshare(CLONE_NEWUSER)``-ed, and it is deliberately not a
general "write any map" tool: everything it may *choose* is decided by four
rules, and each of them is refused by name **before** anything is written.

Those four rules are decisions over **text** -- the uid it was asked for, and
the bytes the target's maps currently hold -- so this file pins them on the
host, where there is no ``/proc/<pid>/uid_map``, no ``CLONE_NEWUSER`` and no
file capability to run the real thing:

① the uid is outside the configured pool (``priv_validate_uid`` -- shared with
   both brokers, never uid 0);
② the target's map already carries a mapping (a user namespace's map is
   written exactly once, so a second writer means somebody else granted an
   identity);
③ the target has not unshared at all (its map is the initial namespace's full
   range), so there is no new namespace to map;
④ the bytes about to be written are not the identity ``X X 1``.

The container lane (``tests/security/test_agent_image_privilege.py``) pins the
rest: the file capabilities, the install shape, and one real grant against a
real unshared child. Splitting it this way is the same call the brokers made in
``tests/unit/test_priv_helpers.py``: the rules stay observable off-Linux, and
the kernel-facing half is pinned where the kernel is.

The host half compiles the **production** translation unit -- not a
reimplementation of it -- through a tiny driver (``DRIVER_C`` below) that links
``as_uid.c`` with ``AS_UID_NO_MAIN`` and exposes its pure entry points, so the
lane exercises the same translation unit the agent image installs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIV_DIR = PROJECT_ROOT / "c3_agent" / "priv"
AS_UID_C = PRIV_DIR / "as_uid.c"

#: The pool this file configures the C side with, explicitly: the default
#: (10000/1000) is asserted below rather than assumed, but every expectation
#: here spells the numbers out so an ambient ``E2B_UID_POOL_*`` cannot move the
#: boundary the assertions are about.
POOL_START = 10000
POOL_SIZE = 1000
POOL_END = POOL_START + POOL_SIZE - 1

#: The initial user namespace's own ``uid_map``, as a reader inside it sees it
#: (measured 2026-09-28 in a Linux container: ``cat /proc/self/uid_map`` ->
#: ``         0          0 4294967295``). A pid whose map reads like this has
#: not unshared, which is refusal ③.
INITIAL_MAP = "         0          0 4294967295\n"

#: What the kernel prints for a map that *has* been written -- here, the one
#: this same program writes. Refusal ②.
WRITTEN_MAP = "     10007      10007          1\n"

#: The one-line success output. ``main`` prints it on stdout and nothing else,
#: so a caller can match on it without parsing; pinned here through the same
#: function ``main`` uses.
OK_LINE = "C3-ASUID-OK pid={pid} uid={uid}\n"

#: A driver around the production translation unit: ``#define AS_UID_NO_MAIN``
#: takes the binary's ``main`` out, and the compiler then sees ``as_uid.c``'s
#: own definitions, so a changed signature cannot compile here while the
#: expectations below keep passing. Subcommands print exactly one line; nothing
#: is read from the environment except the uid pool the C side itself reads.
DRIVER_C = r"""
#define AS_UID_NO_MAIN 1
#include "as_uid.c"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int usage(void) {
    fprintf(stderr,
            "usage: driver pool <uid> | map <name> <pid> <content> | "
            "identity <line> | line <uid> | ok-line <pid> <uid>\n");
    return 2;
}

int main(int argc, char **argv) {
    char err[PRIV_ERR_LEN];
    char line[64];
    long id = -1;

    if (argc < 3) {
        return usage();
    }
    if (strcmp(argv[1], "pool") == 0) {
        long uid;
        if (argc != 3) {
            return usage();
        }
        if (priv_parse_uid(argv[2], &uid, err, sizeof err) != 0 ||
            priv_validate_uid(uid, err, sizeof err) != 0) {
            printf("REFUSED %s\n", err);
            return 0;
        }
        printf("OK %ld\n", uid);
        return 0;
    }
    if (strcmp(argv[1], "map") == 0) {
        if (argc != 5) {
            return usage();
        }
        if (as_uid_check_map(argv[2], strtol(argv[3], NULL, 10), argv[4], err,
                             sizeof err) != 0) {
            printf("REFUSED %s\n", err);
            return 0;
        }
        printf("OK\n");
        return 0;
    }
    if (strcmp(argv[1], "identity") == 0) {
        if (argc != 3) {
            return usage();
        }
        if (as_uid_check_identity_line(argv[2], &id, err, sizeof err) != 0) {
            printf("REFUSED %s\n", err);
            return 0;
        }
        printf("OK %ld\n", id);
        return 0;
    }
    if (strcmp(argv[1], "line") == 0) {
        if (argc != 3) {
            return usage();
        }
        if (as_uid_identity_line(strtol(argv[2], NULL, 10), line, sizeof line) !=
            0) {
            return usage();
        }
        fputs(line, stdout);
        return 0;
    }
    if (strcmp(argv[1], "ok-line") == 0) {
        if (argc != 4) {
            return usage();
        }
        as_uid_ok_line(strtol(argv[2], NULL, 10), strtol(argv[3], NULL, 10),
                       line, sizeof line);
        fputs(line, stdout);
        return 0;
    }
    return usage();
}
"""


@pytest.fixture(scope="module")
def driver(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``as_uid.c`` + ``priv_common.c`` built the way the image builds them.

    ``-Wall -Wextra`` clean and an empty ``stderr`` is the bar the other
    ``c3_agent/priv`` lanes hold themselves to, and the build is what makes these
    expectations test *this* revision instead of a copy of the rules.
    """
    cc = shutil.which("cc")
    if cc is None:
        raise RuntimeError(
            "the face-A lane compiles c3_agent/priv/as_uid.c on the host and "
            "needs a C compiler (`cc`); a skipped lane reads exactly like a "
            "passing one"
        )
    assert AS_UID_C.is_file(), f"{AS_UID_C} is missing"
    out = tmp_path_factory.mktemp("c3-as-uid")
    source = out / "driver.c"
    source.write_text(DRIVER_C, encoding="utf-8")
    binary = out / "as_uid_driver"
    build = subprocess.run(
        [
            cc,
            "-O2",
            "-Wall",
            "-Wextra",
            "-I",
            str(PRIV_DIR),
            "-o",
            str(binary),
            str(source),
            str(PRIV_DIR / "priv_common.c"),
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    assert build.stderr == ""
    return binary


def _drive(driver: Path, *args: str) -> str:
    """One driver invocation; its whole stdout, which must be one line."""
    env = dict(os.environ)
    env["E2B_UID_POOL_START"] = str(POOL_START)
    env["E2B_UID_POOL_SIZE"] = str(POOL_SIZE)
    done = subprocess.run(
        [str(driver), *args], capture_output=True, text=True, env=env
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


# ------------------------------------------------------------ the uid pool (①)


def test_a_uid_outside_the_configured_pool_is_refused(driver: Path) -> None:
    """① Face A grants a *pooled* uid, and the pool rule is the brokers'.

    ``priv_validate_uid`` is the same function ``e2b-maint`` and
    ``e2b-slot-spawn`` refuse with, so the identity grantor cannot drift from
    the pool the rest of the node hands out.
    """
    assert _drive(driver, "pool", str(POOL_START)) == f"OK {POOL_START}\n"
    assert _drive(driver, "pool", str(POOL_END)) == f"OK {POOL_END}\n"
    assert _drive(driver, "pool", "9999") == (
        "REFUSED uid 9999 is outside the privileged helper uid pool "
        "10000..10999\n"
    )
    assert _drive(driver, "pool", "11000") == (
        "REFUSED uid 11000 is outside the privileged helper uid pool "
        "10000..10999\n"
    )
    # uid 0 is not "outside the pool", it is not an identity at all: the
    # shared parse refuses it one step earlier, and by name.
    assert _drive(driver, "pool", "0") == (
        "REFUSED uid/gid must be positive (got 0)\n"
    )


def test_the_pool_boundary_is_the_configured_one(driver: Path) -> None:
    """The pool is read, not compiled in: a site that moves it moves face A."""
    env = dict(os.environ)
    env["E2B_UID_POOL_START"] = "20000"
    env["E2B_UID_POOL_SIZE"] = "10"
    done = subprocess.run(
        [str(driver), "pool", "20009"], capture_output=True, text=True, env=env
    )
    assert done.stdout == "OK 20009\n"
    done = subprocess.run(
        [str(driver), "pool", "10000"], capture_output=True, text=True, env=env
    )
    assert done.stdout == (
        "REFUSED uid 10000 is outside the privileged helper uid pool "
        "20000..20009\n"
    )


# --------------------------------------------------- the target's map state ②③


def test_an_unwritten_map_is_the_only_one_face_a_writes(driver: Path) -> None:
    """The ready state: a target that unshared and has no mapping yet.

    The kernel answers an empty file for exactly that state, and the whole
    point of face A is that this is the only map it will ever touch.
    """
    assert _drive(driver, "map", "uid_map", "4242", "") == "OK\n"
    assert _drive(driver, "map", "gid_map", "4242", "") == "OK\n"


def test_a_target_whose_map_is_already_written_is_refused(driver: Path) -> None:
    """② A user namespace's map is written exactly once.

    A non-empty map means somebody already granted this target an identity --
    another agent, a retry that raced, or a subject that is not the namespace
    the caller thought it was. The refusal names the pid, the map and what it
    read, so a "slot starts, then dies" report can be told from a double grant.
    """
    assert _drive(driver, "map", "uid_map", "4242", WRITTEN_MAP) == (
        "REFUSED uid_map for pid 4242 already carries a mapping "
        "('10007 10007 1'): a user namespace's map is written exactly once, "
        "and face A never rewrites one\n"
    )
    # The gid side is judged the same way and named separately: a uid_map that
    # is still empty next to a written gid_map is a half-granted namespace, and
    # writing the uid half would not repair it.
    assert _drive(driver, "map", "gid_map", "4242", WRITTEN_MAP) == (
        "REFUSED gid_map for pid 4242 already carries a mapping "
        "('10007 10007 1'): a user namespace's map is written exactly once, "
        "and face A never rewrites one\n"
    )


def test_a_target_that_has_not_unshared_is_refused(driver: Path) -> None:
    """③ The initial namespace's full range is not an unshared namespace.

    Writing here would be the "map a pid in the caller's own namespace" step
    that every userns bug is made of, and it is also the state a worker's
    *unshared*-but-not-yet-reported child never shows.
    """
    assert _drive(driver, "map", "uid_map", "4242", INITIAL_MAP) == (
        "REFUSED uid_map for pid 4242 is the initial namespace's full range: "
        "this pid has not unshared a user namespace, so there is no new "
        "identity to grant\n"
    )


def test_a_map_that_is_not_understood_is_never_treated_as_empty(
    driver: Path,
) -> None:
    """Fail closed on anything that is not a map this program can read.

    "We could not parse it" must never collapse into "so it is free": the
    kernel would refuse the write anyway, but by then the caller has already
    been told the target was in the ready state.
    """
    for content, quoted in (
        # A truncated line.
        ("0 0", "0 0"),
        # Not a map at all.
        ("not a map\n", "not a map"),
        # A count past the kernel's own u32 ceiling.
        ("10000 10000 4294967296\n", "10000 10000 4294967296"),
        # A negative field.
        ("-1 0 1\n", "-1 0 1"),
    ):
        assert _drive(driver, "map", "uid_map", "4242", content) == (
            "REFUSED uid_map for pid 4242 is not a map this program can read "
            f"('{quoted}'): a fresh namespace's map is empty, so this pid is "
            "not the one face A was asked to grant\n"
        )


# ------------------------------------------------------ the identity rule (④)


def test_a_non_identity_mapping_is_refused(driver: Path) -> None:
    """④ Face A writes the identity, and *only* the identity.

    ``0 X 1`` is the mapping from the probe's control arm (and the one a
    "let the agent write whatever the control plane says" shape would drift
    into): it hands the container's root the host uid X, i.e. it *is* the
    identity grant, one namespace up. The production binary computes its line
    from ``--uid X`` and passes it through this same check before writing, so
    the rule is on the write path, not only here.
    """
    assert _drive(driver, "identity", "0 10000 1") == (
        "REFUSED the mapping '0 10000 1' is not the identity 'X X 1' face A "
        "writes\n"
    )
    assert _drive(driver, "identity", "10000 10001 1") == (
        "REFUSED the mapping '10000 10001 1' is not the identity 'X X 1' "
        "face A writes\n"
    )
    assert _drive(driver, "identity", "10000 10000 2") == (
        "REFUSED the mapping '10000 10000 2' must map exactly one id\n"
    )
    # A second extent is not an identity map either, whatever its first line
    # says -- and it must not be judged by its first line alone.
    assert _drive(
        driver, "identity", "10000 10000 1\n10001 10001 1\n"
    ) == (
        "REFUSED the mapping '10000 10000 1 10001 10001 1' must map exactly "
        "one id\n"
    )
    # The identity *of root* is still not an identity face A may grant: the
    # whole point of the pool is that a sandbox never becomes uid 0.
    assert _drive(driver, "identity", "0 0 1") == (
        "REFUSED the mapping '0 0 1' names uid 0: face A hands a pooled uid, "
        "never root's\n"
    )


# ------------------------------------------------------ the granted identity


def test_the_granted_identity_is_the_one_the_worker_asked_for(
    driver: Path,
) -> None:
    """The whole output of a successful grant is one line, and this is it."""
    assert _drive(driver, "line", "10000") == "10000 10000 1\n"
    assert _drive(driver, "line", "10999") == "10999 10999 1\n"
    assert _drive(driver, "identity", "10999 10999 1\n") == "OK 10999\n"
    assert _drive(driver, "identity", "10000 10000 1") == "OK 10000\n"
    assert _drive(driver, "ok-line", "4242", "10000") == (
        OK_LINE.format(pid=4242, uid=10000)
    )


def test_the_line_this_program_writes_passes_its_own_check(driver: Path) -> None:
    """The producer and the validator are the same rule, so they cannot drift.

    ``as_uid_identity_line`` is what ``main`` hands the kernel and
    ``as_uid_check_identity_line`` is what ``main`` checks it with; a pool uid
    formatted into anything else would be refused by face A itself rather than
    written.
    """
    for uid in (POOL_START, 10555, POOL_END):
        line = _drive(driver, "line", str(uid))
        assert _drive(driver, "identity", line) == f"OK {uid}\n"
