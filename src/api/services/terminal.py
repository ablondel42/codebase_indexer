"""Host-mode sandboxed ``terminal/*`` command execution and ring-buffer manager."""

import asyncio
import secrets
from pathlib import Path

from api.models.client_ops import (
    CreateTerminalParams,
    CreateTerminalResult,
    KillTerminalParams,
    KillTerminalResult,
    ReleaseTerminalParams,
    ReleaseTerminalResult,
    TerminalExitStatus,
    TerminalOutputParams,
    TerminalOutputResult,
    WaitForTerminalExitParams,
    WaitForTerminalExitResult,
)
from api.models.config import CapabilityPolicyConfig
from api.models.session import SessionRootSet
from api.services.logging import AcpAuditLogger
from api.utils.security import (
    build_sanitized_agent_env,
    validate_path_in_root_set,
    validate_terminal_command,
    validate_terminal_env,
)
from api.utils.terminal_buffer import (
    append_bounded_utf8,
    decode_terminal_buffer,
    signal_name_from_returncode,
)
from core.models.exceptions import (
    AcpInvalidParamsError,
    AcpResourceNotFoundError,
    AcpSecurityError,
)
from core.utils.logging import get_logger

logger = get_logger("api.services.terminal")


class _ManagedTerminal:
    """In-memory state for a single host-executed terminal process."""

    def __init__(
        self,
        terminal_id: str,
        session_id: str,
        process: asyncio.subprocess.Process,
        output_byte_limit: int,
    ) -> None:
        self.terminal_id = terminal_id
        self.session_id = session_id
        self.process = process
        self.output_byte_limit = output_byte_limit
        self.buffer: bytes = b""
        self.truncated: bool = False
        self.exit_code: int | None = None
        self.signal_name: str | None = None
        self.exited: bool = False
        self.exit_event = asyncio.Event()
        self.reader_task: asyncio.Task[None] | None = None


