"""Flake #1: buildkitd's config must not depend on a host bind-mount source.

The fixture used to write ``buildkitd.toml`` through the test container's
``/workspace`` view of the tree and then bind-mount
``$E2B_HOST_PROJECT/tmp/<name>/buildkitd.toml``. A bind mount is resolved by the
HOST daemon, so whenever the two paths are not the same directory -- every
``git archive`` snapshot run of the lanes, for one -- the daemon cannot see the
source and creates an empty *directory* in its place; buildkitd then exits with
``read .../buildkitd.toml: is a directory``, which cost 24 tests a silent skip
in ``tests/contract`` and 9 failures in ``tests/sdk``.

The daemon now receives the config through ``docker cp`` (bytes travel through
the daemon API, no host path is resolved) and the fixture verifies both ends:
the daemon listens on the address only *this* config asks for, and the file
inside the container is exactly the config we copied. This test pins the
regression shape directly by pointing ``E2B_HOST_PROJECT`` at a directory that
is not the tree under test.
"""

from __future__ import annotations

import socket

from tests.conftest import _start_buildkitd, _stop_buildkitd


def test_buildkitd_serves_when_the_host_project_path_is_not_the_tree(
    monkeypatch, tmp_path
) -> None:
    """``E2B_HOST_PROJECT`` must not matter: the config is injected, not mounted.

    Before the fix this is exactly the failing shape (the old fixture resolved
    its mount source under ``E2B_HOST_PROJECT``): docker created
    ``tmp/buildkit-test-*/buildkitd.toml`` as a directory and buildkitd died on
    ``is a directory``. Readiness below is the daemon answering on the TCP
    address from the injected config, i.e. it parsed that file.
    """
    unrelated = tmp_path / "a-host-project-path-that-is-not-the-tree"
    unrelated.mkdir()
    monkeypatch.setenv("E2B_HOST_PROJECT", str(unrelated))

    address, container, cfg_dir = _start_buildkitd()
    try:
        scheme, _, hostport = address.partition("://")
        host, _, port = hostport.partition(":")
        assert scheme == "tcp"
        assert host == "127.0.0.1"
        with socket.create_connection((host, int(port)), timeout=5) as conn:
            # A real TCP client (buildkit's own client does the same) reaches
            # the daemon that read the injected config.
            assert conn.getpeername()[1] == int(port)
    finally:
        _stop_buildkitd(container, cfg_dir)
