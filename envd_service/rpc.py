"""Connect-RPC handlers for process.Process and filesystem.Filesystem."""

from __future__ import annotations

import base64
import json
import logging
import shlex
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request

from envd_service.connect.router import register_routes
from envd_service.executors.base import FailedRunningProcess
from envd_service.process.events import data_event, end_event, start_event
from envd_service.process.manager import ManagedProcess, parse_signal
from gateway_common.errors import (
    ConnectError,
    invalid_argument,
    not_found,
    unimplemented as connect_unimplemented,
)

logger = logging.getLogger(__name__)

# FUP #9: while a drifted record stays unreconciled, every command RPC would
# otherwise repeat the same WARNING. Log at most one drift warning per sandbox
# per window; ``_drift_warn_clock`` is injectable so unit tests can advance
# time without sleeping.
DRIFT_WARN_THROTTLE_SECONDS = 60.0
_drift_warn_clock = time.monotonic
_drift_warned_at: dict[str, float] = {}


def _context(request: Request, runtime) -> Any:
    """Get or create the per-sandbox runtime context."""
    runtimes = request.app.state.runtimes
    ctx = runtimes.get(runtime.sandbox_id)
    if ctx is None:
        ctx = request.app.state.context_factory(runtime)
        runtimes[runtime.sandbox_id] = ctx
    elif getattr(ctx, "_network", None) != runtime.network:
        # The control plane may have pushed a network update into the shared
        # runtime record; apply it to the live context so the next command
        # uses the new policy.
        updater = getattr(ctx, "update_network", None)
        if updater is not None:
            from gateway_common.network import NetworkUpdateConflictError

            try:
                updater(runtime.network)
            except NetworkUpdateConflictError as exc:
                # D4=A: an out-of-band record change that the launched
                # instance cannot express must not break the command path;
                # the live runtime policy stays authoritative until the
                # record is reconciled. FUP #9 throttles the WARNING to one
                # per sandbox per DRIFT_WARN_THROTTLE_SECONDS.
                now = _drift_warn_clock()
                last_warn = _drift_warned_at.get(runtime.sandbox_id)
                if (
                    last_warn is None
                    or now - last_warn >= DRIFT_WARN_THROTTLE_SECONDS
                ):
                    logger.warning(
                        "drift network update for sandbox %s is not "
                        "expressible on the live instance (%s); keeping "
                        "runtime policy",
                        runtime.sandbox_id,
                        exc,
                    )
                    _drift_warned_at[runtime.sandbox_id] = now
    return ctx


def _decode_bytes(value: Any, field: str) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=False)
        except (ValueError, TypeError):
            raise invalid_argument(f"{field} must be base64") from None
    raise invalid_argument(f"{field} must be a base64 string")


def _require_str(payload: dict, key: str, default: str = "") -> str:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise invalid_argument(f"{key} must be a string")
    return value


async def _consume_stream(
    proc: ManagedProcess, queue: Any
) -> AsyncIterator[dict[str, Any]]:
    try:
        while True:
            item = await queue.get()
            kind = item[0]
            if kind == "data":
                yield data_event(item[1], item[2])
            elif kind == "end":
                yield end_event(item[1], item[2])
                return
    finally:
        proc.unsubscribe(queue)


