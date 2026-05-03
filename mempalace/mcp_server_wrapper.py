#!/usr/bin/env python3
"""
MCP Server Subprocess Wrapper — Plan A crash recovery for mempalace MCP bridge
===============================================================================

Spawns mempalace.mcp_server as a subprocess and handles crashes by restarting
it. Designed for the nova-cc MCP bridge path where crashes occur on subsequent
tool calls (particularly mempalace_status operations at ~2s).

The wrapper maintains the JSON-RPC protocol flow while providing crash recovery:
- Detects subprocess crashes (SEGV, unexpected exits)
- Restarts the MCP server subprocess automatically
- Re-initializes the MCP protocol after restart
- Preserves tool call semantics for Claude Code clients
- Logs crash events for root cause investigation

Usage:
    python -m mempalace.mcp_server_wrapper [--palace /path/to/palace]

Environment:
    MEMPALACE_WRAPPER_RESTART_LIMIT=5 — max restarts before giving up (default: 3)
    MEMPALACE_WRAPPER_RESTART_BACKOFF=0.5 — seconds between restarts (default: 0.1)
    MEMPALACE_SKIP_PIN=1 — passed through to subprocess (default: 1)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Configuration
_MAX_RESTARTS = int(os.environ.get("MEMPALACE_WRAPPER_RESTART_LIMIT", "3"))
_RESTART_BACKOFF = float(os.environ.get("MEMPALACE_WRAPPER_RESTART_BACKOFF", "0.1"))
_SUBPROCESS_TIMEOUT = 30.0

# Track restarts for observability
_restart_count = 0
_last_restart_time = 0.0


class MCPServerWrapper:
    """Subprocess wrapper for mempalace MCP server with crash recovery."""

    def __init__(self, palace_path: str | None = None):
        self.palace_path = palace_path
        self.proc: asyncio.subprocess.Process | None = None
        self.req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_lines: list[str] = []
        self._initialized = False
        self._restart_count = 0

    async def __aenter__(self) -> MCPServerWrapper:
        await self._start_subprocess()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._cleanup()

    async def _start_subprocess(self) -> None:
        """Start or restart the MCP server subprocess."""
        global _restart_count, _last_restart_time

        if self.proc and self.proc.returncode is None:
            await self._cleanup()

        # Build argv for subprocess
        argv = [sys.executable, "-m", "mempalace.mcp_server"]
        if self.palace_path:
            argv.extend(["--palace", self.palace_path])

        # Environment with skip-pin enabled
        env = {**os.environ, "MEMPALACE_SKIP_PIN": "1"}

        logger.info(
            "starting mempalace MCP server subprocess",
            extra={
                "restart_count": self._restart_count,
                "palace_path": self.palace_path,
                "argv": argv,
            }
        )

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except Exception as exc:
            logger.error(f"failed to start MCP server subprocess: {exc}")
            raise

        # Start reader tasks
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

        # Initialize MCP protocol
        await self._initialize()
        self._initialized = True

        if self._restart_count > 0:
            _restart_count += 1
            _last_restart_time = time.time()

    async def _cleanup(self) -> None:
        """Clean up subprocess and tasks."""
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task

        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task

        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                self.proc.kill()
                with contextlib.suppress(ProcessLookupError):
                    await self.proc.wait()

        # Fail any pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("MCP server wrapper shutting down"))
        self._pending.clear()
        self._initialized = False

    def _next_id(self) -> int:
        self.req_id += 1
        return self.req_id

    async def _reader_loop(self) -> None:
        """Read JSON-RPC messages from subprocess stdout."""
        while True:
            try:
                raw = await self.proc.stdout.readline()
            except Exception as exc:
                logger.warning(f"subprocess stdout read error: {exc}")
                break

            if not raw:
                # Subprocess closed stdout
                logger.warning(
                    "MCP server subprocess closed stdout",
                    extra={
                        "restart_count": self._restart_count,
                        "stderr_tail": " | ".join(self._stderr_lines[-5:]),
                    }
                )
                break

            try:
                msg = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                logger.warning(f"invalid JSON from subprocess: {raw!r} ({exc})")
                continue

            # Handle response messages
            mid = msg.get("id")
            if mid is not None:
                fut = self._pending.pop(mid, None)
                if fut and not fut.done():
                    fut.set_result(msg)
            else:
                # Notification (no response expected)
                logger.debug(f"received notification: {msg}")

        # Reader loop exited — fail pending requests
        exc = RuntimeError(
            f"MCP server subprocess exited unexpectedly. stderr: "
            + " | ".join(self._stderr_lines[-5:])
        )
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _stderr_loop(self) -> None:
        """Capture stderr from subprocess for debugging."""
        while True:
            try:
                raw = await self.proc.stderr.readline()
            except Exception:
                break

            if not raw:
                break

            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._stderr_lines.append(line)
                # Keep only last 50 lines
                if len(self._stderr_lines) > 50:
                    self._stderr_lines = self._stderr_lines[-25:]

    async def _send(self, payload: dict) -> dict:
        """Send JSON-RPC message to subprocess."""
        if not self.proc or self.proc.returncode is not None:
            raise RuntimeError("MCP server subprocess not running")

        line = json.dumps(payload) + "\n"

        # Set up future for response if this has an ID
        fut: asyncio.Future | None = None
        if "id" in payload:
            mid = payload["id"]
            fut = asyncio.get_running_loop().create_future()
            self._pending[mid] = fut

        try:
            self.proc.stdin.write(line.encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            if fut:
                self._pending.pop(payload.get("id"), None)
            raise RuntimeError(f"failed to send to subprocess: {exc}") from exc

        if fut is None:
            return {}  # Notification, no response expected

        try:
            return await asyncio.wait_for(fut, timeout=_SUBPROCESS_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(payload.get("id"), None)
            raise RuntimeError(f"subprocess timeout after {_SUBPROCESS_TIMEOUT}s")

    async def _initialize(self) -> None:
        """Initialize MCP protocol with subprocess."""
        rid = self._next_id()
        resp = await self._send({
            "jsonrpc": "2.0",
            "id": rid,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mcp-server-wrapper", "version": "1.0"},
            },
        })

        if "error" in resp:
            raise RuntimeError(f"MCP initialize failed: {resp['error']}")

        # Send initialized notification
        await self._send({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })

    async def _check_subprocess_health(self) -> bool:
        """Check if subprocess is still running and healthy."""
        if not self.proc or self.proc.returncode is not None:
            return False
        return True

    async def _handle_crash_and_restart(self) -> bool:
        """Handle subprocess crash and attempt restart."""
        if self._restart_count >= _MAX_RESTARTS:
            logger.error(
                f"MCP server subprocess restart limit exceeded ({_MAX_RESTARTS})",
                extra={"restart_count": self._restart_count}
            )
            return False

        # Log crash details
        exit_code = self.proc.returncode if self.proc else None
        logger.warning(
            "MCP server subprocess crashed — restarting",
            extra={
                "restart_count": self._restart_count,
                "exit_code": exit_code,
                "stderr_tail": " | ".join(self._stderr_lines[-3:]),
            }
        )

        self._restart_count += 1

        # Brief backoff before restart
        await asyncio.sleep(_RESTART_BACKOFF)

        try:
            await self._start_subprocess()
            logger.info(
                "MCP server subprocess restarted successfully",
                extra={"restart_count": self._restart_count}
            )
            return True
        except Exception as exc:
            logger.error(
                f"failed to restart MCP server subprocess: {exc}",
                extra={"restart_count": self._restart_count}
            )
            return False

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a tool via the wrapped MCP server, with crash recovery."""
        for attempt in range(2):  # One retry on crash
            if not await self._check_subprocess_health():
                if not await self._handle_crash_and_restart():
                    raise RuntimeError("MCP server subprocess unavailable after restart attempts")

            rid = self._next_id()
            try:
                resp = await self._send({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                })
                return resp
            except RuntimeError as exc:
                if "subprocess" in str(exc).lower() and attempt == 0:
                    logger.warning(f"tool call failed, attempting restart: {exc}")
                    if not await self._handle_crash_and_restart():
                        raise
                    continue
                raise

        raise RuntimeError("tool call failed after restart attempts")

    async def list_tools(self) -> dict:
        """List available tools via the wrapped MCP server."""
        rid = self._next_id()
        resp = await self._send({
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/list",
            "params": {},
        })
        return resp


