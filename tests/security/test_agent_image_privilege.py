"""C3 Task 1 (面 A): the agent image's install shape, and one real grant.

``tests/unit/test_priv_as_uid.py`` pins the four rules face A decides with, on
the host. What it *cannot* pin is everything below them: whether the image
actually carries the two file-capability binaries with the caps the plan names,
whether ``0710 root:<agent gid>`` survives the build, and whether a 65534
process holding only ``cap_setuid,cap_setgid`` can really write the identity
map of a pid somebody else unshared. That is this lane, and it needs a real
Linux kernel with user namespaces, so it builds and runs
``deploy/docker/Dockerfile.agent``:

    docker run --rm -v "$PWD:/w" -w /w e2b-sandlock-test:latest \\
        sh -c 'python3 -m pytest tests/security/test_agent_image_privilege.py -q'

The image is built from a minimal context (``c3_agent/priv/`` for the two
binaries, the Dockerfile, and -- since Task 2 -- the ``c3_agent`` package
the CMD runs plus its one ``gateway_common.env`` reader; the image needs nothing
else), and the grant runs against a
second container started from that same image with ``--pid=container:`` -- the
worker-side half of the production topology, where the agent sees the worker's
pids and the worker has no capability at all. The target it forks
``unshare(CLONE_NEWUSER)``-s and then polls ``setresuid(X)``, exactly as the
worker's slot does.

Two files' worth of pins live here:

* the agent image: ``/var/lib/e2b-priv`` holds **exactly** ``as_uid`` and
  ``e2b-maint``, at exactly ``cap_setuid,cap_setgid+ep`` /
  ``cap_chown,cap_dac_override+ep`` (``getcap`` verbatim), ``0710`` /
  ``0750``, root-owned with the agent's gid -- the property that makes a
  sandbox uid (a pooled uid, never in that group) unable to reach either;
* one **real** grant: the target becomes host uid X, X is what the kernel
  reports for it outside the namespace, and the target itself can then
  ``setresuid(X)``; plus the three refusals that need a real ``/proc``
  (pool, "already written", "not unshared").

The other half of the plan's judgement #2 -- that the **worker** image
(``deploy/docker/Dockerfile.envd``) has no ``/var/lib/e2b-priv`` at all -- is
C3 Task 4's deliverable, because that is the task that removes the binaries
from it; until then this lane pins the agent side only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_IMAGE = "e2b-sandlock-agent:c3-task1-test"

#: What the plan fixes for face A, to the byte.
PRIV_DIR = "/var/lib/e2b-priv"
AGENT_GID = 65534
DIR_MODE = "710"
FILE_MODE = "750"
AS_UID_CAPS = "cap_setuid,cap_setgid+ep"
MAINT_CAPS = "cap_chown,cap_dac_override+ep"
#: ``getcap`` prints the same xattr in libcap's canonical form: the ``+`` is
#: the *setcap* input operator, and the names come back in bit order. Both
#: spellings are pinned (this one from a real run, the one above as the
#: Dockerfile's own argument) so neither a changed set nor a changed syntax can
#: pass unnoticed.
AS_UID_CAPS_GETCAP = "cap_setgid,cap_setuid=ep"
MAINT_CAPS_GETCAP = "cap_chown,cap_dac_override=ep"
PRIV_BINARIES = ["as_uid", "e2b-maint"]

#: A uid from the pool the C side defaults to (E2B_UID_POOL_START/SIZE), so the
#: refusal lanes below and the grant lane agree on what "valid" means.
GRANT_UID = 10007

#: The worker-side half, as the worker would do it: a plain parent (a process
#: that has *not* unshared -- refusal ③'s target) and a child that unshares,
#: reports the pid it is known by, and then polls `setresuid(X)` exactly as the
#: slot does. Nobody hands it the identity: the grant is asynchronous by
#: design, and the child simply keeps trying until the map is there. It stays
#: alive afterwards so the outside view of the namespace and a second grant
#: attempt can still name the same pid.
TARGET_SCRIPT = r'''
import ctypes
import os
import sys
import time

CLONE_NEWUSER = 0x10000000
uid = int(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)


def say(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


say(f"C3-PLAIN pid={os.getpid()}")
child = os.fork()
if child == 0:
    if libc.unshare(CLONE_NEWUSER) != 0:
        say(f"C3-UNSHARE-FAILED errno={ctypes.get_errno()}")
        os._exit(1)
    say(f"C3-TARGET pid={os.getpid()}")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            os.setresuid(uid, uid, uid)
        except OSError:
            time.sleep(0.1)
            continue
        say(f"C3-SETRESUID-OK uid={os.getuid()} euid={os.geteuid()}")
        while True:
            time.sleep(1)
    say("C3-SETRESUID-TIMEOUT")
    os._exit(1)
_, status = os.waitpid(child, 0)
say(f"C3-PLAIN-DONE status={status}")
'''


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "info"], capture_output=True, text=True
    ).returncode == 0


needs_docker = pytest.mark.skipif(
    not _docker_ready(),
    reason=(
        "the C3 face-A image lane builds and runs Docker images (it needs a "
        "reachable Docker daemon; the container lanes mount the socket in)"
    ),
)


def _run(*args: str, timeout: float | None = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout
    )


@pytest.fixture(scope="module")
def agent_image(tmp_path_factory: pytest.TempPathFactory) -> str:
    """``deploy/docker/Dockerfile.agent`` built from a minimal context.

    The context carries exactly what the Dockerfile copies: the ``c3_agent``
    package (Task 2's control-plane channel -- CMD target -- plus ``priv/``, the
    C it compiles into the two binaries) and the single ``gateway_common.env``
    reader it imports -- and nothing else, so a context that grew would hide a
    missing COPY behind a stray file.
    """
    context = tmp_path_factory.mktemp("c3-agent-context")
    shutil.copy2(
        PROJECT_ROOT / "deploy" / "docker" / "Dockerfile.agent",
        context / "Dockerfile",
    )
    shutil.copytree(
        PROJECT_ROOT / "c3_agent", context / "c3_agent"
    )
    shutil.copytree(
        PROJECT_ROOT / "gateway_common", context / "gateway_common"
    )
    built = _run("docker", "build", "-t", AGENT_IMAGE, str(context))
    assert built.returncode == 0, f"{built.stdout}\n{built.stderr}"
    return AGENT_IMAGE


# ------------------------------------------------ the Dockerfile's own values


def test_the_dockerfile_declares_exactly_the_two_faces_capabilities() -> None:
    """The hard values, pinned where they are written (no daemon needed).

    Face A is `cap_setuid,cap_setgid+ep` and face B is C1's `chown` pair, with
    neither `SYS_ADMIN` nor `SYS_PTRACE` anywhere -- §14.2.7 measured that the
    owner rule makes both unnecessary. The default `USER` is 65534, so a
    container that forgets the DaemonSet's per-container override is powerless
    rather than root. Comments are excluded: the header explains the values it
    forbids, and that is not a declaration.
    """
    dockerfile = (
        PROJECT_ROOT / "deploy" / "docker" / "Dockerfile.agent"
    ).read_text(encoding="utf-8")
    code = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert [line for line in code if "setcap" in line] == [
        f"&& setcap {AS_UID_CAPS} /var/lib/e2b-priv/as_uid \\",
        f"&& setcap {MAINT_CAPS} /var/lib/e2b-priv/e2b-maint \\",
    ]
    assert [line for line in code if line.startswith("USER")] == [
        f"USER {AGENT_GID}:{AGENT_GID}"
    ]
    assert [
        line
        for line in code
        if any(term in line for term in ("SYS_ADMIN", "SYS_PTRACE", "privileged"))
    ] == []


# ------------------------------------------------------- the install shape


@needs_docker
def test_the_privileged_directory_holds_exactly_the_two_binaries(
    agent_image: str,
) -> None:
    """Judgement #2's other edge: the agent's own copy is a closed set.

    A third file here would be a third privileged thing nobody reviewed; the
    listing is asserted as a whole, not searched for the two names.
    """
    listed = _run(
        "docker",
        "run",
        "--rm",
        "--user",
        "0",
        "--entrypoint",
        "ls",
        agent_image,
        "-1",
        PRIV_DIR,
    )
    assert listed.returncode == 0, listed.stderr
    assert sorted(listed.stdout.split()) == ["as_uid", "e2b-maint"]


@needs_docker
def test_the_install_shape_is_root_owned_0710_with_0750_binaries(
    agent_image: str,
) -> None:
    """The barrier is DAC, not Landlock (`0710 root:<agent gid>`).

    A sandbox uid is a pooled uid (10000+) and is never in the agent's group,
    so it can neither traverse the directory nor exec either binary -- in every
    sandbox shape, which a Landlock-prefix argument cannot promise.
    """
    stat = _run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "stat",
        agent_image,
        "-c",
        "%n %u %g %a",
        PRIV_DIR,
        f"{PRIV_DIR}/as_uid",
        f"{PRIV_DIR}/e2b-maint",
    )
    assert stat.returncode == 0, stat.stderr
    assert stat.stdout.splitlines() == [
        f"{PRIV_DIR} 0 {AGENT_GID} {DIR_MODE}",
        f"{PRIV_DIR}/as_uid 0 {AGENT_GID} {FILE_MODE}",
        f"{PRIV_DIR}/e2b-maint 0 {AGENT_GID} {FILE_MODE}",
    ]


@needs_docker
def test_the_file_capabilities_are_exactly_the_expected_strings(
    agent_image: str,
) -> None:
    """Face A is `cap_setuid,cap_setgid+ep`; face B is C1's pair, unchanged.

    Verbatim from ``getcap`` (the image prints the same two lines at build
    time): not a superset, not a different set. ``SYS_ADMIN``/``SYS_PTRACE``
    are the shapes §14.2.7 measured and rejected -- the owner rule is what
    makes the write work, so the caps only ever have to name an id.
    """
    caps = _run(
        "docker",
        "run",
        "--rm",
        "--user",
        "0",
        "--entrypoint",
        "getcap",
        agent_image,
        f"{PRIV_DIR}/as_uid",
        f"{PRIV_DIR}/e2b-maint",
    )
    assert caps.returncode == 0, caps.stderr
    assert caps.stdout.splitlines() == [
        f"{PRIV_DIR}/as_uid {AS_UID_CAPS_GETCAP}",
        f"{PRIV_DIR}/e2b-maint {MAINT_CAPS_GETCAP}",
    ]


@needs_docker
def test_the_image_defaults_to_an_unprivileged_face_a_identity(
    agent_image: str,
) -> None:
    """The image's own USER is 65534 and holds no effective capabilities.

    The DaemonSet sets face B's `runAsUser: 0` per container; the default must
    therefore be the unprivileged one, so a container that forgets the override
    ends up powerless rather than root. `CapEff` is 0 even though the binaries
    carry file caps: nothing here inherits them.
    """
    uid = _run("docker", "run", "--rm", "--entrypoint", "id", agent_image, "-u")
    assert uid.returncode == 0, uid.stderr
    assert uid.stdout.strip() == str(AGENT_GID)
    gid = _run("docker", "run", "--rm", "--entrypoint", "id", agent_image, "-g")
    assert gid.returncode == 0, gid.stderr
    assert gid.stdout.strip() == str(AGENT_GID)
    caps = _run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        agent_image,
        "-c",
        "grep '^CapEff' /proc/self/status",
    )
    assert caps.returncode == 0, caps.stderr
    assert caps.stdout.split() == ["CapEff:", "0000000000000000"]


# ------------------------------------------------------------- the grant


def _start_pair(agent_image: str) -> SimpleNamespace:
    """The production topology in two containers, both from the agent image.

    The worker-shaped one runs as 65534 with **no** capabilities and only needs
    `seccomp=unconfined` for the unprivileged `unshare(CLONE_NEWUSER)`. The
    agent-shaped one joins its pid namespace (`--pid=container:`, which is what
    a `hostPID` DaemonSet gives face A) and declares
    `--cap-drop ALL --cap-add SETUID --cap-add SETGID`: the file capabilities
    must be inside the container's **bounding** set or the kernel refuses the
    exec, and nothing beyond the two is declared. Face A holds no effective
    capability from the declaration -- only the binary's file caps ever appear,
    at exec.
    """
    suffix = uuid.uuid4().hex[:8]
    worker = f"c3-worker-{suffix}"
    agent = f"c3-agent-{suffix}"
    common = (
        "--user",
        f"{AGENT_GID}:{AGENT_GID}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "seccomp=unconfined",
    )
    started = _run(
        "docker",
        "run",
        "-d",
        "--name",
        worker,
        *common,
        "--entrypoint",
        "python3",
        agent_image,
        "-c",
        TARGET_SCRIPT,
        str(GRANT_UID),
    )
    assert started.returncode == 0, started.stderr
    try:
        plain = int(_wait_for(worker, r"^C3-PLAIN pid=(\d+)$"))
        target = int(_wait_for(worker, r"^C3-TARGET pid=(\d+)$"))
        started = _run(
            "docker",
            "run",
            "-d",
            "--name",
            agent,
            *common,
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--pid",
            f"container:{worker}",
            "--entrypoint",
            "sleep",
            agent_image,
            "infinity",
        )
        assert started.returncode == 0, started.stderr
    except BaseException:
        _run("docker", "rm", "-f", worker, agent)
        raise
    return SimpleNamespace(
        worker=worker,
        agent=agent,
        plain_pid=plain,
        target_pid=target,
    )


def _stop_pair(pair: SimpleNamespace) -> None:
    _run("docker", "rm", "-f", pair.worker, pair.agent)


def _logs(container: str) -> str:
    logs = _run("docker", "logs", container)
    return logs.stdout + logs.stderr


def _wait_for(container: str, pattern: str, timeout: float = 60.0) -> str:
    """The first capture of ``pattern`` in the container's logs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        match = re.search(pattern, _logs(container), re.MULTILINE)
        if match is not None:
            return match.group(1)
        time.sleep(0.25)
    raise AssertionError(
        f"{container}: no {pattern!r} in its logs within {timeout}s:\n"
        f"{_logs(container)}"
    )