def build_process_handlers() -> tuple[dict[str, Any], dict[str, Any]]:
    async def rpc_list(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        return {"processes": ctx.processes.list()}

    async def rpc_start(request: Request, payload: dict, runtime):
        ctx = _context(request, runtime)
        process = payload.get("process") or {}
        if not isinstance(process, dict):
            raise invalid_argument("process must be an object")
        cmd = process.get("cmd") or ""
        args = process.get("args") or []
        if not isinstance(cmd, str) or not cmd:
            raise invalid_argument("process.cmd is required")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise invalid_argument("process.args must be a list of strings")
        envs = process.get("envs") or {}
        if not isinstance(envs, dict):
            raise invalid_argument("process.envs must be an object")
        cwd = process.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = runtime.workspace_dir
        elif not cwd.startswith("/"):
            from pathlib import Path

            cwd = str(Path(runtime.workspace_dir) / cwd)
        stdin = payload.get("stdin", False)
        pty_size = None
        pty = payload.get("pty")
        if pty is not None:
            size = (pty or {}).get("size") or {}
            rows = size.get("rows", 24)
            cols = size.get("cols", 80)
            pty_size = (int(rows), int(cols))
        tag = payload.get("tag")
        process_env = {str(k): str(v) for k, v in envs.items()}
        merged_env = dict(runtime.env_vars)
        merged_env.update(process_env)

        # MCP: the SDK runs ``mcp-gateway --config <json>`` after
        # ``Sandbox.create(mcp=...)``. The gateway must outlive the command, so
        # envd starts it as a managed long-running process and reports an
        # immediate exit-0 to the SDK.
        if "mcp-gateway" in (cmd + " " + " ".join(args)):
            gateway_token = merged_env.get("GATEWAY_ACCESS_TOKEN")
            if gateway_token:
                mcp_config = _extract_mcp_config(args)
                if mcp_config is None:
                    raise invalid_argument(
                        "mcp-gateway requires --config <json>"
                    )
                try:
                    proc = await ctx.start_mcp_gateway(mcp_config, gateway_token)
                except Exception as e:
                    proc = FailedRunningProcess(
                        f"envd: failed to start MCP gateway: {e}\n"
                    )

                async def mcp_gen() -> AsyncIterator[dict[str, Any]]:
                    yield start_event(proc.pid)
                    if isinstance(proc, FailedRunningProcess):
                        async for chunk in proc.output():
                            if chunk[0] in ("stdout", "stderr"):
                                yield data_event(chunk[0], chunk[1])
                        yield end_event(await proc.exit_code(), "exited")
                    else:
                        # The gateway keeps running as a managed sandbox
                        # process; the SDK command completes immediately.
                        yield end_event(0, "exited")

                return mcp_gen()

        # FUP #4 (Task D1): a gateway that died at startup must be visible to
        # the SDK instead of every later command looking healthy. The watcher
        # records the death as a typed ``McpGatewayFailure`` on the runtime
        # context; this branch replays it *before exec* -- the whole stderr is
        # the recorded text verbatim and the exit code is the gateway's own.
        # The record's presence is the entire test: no substring matching and
        # no broad ``except`` classification.
        failure = getattr(ctx, "mcp_gateway_failure", None)
        if failure is not None:
            failed = FailedRunningProcess(f"{failure.text}\n")

            async def failed_gen() -> AsyncIterator[dict[str, Any]]:
                yield start_event(failed.pid)
                async for kind, chunk in failed.output():
                    if kind in ("stdout", "stderr"):
                        yield data_event(kind, chunk)
                yield end_event(failure.exit_code, "exited")

            return failed_gen()

        proc = await ctx.processes.start(
            cmd=[cmd, *args],
            env=merged_env,
            cwd=cwd,
            stdin_enabled=bool(stdin),
            pty_size=pty_size,
            tag=tag if isinstance(tag, str) else None,
        )
        queue = proc.subscribe(replay=False)

        async def gen() -> AsyncIterator[dict[str, Any]]:
            yield start_event(proc.pid)
            async for event in _consume_stream(proc, queue):
                yield event

        return gen()


    async def rpc_connect(request: Request, payload: dict, runtime):
        ctx = _context(request, runtime)
        process = payload.get("process") or {}
        pid = _pid(process)
        proc = ctx.processes.get(pid)
        queue = proc.subscribe(replay=True)

        async def gen() -> AsyncIterator[dict[str, Any]]:
            yield start_event(proc.pid)
            async for event in _consume_stream(proc, queue):
                yield event

        return gen()

    async def rpc_update(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        process = payload.get("process") or {}
        pid = _pid(process)
        pty = payload.get("pty") or {}
        size = pty.get("size") or {}
        ctx.processes.update(
            pid,
            rows=int(size.get("rows", 24)),
            cols=int(size.get("cols", 80)),
        )
        return {}

    async def rpc_send_input(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        process = payload.get("process") or {}
        pid = _pid(process)
        inp = payload.get("input") or {}
        data = _decode_bytes(inp.get("stdin") or inp.get("pty"), "input")
        ctx.processes.send_input(pid, data)
        return {}

    async def rpc_send_signal(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        process = payload.get("process") or {}
        pid = _pid(process)
        sig = parse_signal(payload.get("signal"))
        ctx.processes.send_signal(pid, sig)
        return {}

    async def rpc_close_stdin(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        process = payload.get("process") or {}
        pid = _pid(process)
        ctx.processes.close_stdin(pid)
        return {}

    async def rpc_stream_input(request: Request, payload: dict, runtime):
        raise connect_unimplemented("StreamInput is reserved by the official proto")

    unary = {
        "process.Process/List": rpc_list,
        "process.Process/Update": rpc_update,
        "process.Process/SendInput": rpc_send_input,
        "process.Process/SendSignal": rpc_send_signal,
        "process.Process/CloseStdin": rpc_close_stdin,
    }
    stream = {
        "process.Process/Start": rpc_start,
        "process.Process/Connect": rpc_connect,
        "process.Process/StreamInput": rpc_stream_input,
    }
    return unary, stream


def _extract_mcp_config(args: list[str]) -> dict | None:
    """Extract the JSON config from ``mcp-gateway --config <json>`` args."""
    line = " ".join(args)
    if "--config" not in line:
        return None
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    if "mcp-gateway" in tokens:
        tokens = tokens[tokens.index("mcp-gateway") + 1 :]
    if "--config" in tokens:
        idx = tokens.index("--config")
        if idx + 1 < len(tokens):
            try:
                parsed = json.loads(tokens[idx + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _pid(process: Any) -> int:
    if not isinstance(process, dict):
        raise invalid_argument("process selector must be an object")
    pid = process.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise invalid_argument("process selector requires an integer pid")
    return pid


def build_filesystem_handlers() -> tuple[dict[str, Any], dict[str, Any]]:
    async def rpc_stat(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        return {"entry": ctx.files.stat(_require_str(payload, "path"))}

    async def rpc_make_dir(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        return {"entry": ctx.files.make_dir(_require_str(payload, "path"))}

    async def rpc_move(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        return {
            "entry": ctx.files.move(
                _require_str(payload, "source"),
                _require_str(payload, "destination"),
            )
        }

    async def rpc_remove(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        ctx.files.remove(_require_str(payload, "path"))
        return {}

    async def rpc_list_dir(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        depth = payload.get("depth", 1)
        if depth is None:
            depth = 1
        depth = int(depth)
        if depth < 0:
            raise invalid_argument("depth must be non-negative")
        result = ctx.files.list_dir(_require_str(payload, "path"), depth)
        return {"entries": result["entries"]}

    async def rpc_watch_dir(request: Request, payload: dict, runtime):
        ctx = _context(request, runtime)
        return await ctx.watch_stream.watch(
            _require_str(payload, "path"),
            recursive=bool(payload.get("recursive", False)),
            include_entry=bool(payload.get("includeEntry", False)),
        )

    async def rpc_create_watcher(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        wid = ctx.watchers.create(
            _require_str(payload, "path"),
            recursive=bool(payload.get("recursive", False)),
            include_entry=bool(payload.get("includeEntry", False)),
        )
        return {"watcherId": wid}

    async def rpc_get_watcher_events(
        request: Request, payload: dict, runtime
    ) -> dict[str, Any]:
        ctx = _context(request, runtime)
        return ctx.watchers.events(_require_str(payload, "watcherId"))

    async def rpc_remove_watcher(request: Request, payload: dict, runtime) -> dict[str, Any]:
        ctx = _context(request, runtime)
        ctx.watchers.remove(_require_str(payload, "watcherId"))
        return {}

    unary = {
        "filesystem.Filesystem/Stat": rpc_stat,
        "filesystem.Filesystem/MakeDir": rpc_make_dir,
        "filesystem.Filesystem/Move": rpc_move,
        "filesystem.Filesystem/Remove": rpc_remove,
        "filesystem.Filesystem/ListDir": rpc_list_dir,
        "filesystem.Filesystem/CreateWatcher": rpc_create_watcher,
        "filesystem.Filesystem/GetWatcherEvents": rpc_get_watcher_events,
        "filesystem.Filesystem/RemoveWatcher": rpc_remove_watcher,
    }
    stream = {
        "filesystem.Filesystem/WatchDir": rpc_watch_dir,
    }
    return unary, stream


def register_rpc(app: Any) -> None:
    process_unary, process_stream = build_process_handlers()
    fs_unary, fs_stream = build_filesystem_handlers()
    register_routes(
        app,
        unary={**process_unary, **fs_unary},
        stream={**process_stream, **fs_stream},
        require_sandbox=True,
    )
