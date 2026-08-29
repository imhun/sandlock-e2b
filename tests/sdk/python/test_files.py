"""Filesystem operations via the official e2b SDK."""

from __future__ import annotations

import hashlib
import os
import time

import pytest

from e2b.sandbox.filesystem.filesystem import EntryInfo, FileType


def test_files_roundtrip(sandbox):
    info = sandbox.files.write("workspace/a.txt", "hello")
    assert info.name == "a.txt"
    assert info.path == "workspace/a.txt"
    assert info.type == FileType.FILE
    assert sandbox.files.read("workspace/a.txt") == "hello"
    assert sandbox.files.exists("workspace/a.txt") is True
    sandbox.files.remove("workspace/a.txt")
    assert sandbox.files.exists("workspace/a.txt") is False


def test_files_binary_roundtrip(sandbox):
    data = bytes(range(256)) * 100
    sandbox.files.write("workspace/bin.dat", data)
    assert bytes(sandbox.files.read("workspace/bin.dat", format="bytes")) == data
    sandbox.files.remove("workspace/bin.dat")


def test_files_list_and_get_info(sandbox):
    sandbox.files.write("workspace/dir/nested.txt", "x")
    entries = sandbox.files.list("workspace")
    assert any(e.name == "dir" and e.type == FileType.DIR for e in entries)
    info = sandbox.files.get_info("workspace/dir/nested.txt")
    assert info.name == "nested.txt"
    assert info.type == FileType.FILE
    assert info.size == 1
    assert info.path == "workspace/dir/nested.txt"


def test_files_rename_and_make_dir(sandbox):
    sandbox.files.write("workspace/old.txt", "data")
    info = sandbox.files.rename("workspace/old.txt", "workspace/new.txt")
    assert info.path == "workspace/new.txt"
    assert sandbox.files.exists("workspace/old.txt") is False
    assert sandbox.files.read("workspace/new.txt") == "data"

    assert sandbox.files.make_dir("workspace/a/b/c") is True
    assert sandbox.files.make_dir("workspace/a/b/c") is False
    assert sandbox.files.exists("workspace/a/b/c") is True


def test_files_missing_get_info_raises(sandbox):
    from e2b.exceptions import FileNotFoundException

    with pytest.raises(FileNotFoundException):
        sandbox.files.get_info("workspace/missing.txt")


def test_large_file_hash(sandbox):
    payload = os.urandom(5 * 1024 * 1024)
    sandbox.files.write("workspace/big.bin", payload)
    got = bytes(sandbox.files.read("workspace/big.bin", format="bytes"))
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(payload).hexdigest()
    assert len(got) == len(payload)
    sandbox.files.remove("workspace/big.bin")


def test_files_watch_dir(sandbox):
    sandbox.files.make_dir("workspace/watch")
    handle = sandbox.files.watch_dir("workspace/watch")
    try:
        sandbox.files.write("workspace/watch/evt.txt", "x")
        deadline = time.time() + 5
        events = []
        while time.time() < deadline:
            events = handle.get_new_events()
            if any(e.type.value == "create" for e in events):
                break
            time.sleep(0.1)
        assert any(e.type.value == "create" and e.name == "evt.txt" for e in events)
    finally:
        handle.stop()


@pytest.mark.asyncio
async def test_async_files_roundtrip(async_sandbox):
    info = await async_sandbox.files.write("workspace/async.txt", "hello-async")
    assert info.name == "async.txt"
    assert (await async_sandbox.files.read("workspace/async.txt")) == "hello-async"
    assert (await async_sandbox.files.exists("workspace/async.txt")) is True
    await async_sandbox.files.remove("workspace/async.txt")
    assert (await async_sandbox.files.exists("workspace/async.txt")) is False