def _grant(
    pair: SimpleNamespace, uid: int, pid: int | None = None
) -> subprocess.CompletedProcess:
    return _run(
        "docker",
        "exec",
        pair.agent,
        f"{PRIV_DIR}/as_uid",
        "--uid",
        str(uid),
        "--pid",
        str(pair.target_pid if pid is None else pid),
    )


@needs_docker
def test_a_real_grant_hands_the_target_the_host_identity(agent_image: str) -> None:
    """The primitive, end to end: one line, one host identity, no root.

    Face A here is a 65534 process whose only capabilities are the two file
    ones, writing the map of a child it is neither the parent of nor in the
    same namespace as -- the shape §14.2.7 measured. The decisive assertion is
    the *outside* view: the kernel reports host uid X for the target, which no
    amount of in-namespace bookkeeping can fake.
    """
    pair = _start_pair(agent_image)
    try:
        granted = _grant(pair, GRANT_UID)
        assert granted.returncode == 0, granted.stderr
        assert granted.stdout == (
            f"C3-ASUID-OK pid={pair.target_pid} uid={GRANT_UID}\n"
        )
        assert granted.stderr == ""
        # The worker-side half, exactly as the slot does it.
        assert _wait_for(
            pair.worker, r"^(C3-SETRESUID-OK uid=\d+ euid=\d+)$"
        ) == f"C3-SETRESUID-OK uid={GRANT_UID} euid={GRANT_UID}"
        # And the outside view: the target *is* host uid X (its gid was never
        # granted, so it still reads 65534 -- the same pair the probe measured).
        outside = _run(
            "docker",
            "exec",
            pair.agent,
            "stat",
            "-c",
            "%u %g",
            f"/proc/{pair.target_pid}",
        )
        assert outside.returncode == 0, outside.stderr
        assert outside.stdout.strip() == f"{GRANT_UID} {AGENT_GID}"
    finally:
        _stop_pair(pair)


