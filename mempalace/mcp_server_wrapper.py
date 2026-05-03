#!/usr/bin/env python3
"""
MCP Server Subprocess Wrapper — Plan A crash recovery for mempalace MCP bridge
===============================================================================

Transparent proxy that spawns mempalace.mcp_server as a subprocess and handles
crashes by restarting it. Forwards all JSON-RPC messages unchanged while providing
crash recovery for the nova-cc MCP bridge path.

The wrapper maintains protocol transparency while providing crash recovery:
- Detects subprocess crashes (SEGV, unexpected exits)
- Restarts the MCP server subprocess automatically
- Forwards all messages transparently to/from subprocess
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
from typing import Any

logger = logging.getLogger(__name__)

# Configuration
_MAX_RESTARTS = int(os.environ.get("MEMPALACE_WRAPPER_RESTART_LIMIT", "3"))
_RESTART_BACKOFF = float(os.environ.get("MEMPALACE_WRAPPER_RESTART_BACKOFF", "0.1"))

# Track restarts for observability
_restart_count = 0
_last_restart_time = 0.0


class MCPServerProxy:
    """Transparent proxy for mempalace MCP server with crash recovery."""

    def __init__(self, palace_path: str | None = None):
        self.palace_path = palace_path
        self.proc: asyncio.subprocess.Process | None = None
        self.restart_count = 0
        self._stdin_task: asyncio.Task | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_lines: list[str] = []

    async def start(self) -> None:
        """Start the proxy and subprocess."""
        await self._start_subprocess()
        # Start forwarding tasks
        self._stdin_task = asyncio.create_task(self._forward_stdin())
        self._stdout_task = asyncio.create_task(self._forward_stdout())
        self._stderr_task = asyncio.create_task(self._forward_stderr())

    async def wait(self) -> None:
        """Wait for all tasks to complete."""
        if self._stdin_task:
            await self._stdin_task
        if self._stdout_task:
            await self._stdout_task
        if self._stderr_task:
            await self._stderr_task

    async def _start_subprocess(self) -> None:
        """Start or restart the MCP server subprocess."""
        global _restart_count, _last_restart_time

        if self.proc and self.proc.returncode is None:
            await self._cleanup_subprocess()

        # Build argv for subprocess
        argv = [sys.executable, "-m", "mempalace.mcp_server"]
        if self.palace_path:
            argv.extend(["--palace", self.palace_path])

        # Environment with skip-pin enabled
        env = {**os.environ, "MEMPALACE_SKIP_PIN": "1"}

        logger.info(
            "starting mempalace MCP server subprocess",
            extra={
                "restart_count": self.restart_count,
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

        if self.restart_count > 0:
            _restart_count += 1
            _last_restart_time = time.time()

    async def _cleanup_subprocess(self) -> None:
        """Clean up subprocess."""
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                self.proc.kill()
                with contextlib.suppress(ProcessLookupError):
                    await self.proc.wait()

    async def _handle_crash_and_restart(self) -> bool:
        """Handle subprocess crash and attempt restart."""
        if self.restart_count >= _MAX_RESTARTS:
            logger.error(
                f"MCP server subprocess restart limit exceeded ({_MAX_RESTARTS})",
                extra={"restart_count": self.restart_count}
            )
            return False

        # Log crash details
        exit_code = self.proc.returncode if self.proc else None
        logger.warning(
            "MCP server subprocess crashed — restarting",
            extra={
                "restart_count": self.restart_count,
                "exit_code": exit_code,
                "stderr_tail": " | ".join(self._stderr_lines[-3:]),
            }
        )

        self.restart_count += 1

        # Brief backoff before restart
        await asyncio.sleep(_RESTART_BACKOFF)

        try:
            await self._start_subprocess()
            logger.info(
                "MCP server subprocess restarted successfully",
                extra={"restart_count": self.restart_count}
            )
            return True
        except Exception as exc:
            logger.error(
                f"failed to restart MCP server subprocess: {exc}",
                extra={"restart_count": self.restart_count}
            )
            return False

    async def _forward_stdin(self) -> None:
        """Forward stdin to subprocess stdin."""
        try:
            while True:
                # Read from our stdin (read1 returns as soon as ANY data is
                # available; plain read() blocks until full 4096 bytes or EOF,
                # which deadlocks on MCP messages smaller than 4096 bytes).
                data = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.buffer.read1, 4096
                )
                if not data:
                    break

                # Write to subprocess stdin
                while True:
                    if not self.proc or self.proc.returncode is not None:
                        # Subprocess died, attempt restart
                        if not await self._handle_crash_and_restart():
                            logger.error("unable to restart subprocess for stdin forwarding")
                            return
                        continue

                    try:
                        self.proc.stdin.write(data)
                        await self.proc.stdin.drain()
                        break
                    except (BrokenPipeError, ConnectionResetError):
                        # Subprocess died during write, attempt restart
                        if not await self._handle_crash_and_restart():
                            logger.error("unable to restart subprocess after write failure")
                            return
                        continue
        except Exception as exc:
            logger.error(f"stdin forwarding failed: {exc}")
        finally:
            # Close subprocess stdin
            if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()

    async def _forward_stdout(self) -> None:
        """Forward subprocess stdout to our stdout."""
        try:
            while True:
                # Check if subprocess is alive
                if not self.proc:
                    await asyncio.sleep(0.1)
                    continue

                if self.proc.returncode is not None:
                    # Subprocess exited, try restart
                    if not await self._handle_crash_and_restart():
                        logger.error("unable to restart subprocess for stdout forwarding")
                        break
                    continue

                try:
                    data = await self.proc.stdout.read(4096)
                    if not data:
                        # EOF from subprocess
                        if self.proc.returncode is None:
                            # Subprocess closed stdout but is still running - unusual
                            logger.warning("subprocess closed stdout but still running")
                        break

                    # Write to our stdout
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except Exception as exc:
                    logger.warning(f"stdout read error: {exc}")
                    # Try to restart on read errors
                    if not await self._handle_crash_and_restart():
                        break
        except Exception as exc:
            logger.error(f"stdout forwarding failed: {exc}")

    async def _forward_stderr(self) -> None:
        """Capture subprocess stderr for debugging."""
        try:
            while True:
                if not self.proc:
                    await asyncio.sleep(0.1)
                    continue

                if self.proc.returncode is not None:
                    break

                try:
                    data = await self.proc.stderr.read(4096)
                    if not data:
                        break

                    # Log stderr from subprocess
                    stderr_text = data.decode("utf-8", errors="replace")
                    for line in stderr_text.splitlines():
                        if line.strip():
                            self._stderr_lines.append(line.strip())
                            # Keep only last 50 lines
                            if len(self._stderr_lines) > 50:
                                self._stderr_lines = self._stderr_lines[-25:]

                            # Also forward to our stderr
                            print(f"subprocess: {line}", file=sys.stderr)
                except Exception as exc:
                    logger.warning(f"stderr read error: {exc}")
                    break
        except Exception as exc:
            logger.error(f"stderr forwarding failed: {exc}")


async def main() -> int:
    """Main entry point for the wrapper."""
    # Set up logging to stderr only (stdout is for MCP protocol)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr
    )

    parser = argparse.ArgumentParser(description="MemPalace MCP Server Wrapper")
    parser.add_argument("--palace", help="Path to mempalace data directory")
    args = parser.parse_args()

    try:
        proxy = MCPServerProxy(args.palace)
        await proxy.start()
        await proxy.wait()
        return 0
    except KeyboardInterrupt:
        logger.info("received interrupt, shutting down")
        return 0
    except Exception as exc:
        logger.error(f"wrapper failed: {exc}")
        return 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        sys.exit(130)