class HostTerminalManager:
    """Manages host-executed ACP ``terminal/*`` processes confined to session root sets."""

    def __init__(self, policy: CapabilityPolicyConfig, audit: AcpAuditLogger) -> None:
        """Initialize terminal manager with security policy and audit logger."""
        self._policy = policy
        self._audit = audit
        self._terminals: dict[str, _ManagedTerminal] = {}

    def _session_terminal_count(self, session_id: str) -> int:
        """Count currently un-released terminals owned by ``session_id``."""
        return sum(1 for item in self._terminals.values() if item.session_id == session_id)

    def _lookup_terminal(self, session_id: str, terminal_id: str) -> _ManagedTerminal:
        """Return matching terminal record or raise ``-32002`` if unknown or released."""
        record = self._terminals.get(terminal_id)
        if record is None or record.session_id != session_id:
            raise AcpResourceNotFoundError(
                f"Unknown or released terminal: '{terminal_id}'",
                wire_data={"terminalId": terminal_id},
            )
        return record

    def _resolve_terminal_cwd(
        self,
        requested_cwd: str | None,
        root_set: SessionRootSet | None,
    ) -> Path:
        """Resolve and validate terminal working directory inside ``root_set``."""
        raw_cwd = requested_cwd if requested_cwd is not None else (root_set.cwd if root_set else "")
        if not raw_cwd:
            raise AcpInvalidParamsError("Terminal cwd could not be determined for session")
        resolved = validate_path_in_root_set(raw_cwd, root_set, allow_missing_leaf=False)
        if not resolved.exists() or not resolved.is_dir():
            raise AcpInvalidParamsError(f"Terminal cwd directory does not exist: {raw_cwd!r}")
        return resolved

    async def _read_stream_chunks(self, record: _ManagedTerminal) -> None:
        """Read merged stdout/stderr bytes into bounded UTF-8 ring buffer until EOF."""
        stdout = record.process.stdout
        if stdout is None:
            return
        while True:
            chunk = await stdout.read(8192)
            if not chunk:
                return
            record.buffer, was_truncated = append_bounded_utf8(
                record.buffer,
                chunk,
                record.output_byte_limit,
            )
            if was_truncated:
                record.truncated = True

    async def _pump_output(self, record: _ManagedTerminal) -> None:
        """Drain merged stdout/stderr into bounded UTF-8 ring buffer until process exits."""
        try:
            await self._read_stream_chunks(record)
            returncode = await record.process.wait()
            record.exit_code, record.signal_name = signal_name_from_returncode(returncode)
            record.exited = True
        except asyncio.CancelledError:
            logger.debug("Cancelled terminal reader task for %s", record.terminal_id)
            raise
        except OSError as exc:
            logger.warning("Terminal %s stream read failed: %s", record.terminal_id, exc)
            record.exited = True
        finally:
            record.exit_event.set()

    async def _stop_process(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate a running terminal process and escalate to SIGKILL after timeout."""
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError as exc:
            logger.debug("Terminal process %s already exited: %s", proc.pid, exc)
            return
        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=self._policy.terminal_terminate_wait_seconds,
            )
        except TimeoutError as exc:
            logger.warning("Terminal process %s did not exit cleanly (%s); killing", proc.pid, exc)
            try:
                proc.kill()
            except ProcessLookupError as kill_exc:
                logger.debug("Terminal process %s already gone: %s", proc.pid, kill_exc)
            await proc.wait()

    async def create_terminal(
        self,
        params: CreateTerminalParams,
        root_set: SessionRootSet | None,
    ) -> CreateTerminalResult:
        """Validate command/env/cwd, spawn process without shell, and return ``terminalId``."""
        if self._session_terminal_count(params.session_id) >= self._policy.terminal_max_per_session:
            self._audit.record(
                "terminal_limit_exceeded",
                outcome="denied",
                details={"session_id": params.session_id},
            )
            raise AcpSecurityError("Maximum active terminals per session exceeded")

        validate_terminal_command(
            params.command,
            allowed_commands=self._policy.terminal_allowed_commands,
            allow_unrestricted=self._policy.terminal_allow_unrestricted_commands,
        )
        env_overrides = validate_terminal_env(
            params.env,
            allow_dangerous_env=self._policy.terminal_allow_dangerous_env,
        )
        cwd_path = self._resolve_terminal_cwd(params.cwd, root_set)
        env_map = build_sanitized_agent_env()
        env_map.update(env_overrides)

        requested_limit = params.output_byte_limit
        byte_limit = (
            self._policy.terminal_default_output_byte_limit
            if requested_limit is None
            else min(requested_limit, self._policy.terminal_max_output_byte_limit)
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                params.command,
                *params.args,
                cwd=str(cwd_path),
                env=env_map,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise AcpInvalidParamsError(f"Failed executing terminal command: {exc}") from exc

        terminal_id = f"term_{secrets.token_hex(8)}"
        record = _ManagedTerminal(terminal_id, params.session_id, proc, byte_limit)
        record.reader_task = asyncio.create_task(self._pump_output(record))
        self._terminals[terminal_id] = record
        self._audit.record(
            "terminal_created",
            outcome="allowed",
            details={
                "session_id": params.session_id,
                "terminal_id": terminal_id,
                "command": params.command,
                "cwd": str(cwd_path),
            },
        )
        return CreateTerminalResult(terminalId=terminal_id)

    def get_output(self, params: TerminalOutputParams) -> TerminalOutputResult:
        """Return captured output, ``truncated`` flag, and optional ``exitStatus``."""
        record = self._lookup_terminal(params.session_id, params.terminal_id)
        exit_status = (
            TerminalExitStatus(exitCode=record.exit_code, signal=record.signal_name)
            if record.exited
            else None
        )
        return TerminalOutputResult(
            output=decode_terminal_buffer(record.buffer),
            truncated=record.truncated,
            exitStatus=exit_status,
        )

    async def wait_for_exit(
        self,
        params: WaitForTerminalExitParams,
    ) -> WaitForTerminalExitResult:
        """Wait until ``params.terminal_id`` finishes and return its exit status."""
        record = self._lookup_terminal(params.session_id, params.terminal_id)
        await record.exit_event.wait()
        return WaitForTerminalExitResult(
            exitCode=record.exit_code,
            signal=record.signal_name,
        )

    async def kill_terminal(self, params: KillTerminalParams) -> KillTerminalResult:
        """Terminate the running command while keeping ``terminalId`` valid for reads."""
        record = self._lookup_terminal(params.session_id, params.terminal_id)
        await self._stop_process(record.process)
        self._audit.record(
            "terminal_killed",
            outcome="allowed",
            details={"session_id": params.session_id, "terminal_id": params.terminal_id},
        )
        return KillTerminalResult()

    async def _close_record(self, record: _ManagedTerminal) -> None:
        """Stop process, cancel reader task, and close subprocess transport cleanly."""
        await self._stop_process(record.process)
        if record.reader_task is not None and not record.reader_task.done():
            record.reader_task.cancel()
            try:
                await record.reader_task
            except asyncio.CancelledError as exc:
                logger.debug("Stopped terminal reader %s: %s", record.terminal_id, exc)
        transport = getattr(record.process, "_transport", None)
        if transport is not None:
            transport.close()

    async def release_terminal(self, params: ReleaseTerminalParams) -> ReleaseTerminalResult:
        """Kill process if running and invalidate ``terminalId`` for further calls."""
        record = self._lookup_terminal(params.session_id, params.terminal_id)
        self._terminals.pop(params.terminal_id, None)
        await self._close_record(record)
        self._audit.record(
            "terminal_released",
            outcome="allowed",
            details={"session_id": params.session_id, "terminal_id": params.terminal_id},
        )
        return ReleaseTerminalResult()

    async def release_session(self, session_id: str) -> None:
        """Release all terminals belonging to ``session_id``."""
        targets = [
            self._terminals.pop(tid)
            for tid, rec in list(self._terminals.items())
            if rec.session_id == session_id
        ]
        for rec in targets:
            await self._close_record(rec)

    async def release_all(self) -> None:
        """Release all active terminals across every session during connection shutdown."""
        targets = list(self._terminals.values())
        self._terminals.clear()
        for rec in targets:
            await self._close_record(rec)


__all__ = [
    "HostTerminalManager",
]