@needs_docker
def test_the_pool_and_not_unshared_refusals_fire_against_real_proc(
    agent_image: str,
) -> None:
    """① and ③, end to end: the rules that need a real target to be wrong.

    ③ in particular is only reachable with a real ``/proc``: the plain parent
    has never unshared, so its ``uid_map`` is the initial namespace's full
    range -- the state a caller that mixed up two pids would hand over.
    """
    pair = _start_pair(agent_image)
    try:
        pooled = _grant(pair, 9999)
        assert pooled.returncode == 77
        assert pooled.stdout == ""
        assert pooled.stderr == (
            "as_uid: refused: uid 9999 is outside the privileged helper uid "
            "pool 10000..10999\n"
        )
        plain = _grant(pair, GRANT_UID, pid=pair.plain_pid)
        assert plain.returncode == 77
        assert plain.stdout == ""
        assert plain.stderr == (
            f"as_uid: refused: uid_map for pid {pair.plain_pid} is the "
            "initial namespace's full range: this pid has not unshared a user "
            "namespace, so there is no new identity to grant\n"
        )
    finally:
        _stop_pair(pair)


@needs_docker
def test_a_second_grant_for_the_same_target_is_refused(agent_image: str) -> None:
    """② A namespace's map is written once -- and the refusal says so.

    The second call is the "agent retried after a timeout" shape: the first
    grant is already on the target, and writing again would be a second
    identity for one namespace. Fail closed, name the pid, and quote what the
    map holds (here, the identity the first grant wrote).
    """
    pair = _start_pair(agent_image)
    try:
        first = _grant(pair, GRANT_UID)
        assert first.returncode == 0, first.stderr
        again = _grant(pair, GRANT_UID)
        assert again.returncode == 77
        assert again.stdout == ""
        assert again.stderr == (
            f"as_uid: refused: uid_map for pid {pair.target_pid} already "
            f"carries a mapping ('{GRANT_UID} {GRANT_UID} 1'): a user "
            "namespace's map is written exactly once, and face A never "
            "rewrites one\n"
        )
    finally:
        _stop_pair(pair)
