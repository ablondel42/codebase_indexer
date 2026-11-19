"""Subprocess lifecycle manager for ``pyright-langserver --stdio``."""

import shutil
import subprocess
import threading
from collections import deque
from typing import IO

from core.models.exceptions import LspError
from core.utils.logging import get_logger
from lsp.models.protocol import LspClientOptions

logger = get_logger("lsp.services.process")


class PyrightProcess:
    """Manages spawning, ``stderr`` draining, and zombie-free teardown of an LSP server."""

    _MAX_STDERR_LINE_BYTES: int = 4096
    _THREAD_JOIN_TIMEOUT_SECONDS: float = 1.0

    def __init__(self, options: LspClientOptions | None = None) -> None:
        """Initialize the subprocess manager with bounded ``stderr`` history.

        Args:
            options: Optional ``LspClientOptions`` controlling command and timeouts.
        """
        self._options: LspClientOptions = options or LspClientOptions()
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=self._options.stderr_history_limit)
        self._stderr_thread: threading.Thread | None = None

    @property
    def options(self) -> LspClientOptions:
        """Return the immutable ``LspClientOptions`` governing this process."""
        return self._options

    @property
    def pid(self) -> int | None:
        """Return the active child process ID, or ``None`` if not spawned."""
        return self._proc.pid if self._proc is not None else None

    @property
    def is_alive(self) -> bool:
        """Return ``True`` when the child process is spawned and has not exited."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def stdin(self) -> IO[bytes] | None:
        """Return the writable binary ``stdin`` pipe of the child process, if open."""
        return self._proc.stdin if self._proc is not None else None

    @property
    def stdout(self) -> IO[bytes] | None:
        """Return the readable binary ``stdout`` pipe of the child process, if open."""
        return self._proc.stdout if self._proc is not None else None

    @property
    def stderr_lines(self) -> list[str]:
        """Return a snapshot of recent ``stderr`` lines drained from the server."""
        return list(self._stderr_lines)

    def start(self) -> None:
        """Validate the server executable, spawn the subprocess, and start ``stderr`` draining.

        Raises:
            LspError: If ``server_cmd`` is empty, missing from ``PATH``, fails to spawn,
                or fails to allocate stdio pipes.
        """
        if not self._options.server_cmd:
            raise LspError("LSP server command cannot be empty.")

        executable = self._options.server_cmd[0]
        if shutil.which(executable) is None:
            raise LspError(
                f"LSP server executable '{executable}' not found in PATH.",
                context={"executable": executable},
            )

        self._stderr_lines.clear()
        cmd_list = list(self._options.server_cmd)
        try:
            self._proc = subprocess.Popen(
                cmd_list,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self._proc = None
            raise LspError(
                f"Failed to launch LSP server '{executable}': {exc}",
                context={"server_cmd": cmd_list},
            ) from exc

        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            self.stop()
            raise LspError("LSP server pipes were not created.")

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self._proc.stderr,),
            name="pyright-lsp-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        logger.info("Spawned LSP server subprocess pid=%s cmd=%s", self._proc.pid, cmd_list)

    def stop(self) -> None:
        """Close pipes, terminate or kill the child process, and reap its exit status."""
        proc = self._proc
        if proc is None:
            self._stderr_thread = None
            return

        self._close_stream(proc.stdin, "stdin")
        self._terminate_and_reap(proc)

        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=self._THREAD_JOIN_TIMEOUT_SECONDS)

        self._close_stream(proc.stdout, "stdout")
        self._close_stream(proc.stderr, "stderr")

        logger.debug("Stopped and reaped LSP subprocess pid=%s", proc.pid)
        self._proc = None
        self._stderr_thread = None

    def _drain_stderr(self, stderr: IO[bytes]) -> None:
        """Continuously drain ``stderr`` lines into ``self._stderr_lines`` on a daemon thread.

        Args:
            stderr: Readable binary ``stderr`` pipe from ``subprocess.Popen``.
        """
        try:
            while True:
                line = stderr.readline(self._MAX_STDERR_LINE_BYTES)
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                self._stderr_lines.append(text)
                logger.debug("LSP stderr: %s", text)
        except (OSError, ValueError) as exc:
            logger.debug("LSP stderr reader stopped on pipe close: %s", exc)

    def _terminate_and_reap(self, proc: subprocess.Popen[bytes]) -> None:
        """Send ``SIGTERM``, wait for graceful exit, or escalate to ``SIGKILL`` and reap.

        Args:
            proc: Active or exited ``subprocess.Popen`` handle.
        """
        if proc.poll() is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError) as exc:
                logger.debug(
                    "LSP subprocess pid=%s already exited prior to terminate: %s",
                    proc.pid,
                    exc,
                )

        try:
            proc.wait(timeout=self._options.terminate_wait_seconds)
        except subprocess.TimeoutExpired as exc:
            logger.warning(
                "LSP subprocess pid=%s did not exit within %.2fs (%s); sending SIGKILL",
                proc.pid,
                self._options.terminate_wait_seconds,
                exc,
            )
            self._kill_and_reap(proc)

    @staticmethod
    def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
        """Force-kill ``proc`` and block until its OS process entry is reaped.

        Args:
            proc: Unresponsive ``subprocess.Popen`` handle.
        """
        try:
            proc.kill()
        except (ProcessLookupError, OSError) as exc:
            logger.debug(
                "LSP subprocess pid=%s already exited prior to kill: %s",
                proc.pid,
                exc,
            )
        proc.wait()

    @staticmethod
    def _close_stream(stream: IO[bytes] | None, label: str) -> None:
        """Close a subprocess stdio pipe, logging expected teardown errors at ``DEBUG``.

        Args:
            stream: Optional pipe stream to close.
            label: Human-readable stream name (``"stdin"``, ``"stdout"``, ``"stderr"``).
        """
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError) as exc:
            logger.debug("Ignoring %s close error during LSP teardown: %s", label, exc)