async def main() -> int:
    """Main entry point for the wrapper."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="MemPalace MCP Server Wrapper")
    parser.add_argument("--palace", help="Path to mempalace data directory")
    args = parser.parse_args()

    try:
        # Main wrapper loop - handle stdin/stdout for MCP protocol
        async with MCPServerWrapper(args.palace) as wrapper:
            # Forward stdin to the wrapper and stdout back to client
            while True:
                try:
                    raw_line = await asyncio.get_event_loop().run_in_executor(
                        None, sys.stdin.buffer.readline
                    )
                    if not raw_line:
                        break

                    try:
                        msg = json.loads(raw_line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue

                    # Handle different message types
                    method = msg.get("method")
                    if method == "tools/call":
                        params = msg.get("params", {})
                        tool_name = params.get("name")
                        tool_args = params.get("arguments", {})

                        try:
                            resp = await wrapper.call_tool(tool_name, tool_args)
                            # Forward response to stdout
                            print(json.dumps(resp), flush=True)
                        except Exception as exc:
                            error_resp = {
                                "jsonrpc": "2.0",
                                "id": msg.get("id"),
                                "error": {
                                    "code": -32603,
                                    "message": f"Tool call failed: {exc}",
                                },
                            }
                            print(json.dumps(error_resp), flush=True)

                    elif method == "tools/list":
                        try:
                            resp = await wrapper.list_tools()
                            print(json.dumps(resp), flush=True)
                        except Exception as exc:
                            error_resp = {
                                "jsonrpc": "2.0",
                                "id": msg.get("id"),
                                "error": {
                                    "code": -32603,
                                    "message": f"Tools list failed: {exc}",
                                },
                            }
                            print(json.dumps(error_resp), flush=True)

                    elif method == "initialize":
                        # Handle initialization directly
                        init_resp = {
                            "jsonrpc": "2.0",
                            "id": msg.get("id"),
                            "result": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {
                                    "tools": {}
                                },
                                "serverInfo": {
                                    "name": "mempalace-wrapper",
                                    "version": "1.0",
                                },
                            },
                        }
                        print(json.dumps(init_resp), flush=True)

                    elif method == "notifications/initialized":
                        # Notification, no response needed
                        pass

                except Exception as exc:
                    logger.error(f"error handling message: {exc}")

    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error(f"wrapper failed: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        sys.exit(130)