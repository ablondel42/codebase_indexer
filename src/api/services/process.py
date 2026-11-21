"""Async subprocess lifecycle manager for ``agy acp --stdio``."""

import asyncio
import shutil
from collections import deque
from collections.abc import Mapping
from typing import Any

from api.models.config import ApiServerConfig
from api.utils.jsonrpc import encode_stdio_line
from api.utils.security import build_agy_acp_command, build_sanitized_agy_env
from core.models.exceptions import AcpError
from core.utils.logging import get_logger

logger = get_logger("api.services.process")


class AgyAcpProcess:
    """Manages spawning, stdio NDJSON I/O, ``stderr`` draining, and teardown of ``agy acp``."""

    def __init__(self, config: ApiServerConfig) -> None:
        """Initialize the ACP subprocess manager from immutable ``ApiServerConfig``."""
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_lines: deque[str] = deque(maxlen=config.stderr_history_limit)
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        """Return spawned child process ID, or ``None`` when not running."""
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> int | None:
        """Return child process exit status code if exited or torn down."""
        return self._proc.returncode if self._proc is not None else None

    @property
    def is_alive(self) -> bool:
        """Return ``True`` when the child subprocess is currently running."""
        return self._proc is not None and self._proc.returncode is None

    @property
    def stderr_lines(self) -> list[str]:
        """Return bounded snapshot of recent ``stderr`` diagnostic lines."""
        return list(self._stderr_lines)

    def _resolve_workspace_cwd(self) -> str | None:
        """Validate and return resolved working directory string when configured."""
        workspace = self._config.workspace_root
        if workspace is None:
            return None
        resolved = workspace.resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise AcpError(
                f"Configured ACP workspace directory does not exist: {workspace}",
                context={"workspace_root": str(workspace)},
            )
        return str(resolved)

    async def start(self) -> None:
        """Validate command/workspace, spawn ``agy acp`` safely, and begin draining stderr."""
        if self._proc is not None:
            return
        cmd = build_agy_acp_command(self._config)
        executable = cmd[0]
        if shutil.which(executable) is None:
            raise AcpError(
                f"ACP agent executable '{executable}' was not found in PATH.",
                context={"executable": executable},
            )

        cwd = self._resolve_workspace_cwd()
        self._stderr_lines.clear()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=build_sanitized_agy_env(),
                limit=self._config.max_frame_bytes,
            )
        except OSError as exc:
            raise AcpError(
                f"Failed to spawn ACP agent '{executable}': {exc}",
                context={"command": list(cmd)},
            ) from exc

        self._proc = proc
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            await self.stop()
            raise AcpError("ACP subprocess failed to attach stdio pipes.")

        self._stderr_task = asyncio.create_task(self._drain_stderr(proc.stderr))
        logger.info("Spawned ACP subprocess pid=%s cmd=%s", proc.pid, list(cmd))

    async def send_line(self, payload: Mapping[str, Any]) -> None:
        """Write one UTF-8 newline-delimited JSON-RPC message to ``agy acp`` ``stdin``."""
        async with self._write_lock:
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise AcpError("Cannot write to ACP subprocess: stdin is closed.")
            encoded = encode_stdio_line(payload, max_bytes=self._config.max_frame_bytes)
            try:
                proc.stdin.write(encoded)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise AcpError(f"Failed writing to ACP subprocess stdin: {exc}") from exc

    async def read_line(self) -> str | None:
        """Read one newline-delimited JSON-RPC frame from ``agy acp`` ``stdout``."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise AcpError("Cannot read from ACP subprocess: stdout is closed.")
        try:
            raw_line = await proc.stdout.readline()
        except (asyncio.LimitOverrunError, ValueError, OSError) as exc:
            logger.warning("ACP subprocess stdout read error: %s", exc)
            raise AcpError(f"ACP stdout frame failed size or stream check: {exc}") from exc
        if not raw_line:
            return None
        try:
            return raw_line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise AcpError(f"ACP agent emitted non-UTF-8 stdout data: {exc}") from exc

    async def stop(self) -> None:
        """Close pipes, gracefully terminate or kill the child process, and join stderr."""
        proc = self._proc
        stderr_task = self._stderr_task
        self._proc = None
        self._stderr_task = None
        if proc is None:
            return

        self._close_stdin(proc)
        await self._terminate_and_reap(proc)
        await self._stop_stderr_task(stderr_task)
        logger.debug("Stopped and reaped ACP subprocess pid=%s", proc.pid)

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        """Continuously drain child ``stderr`` lines into bounded memory history."""
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                self._stderr_lines.append(text)
                logger.debug("ACP stderr: %s", text)
        except (asyncio.LimitOverrunError, ValueError, OSError) as exc:
            logger.debug("ACP stderr reader stopped cleanly: %s", exc)

    @staticmethod
    def _close_stdin(proc: asyncio.subprocess.Process) -> None:
        """Close child ``stdin`` pipe cleanly, logging expected pipe teardown errors."""
        if proc.stdin is None or proc.stdin.is_closing():
            return
        try:
            proc.stdin.close()
        except OSError as exc:
            logger.debug("Ignoring ACP stdin close error during teardown: %s", exc)

    async def _terminate_and_reap(self, proc: asyncio.subprocess.Process) -> None:
        """Send ``SIGTERM`` and wait up to configured timeout before escalating to ``SIGKILL``."""
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError as exc:
                logger.debug("ACP subprocess pid=%s already exited: %s", proc.pid, exc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._config.terminate_wait_seconds)
        except TimeoutError as exc:
            logger.warning(
                "ACP subprocess pid=%s did not exit within %.2fs (%s); killing",
                proc.pid,
                self._config.terminate_wait_seconds,
                exc,
            )
            await self._kill_and_reap(proc)

    @staticmethod
    async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
        """Force-kill unresponsive child subprocess and await exit."""
        try:
            proc.kill()
        except ProcessLookupError as exc:
            logger.debug("ACP subprocess pid=%s already exited prior to kill: %s", proc.pid, exc)
        await proc.wait()

    @staticmethod
    async def _stop_stderr_task(stderr_task: asyncio.Task[None] | None) -> None:
        """Cancel and await background ``stderr`` drain task."""
        if stderr_task is None:
            return
        if not stderr_task.done():
            stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError as exc:
            logger.debug("ACP stderr drain task cancelled: %s", exc)


__all__ = [
    "AgyAcpProcess",
]
