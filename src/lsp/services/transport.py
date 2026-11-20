"""Thread-safe JSON-RPC 2.0 stdio transport with request correlation and server replies."""

import json
import threading
from collections.abc import Mapping
from typing import IO, Any

from core.models.exceptions import LspError
from core.utils.logging import get_logger
from lsp.models.protocol import JsonRpcResponse, LspClientOptions
from lsp.services.process import PyrightProcess
from lsp.utils.framing import (
    encode_jsonrpc_message,
    parse_content_length_header,
    parse_header_line,
)

logger = get_logger("lsp.services.transport")


class _PendingWaiter:
    """Synchronization primitive correlating an outbound JSON-RPC request ID to its reply."""

    __slots__ = ("error", "event", "response")

    def __init__(self) -> None:
        """Initialize an unset event with empty response and error slots."""
        self.event = threading.Event()
        self.response: JsonRpcResponse | None = None
        self.error: LspError | None = None


class JsonRpcTransport:
    """Manages framed JSON-RPC 2.0 I/O, pending request waiters, and server requests."""

    def __init__(
        self,
        process: PyrightProcess,
        options: LspClientOptions | None = None,
    ) -> None:
        """Initialize the transport bound to ``process``.

        Args:
            process: Underlying ``PyrightProcess`` owning ``stdin`` and ``stdout`` pipes.
            options: Optional ``LspClientOptions`` overriding ``process.options``.
        """
        self._process = process
        self._options = options or process.options
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int | str, _PendingWaiter] = {}
        self._next_id = 1
        self._closing = False
        self._reader_error: LspError | None = None
        self._stdout_thread: threading.Thread | None = None

    @property
    def is_healthy(self) -> bool:
        """Return ``True`` when the transport is open, error-free, and process is alive."""
        return not self._closing and self._reader_error is None and self._process.is_alive

    @property
    def reader_error(self) -> LspError | None:
        """Return any fatal ``LspError`` recorded by the ``stdout`` reader thread."""
        return self._reader_error

    def start(self) -> None:
        """Reset transport state and launch the ``stdout`` frame reader daemon thread.

        Raises:
            LspError: If ``process.stdout`` is unavailable.
        """
        stdout = self._process.stdout
        if stdout is None:
            raise LspError("Cannot start JSON-RPC transport without process stdout.")

        with self._pending_lock:
            self._pending.clear()
            self._next_id = 1
            self._reader_error = None
            self._closing = False

        self._stdout_thread = threading.Thread(
            target=self._stdout_loop,
            args=(stdout,),
            name="pyright-lsp-stdout",
            daemon=True,
        )
        self._stdout_thread.start()

    def begin_close(self) -> None:
        """Mark the transport as closing and unblock all in-flight waiters immediately."""
        self._closing = True
        self._fail_all_pending(LspError("LSP client closed."))

    def join_reader(self, timeout: float = 1.0) -> None:
        """Join the ``stdout`` reader daemon thread up to ``timeout`` seconds.

        Args:
            timeout: Maximum seconds to wait for the reader thread to exit.
        """
        if self._stdout_thread is not None and self._stdout_thread.is_alive():
            self._stdout_thread.join(timeout=timeout)
        self._stdout_thread = None

    def send_notification(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        allow_while_closing: bool = False,
    ) -> None:
        """Write a one-way JSON-RPC notification message without expecting a response.

        Args:
            method: LSP notification method name (e.g., ``"initialized"``).
            params: Optional notification parameters mapping.
            allow_while_closing: Allow sending ``"exit"`` while shutting down.

        Raises:
            LspError: If the transport is closed/inactive or writing fails.
        """
        if not self._process.is_alive or (self._closing and not allow_while_closing):
            raise LspError("LSP client is not running.")
        self._write_message({
            "jsonrpc": "2.0",
            "method": method,
            "params": dict(params) if params is not None else {},
        })

    def send_request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> JsonRpcResponse:
        """Send a JSON-RPC request and block until its correlated response arrives.

        Args:
            method: LSP request method name (e.g., ``"textDocument/references"``).
            params: Optional request parameters mapping.
            timeout: Optional per-call timeout override in seconds.

        Returns:
            Validated ``JsonRpcResponse`` envelope.

        Raises:
            LspError: If the client is inactive, writing fails, the request times out,
                or the server returns a JSON-RPC error object.
        """
        if self._closing or not self._process.is_alive:
            raise LspError("LSP client is not running.")

        wait_timeout = self._options.request_timeout_seconds if timeout is None else float(timeout)
        msg_id, waiter = self._register_waiter()

        try:
            self._write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": dict(params) if params is not None else {},
            })
        except LspError:
            self._discard_waiter(msg_id)
            raise

        return self._await_response(method, msg_id, waiter, wait_timeout)

    def _register_waiter(self) -> tuple[int, _PendingWaiter]:
        """Allocate the next request ID and register its ``_PendingWaiter`` atomically.

        Returns:
            Tuple of ``(request_id, waiter)``.

        Raises:
            LspError: If a fatal reader error has already been recorded.
        """
        waiter = _PendingWaiter()
        with self._pending_lock:
            if self._reader_error is not None:
                raise self._reader_error
            msg_id = self._next_id
            self._next_id += 1
            self._pending[msg_id] = waiter
        return msg_id, waiter

    def _discard_waiter(self, msg_id: int | str) -> None:
        """Remove an in-flight waiter by ``msg_id`` when a write or timeout occurs.

        Args:
            msg_id: Request identifier to remove.
        """
        with self._pending_lock:
            self._pending.pop(msg_id, None)

    def _await_response(
        self,
        method: str,
        msg_id: int | str,
        waiter: _PendingWaiter,
        wait_timeout: float,
    ) -> JsonRpcResponse:
        """Wait for ``waiter`` completion and validate the resulting ``JsonRpcResponse``.

        Args:
            method: LSP method name for contextual error reporting.
            msg_id: Request identifier.
            waiter: Registered ``_PendingWaiter`` instance.
            wait_timeout: Timeout in seconds.

        Returns:
            Validated ``JsonRpcResponse`` without error payload.

        Raises:
            LspError: On timeout, transport reader failure, or JSON-RPC error payload.
        """
        if not waiter.event.wait(timeout=wait_timeout):
            self._discard_waiter(msg_id)
            raise LspError(
                f"LSP request '{method}' timed out after {wait_timeout}s.",
                context={
                    "method": method,
                    "timeout": wait_timeout,
                    "stderr_tail": self._process.stderr_lines[-10:],
                },
            )

        if waiter.error is not None:
            raise waiter.error

        response = waiter.response
        if response is None:
            raise LspError(
                f"Missing JSON-RPC response for '{method}'.",
                context={"method": method, "id": msg_id},
            )
        if response.error is not None:
            raise LspError(
                f"LSP error for {method}: {response.error}",
                context={"method": method, "error": response.error},
            )
        return response

    def _write_message(self, payload: Mapping[str, Any]) -> None:
        """Encode and write a framed JSON-RPC message to ``process.stdin`` atomically.

        Args:
            payload: JSON-RPC message mapping to transmit.

        Raises:
            LspError: If ``stdin`` is unavailable or writing/flushing fails.
        """
        frame = encode_jsonrpc_message(payload, self._options.max_payload_bytes)
        with self._write_lock:
            stdin = self._process.stdin
            if stdin is None:
                raise LspError("LSP server stdin is unavailable.")
            try:
                stdin.write(frame)
                stdin.flush()
            except (OSError, ValueError, BrokenPipeError) as exc:
                raise LspError(f"Failed writing to LSP server stdin: {exc}") from exc

    def _stdout_loop(self, stdout: IO[bytes]) -> None:
        """Read and dispatch JSON-RPC frames from ``stdout`` until EOF or shutdown.

        Args:
            stdout: Readable binary ``stdout`` pipe from ``PyrightProcess``.
        """
        try:
            while not self._closing:
                msg = self._read_message(stdout)
                self._dispatch_message(msg)
        except EOFError as exc:
            if self._closing:
                logger.debug("LSP stdout closed cleanly during shutdown: %s", exc)
                return
            err = LspError(
                "LSP server closed stdout unexpectedly.",
                context={"stderr_tail": self._process.stderr_lines[-10:]},
            )
            logger.warning("LSP server closed stdout unexpectedly: %s", exc)
            self._record_reader_failure(err)
        except LspError as err:
            if self._closing:
                logger.debug("LSP stdout reader stopped during shutdown: %s", err)
                return
            logger.warning("LSP transport protocol error: %s", err)
            self._record_reader_failure(err)
        except Exception as exc:
            if self._closing:
                logger.debug("LSP stdout reader stopped during shutdown: %s", exc)
                return
            logger.exception("Unexpected LSP stdout reader failure: %s", exc)
            self._record_reader_failure(
                LspError(
                    f"Unexpected LSP stdout reader failure: {exc}",
                    context={"stderr_tail": self._process.stderr_lines[-10:]},
                )
            )

    def _record_reader_failure(self, err: LspError) -> None:
        """Store ``err`` as the terminal reader error and wake all pending waiters.

        Args:
            err: Fatal ``LspError`` encountered while reading ``stdout``.
        """
        self._reader_error = err
        self._fail_all_pending(err)

    def _fail_all_pending(self, err: LspError) -> None:
        """Propagate ``err`` to all currently registered request waiters.

        Args:
            err: ``LspError`` to attach to each unfulfilled waiter.
        """
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            waiter.error = err
            waiter.event.set()

    def _dispatch_message(self, msg: Mapping[str, Any]) -> None:
        """Route a decoded JSON-RPC message by response, server request, or notification.

        Args:
            msg: Decoded JSON-RPC object mapping.
        """
        if "id" in msg and "method" not in msg:
            self._complete_response_waiter(msg)
            return

        if "id" in msg and "method" in msg:
            self._handle_server_request(msg)
            return

        if "method" in msg:
            logger.debug("LSP notification received: %s", msg.get("method"))
            return

        logger.debug("Ignoring malformed LSP message without id or method")

    def _complete_response_waiter(self, msg: Mapping[str, Any]) -> None:
        """Fulfill the pending waiter matching ``msg['id']`` with a ``JsonRpcResponse``.

        Args:
            msg: Decoded JSON-RPC response mapping containing ``id``.
        """
        msg_id = msg["id"]
        with self._pending_lock:
            waiter = self._pending.pop(msg_id, None)
        if waiter is None:
            logger.debug("Ignoring unmatched LSP response id=%r", msg_id)
            return

        raw_error = msg.get("error")
        normalized_error: dict[str, Any] | None
        if raw_error is None:
            normalized_error = None
        elif isinstance(raw_error, Mapping):
            normalized_error = dict(raw_error)
        else:
            normalized_error = {"message": str(raw_error)}

        raw_id = msg_id if isinstance(msg_id, (int, str)) else None
        waiter.response = JsonRpcResponse(
            id=raw_id,
            result=msg.get("result"),
            error=normalized_error,
        )
        waiter.event.set()

    def _handle_server_request(self, msg: Mapping[str, Any]) -> None:
        """Reply immediately to server-initiated requests so Pyright never stalls.

        Args:
            msg: Server-initiated JSON-RPC request containing ``id`` and ``method``.
        """
        req_id = msg["id"]
        method = str(msg["method"])
        params = msg.get("params")

        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, Mapping) else None
            result: Any = [None] * len(items) if isinstance(items, list) else []
        else:
            result = None

        try:
            self._write_message({"jsonrpc": "2.0", "id": req_id, "result": result})
        except LspError as exc:
            logger.debug(
                "Failed replying to server request %s (id=%r): %s",
                method,
                req_id,
                exc,
            )

    def _read_message(self, stdout: IO[bytes]) -> Mapping[str, Any]:
        """Read and decode a single framed JSON-RPC object from ``stdout``.

        Args:
            stdout: Readable binary ``stdout`` stream.

        Returns:
            Decoded JSON object mapping.

        Raises:
            EOFError: If ``stdout`` closes cleanly before a new frame begins.
            LspError: If headers, ``Content-Length``, or JSON body are invalid.
        """
        headers = self._read_headers(stdout)
        content_length = parse_content_length_header(headers, self._options.max_payload_bytes)
        body = self._read_exact(stdout, content_length)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LspError(f"Invalid JSON in LSP payload: {exc}") from exc

        if not isinstance(decoded, dict):
            raise LspError(f"Expected JSON object in LSP payload, got {type(decoded).__name__}.")
        return decoded

    def _read_headers(self, stdout: IO[bytes]) -> Mapping[str, str]:
        """Read ASCII LSP headers up to the ``\\r\\n\\r\\n`` delimiter within size bounds.

        Args:
            stdout: Readable binary ``stdout`` stream.

        Returns:
            Mapping of lowercased header names to string values.

        Raises:
            EOFError: If ``stdout`` reaches EOF prior to any header bytes.
            LspError: If the header block exceeds ``max_header_bytes`` or is truncated.
        """
        max_header_bytes = self._options.max_header_bytes
        total_header_bytes = 0
        headers: dict[str, str] = {}

        while True:
            remaining = max_header_bytes - total_header_bytes
            if remaining <= 0:
                raise LspError(f"LSP header block exceeded {max_header_bytes} bytes.")

            line = self._read_header_line(stdout, remaining, total_header_bytes)
            total_header_bytes += len(line)
            if total_header_bytes > max_header_bytes:
                raise LspError(f"LSP header block exceeded {max_header_bytes} bytes.")

            stripped = line.rstrip(b"\r\n")
            if not stripped:
                return headers

            key, value = parse_header_line(stripped)
            headers[key] = value

    @staticmethod
    def _read_header_line(
        stdout: IO[bytes],
        remaining: int,
        total_header_bytes: int,
    ) -> bytes:
        """Read a single raw header line from ``stdout`` handling EOF and pipe closure.

        Args:
            stdout: Readable binary ``stdout`` stream.
            remaining: Remaining permitted header byte budget.
            total_header_bytes: Bytes already read in the current header block.

        Returns:
            Non-empty raw header line bytes.

        Raises:
            EOFError: If EOF occurs before any bytes of a new frame are read.
            LspError: If ``stdout`` closes mid-header or raises an I/O error.
        """
        try:
            line = stdout.readline(remaining + 1)
        except (OSError, ValueError) as exc:
            if total_header_bytes == 0:
                raise EOFError("LSP server stdout stream closed.") from exc
            raise LspError(f"Failed reading LSP header line: {exc}") from exc

        if not line:
            if total_header_bytes == 0:
                raise EOFError("LSP server closed stdout.")
            raise LspError("LSP server closed stdout mid-header.")
        return line

    @staticmethod
    def _read_exact(stream: IO[bytes], size: int) -> bytes:
        """Read exactly ``size`` bytes from ``stream``.

        Args:
            stream: Readable binary stream.
            size: Exact byte count required.

        Returns:
            Concatenated byte buffer of length ``size``.

        Raises:
            LspError: On stream read error or premature EOF before ``size`` bytes.
        """
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            try:
                chunk = stream.read(remaining)
            except (OSError, ValueError) as exc:
                raise LspError(f"Failed reading LSP payload body: {exc}") from exc
            if not chunk:
                raise LspError(
                    f"Premature EOF while reading LSP payload "
                    f"(expected {size} bytes, got {size - remaining})."
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
