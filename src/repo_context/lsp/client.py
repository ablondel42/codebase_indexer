"""Thread-safe stdio JSON-RPC LSP client and session pool for pyright-langserver."""

from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any
from urllib.parse import unquote, urlparse

from repo_context.config import AppConfig, get_config
from repo_context.exceptions import LspError
from repo_context.logging_config import get_logger
from repo_context.parsing.pathing import ensure_safe_repo_file

logger = get_logger("lsp.client")

MAX_HEADER_BYTES = 8192
MAX_STDERR_LINE_BYTES = 4096
STDERR_HISTORY_LIMIT = 200
SHUTDOWN_TIMEOUT_SECONDS = 2.0
TERMINATE_WAIT_SECONDS = 2.0


class _PendingRequest:
    """Waiter state for an in-flight client-to-server JSON-RPC request."""

    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None
        self.error: LspError | None = None


class PyrightLspClient:
    """Thread-safe stdio JSON-RPC client for pyright-langserver.

    Uses dedicated daemon reader threads for stdout and stderr so pipe buffers
    never deadlock, distinguishes JSON-RPC responses from server-initiated
    requests, bounds header/payload sizes, enforces request timeouts, and
    reaps subprocesses cleanly on close.
    """

    def __init__(
        self,
        server_cmd: Sequence[str] | None = None,
        *,
        timeout: float | None = None,
        max_payload_bytes: int | None = None,
        config: AppConfig | None = None,
    ) -> None:
        cfg = config or get_config()
        self.server_cmd: list[str] = (
            list(server_cmd) if server_cmd is not None else ["pyright-langserver", "--stdio"]
        )
        self.timeout: float = float(timeout if timeout is not None else cfg.lsp_timeout_seconds)
        self.max_payload_bytes: int = int(
            max_payload_bytes if max_payload_bytes is not None else cfg.max_lsp_payload_bytes
        )

        self.proc: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._started = False
        self._closing = False
        self._repo_root: Path | None = None

        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int | str, _PendingRequest] = {}
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=STDERR_HISTORY_LIMIT)
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._reader_error: LspError | None = None

        self._document_versions: dict[str, int] = {}
        self._document_hashes: dict[str, str] = {}

    @property
    def is_running(self) -> bool:
        """Return True if the LSP server subprocess is started and alive."""
        proc = self.proc
        return (
            self._started
            and not self._closing
            and proc is not None
            and proc.poll() is None
            and self._reader_error is None
        )

    @property
    def stderr_lines(self) -> list[str]:
        """Return a snapshot of recent stderr lines drained from the server."""
        return list(self._stderr_lines)

    def _reset_session_state(self) -> None:
        """Reset per-session counters, caches, and tracking containers before spawn."""
        self._closing = False
        self._started = False
        self._reader_error = None
        self._next_id = 1
        self._pending.clear()
        self._stderr_lines.clear()
        self._document_versions.clear()
        self._document_hashes.clear()

    def _spawn_server_process(self) -> None:
        """Validate server executable, launch subprocess, and start stdio reader threads."""
        if not self.server_cmd:
            raise LspError("LSP server command cannot be empty.")

        executable = self.server_cmd[0]
        if shutil.which(executable) is None:
            raise LspError(
                f"LSP server executable '{executable}' not found in PATH.",
                context={"executable": executable},
            )

        try:
            self.proc = subprocess.Popen(
                self.server_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.proc = None
            raise LspError(
                f"Failed to launch LSP server '{executable}': {exc}",
                context={"server_cmd": self.server_cmd},
            ) from exc

        if self.proc.stdin is None or self.proc.stdout is None or self.proc.stderr is None:
            self._cleanup_process(send_shutdown=False)
            raise LspError("LSP server pipes were not created.")

        self._stdout_thread = threading.Thread(
            target=self._stdout_loop,
            args=(self.proc.stdout,),
            name="pyright-lsp-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            args=(self.proc.stderr,),
            name="pyright-lsp-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _initialize_workspace(self, repo_root: str | Path) -> None:
        """Perform LSP initialize handshake for ``repo_root`` or clean up on failure."""
        resolved_root = Path(repo_root).resolve()
        root_uri = resolved_root.as_uri()
        try:
            self._send_request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": root_uri,
                    "capabilities": {},
                    "workspaceFolders": [
                        {
                            "name": resolved_root.name or "workspace",
                            "uri": root_uri,
                        }
                    ],
                },
                timeout=self.timeout,
            )
            self.notify("initialized", {})
            self._repo_root = resolved_root
            self._started = True
        except Exception as exc:
            self._cleanup_process(send_shutdown=False)
            if isinstance(exc, LspError):
                raise
            raise LspError(f"LSP initialize failed: {exc}") from exc

    def start(self, repo_root: str | Path) -> None:
        """Start the LSP server subprocess and complete initialization."""
        with self._lifecycle_lock:
            if self.is_running:
                return
            if self.proc is not None:
                self._cleanup_process(send_shutdown=False)
            self._reset_session_state()
            self._spawn_server_process()
            self._initialize_workspace(repo_root)

    def _stderr_loop(self, stderr: IO[bytes]) -> None:
        """Drain stderr continuously into a bounded deque to prevent pipe stalls."""
        try:
            while True:
                line = stderr.readline(MAX_STDERR_LINE_BYTES)
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                self._stderr_lines.append(text)
                logger.debug("LSP stderr: %s", text)
        except (OSError, ValueError):
            pass

    def _stdout_loop(self, stdout: IO[bytes]) -> None:
        """Read and dispatch JSON-RPC frames from stdout."""
        try:
            while not self._closing:
                msg = self._read_message(stdout)
                self._dispatch_message(msg)
        except EOFError:
            if not self._closing:
                err = LspError(
                    "LSP server closed stdout unexpectedly.",
                    context={"stderr_tail": list(self._stderr_lines)[-10:]},
                )
                self._reader_error = err
                self._fail_all_pending(err)
        except LspError as err:
            if not self._closing:
                self._reader_error = err
                self._fail_all_pending(err)
        except Exception as exc:
            if not self._closing:
                err = LspError(
                    f"Unexpected LSP stdout reader failure: {exc}",
                    context={"stderr_tail": list(self._stderr_lines)[-10:]},
                )
                self._reader_error = err
                self._fail_all_pending(err)

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """Route a parsed JSON-RPC message by response, server request, or notification."""
        if "id" in msg and "method" not in msg:
            msg_id = msg["id"]
            with self._pending_lock:
                waiter = self._pending.pop(msg_id, None)
            if waiter is not None:
                waiter.response = msg
                waiter.event.set()
            else:
                logger.debug("Ignoring unmatched LSP response id=%r", msg_id)
            return

        if "id" in msg and "method" in msg:
            self._handle_server_request(msg)
            return

        if "method" in msg:
            logger.debug("LSP notification: %s", msg.get("method"))
            return

        logger.debug("Ignoring malformed LSP message without id/method")

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        """Reply immediately to server-initiated JSON-RPC requests so the server never stalls."""
        req_id = msg["id"]
        method = msg["method"]
        params = msg.get("params")

        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, dict) else None
            result: Any = [None] * len(items) if isinstance(items, list) else []
        else:
            result = None

        try:
            self._write_message({"jsonrpc": "2.0", "id": req_id, "result": result})
        except LspError as exc:
            logger.debug("Failed replying to server request %s (id=%r): %s", method, req_id, exc)

    def _fail_all_pending(self, err: LspError) -> None:
        """Wake all pending request waiters with an error."""
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            waiter.error = err
            waiter.event.set()

    @staticmethod
    def _read_exact(stream: IO[bytes], size: int) -> bytes:
        """Read exactly `size` bytes from `stream`, raising LspError on premature EOF."""
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                raise LspError(
                    f"Premature EOF while reading LSP payload "
                    f"(expected {size} bytes, got {size - remaining})."
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse_header_line(stripped: bytes) -> tuple[str, str]:
        """Decode and validate one ASCII ``Key: Value`` LSP header line."""
        try:
            decoded = stripped.decode("ascii")
        except UnicodeDecodeError as exc:
            raise LspError(f"Non-ASCII LSP header line: {exc}") from exc

        if ":" not in decoded:
            raise LspError(f"Malformed LSP header line: {decoded!r}")

        key, value = decoded.split(":", 1)
        return key.strip().lower(), value.strip()

    def _read_headers(self, stdout: IO[bytes]) -> dict[str, str]:
        """Read LSP message headers up to the blank delimiter within ``MAX_HEADER_BYTES``."""
        total_header_bytes = 0
        headers: dict[str, str] = {}

        while True:
            remaining = MAX_HEADER_BYTES - total_header_bytes
            if remaining <= 0:
                raise LspError(f"LSP header block exceeded {MAX_HEADER_BYTES} bytes.")

            line = stdout.readline(remaining + 1)
            if not line:
                if total_header_bytes == 0:
                    raise EOFError("LSP server closed stdout.")
                raise LspError("LSP server closed stdout mid-header.")

            total_header_bytes += len(line)
            if total_header_bytes > MAX_HEADER_BYTES:
                raise LspError(f"LSP header block exceeded {MAX_HEADER_BYTES} bytes.")

            stripped = line.rstrip(b"\r\n")
            if not stripped:
                return headers

            key, value = self._parse_header_line(stripped)
            headers[key] = value

    def _parse_content_length(self, headers: dict[str, str]) -> int:
        """Extract and validate bounded positive ``Content-Length`` from LSP headers."""
        raw_length = headers.get("content-length")
        if raw_length is None:
            raise LspError("Missing Content-Length header in LSP message.")

        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise LspError(f"Invalid Content-Length header: {raw_length!r}") from exc

        if content_length <= 0:
            raise LspError(f"Invalid non-positive Content-Length: {content_length}")

        if content_length > self.max_payload_bytes:
            raise LspError(
                f"LSP Content-Length {content_length} exceeds max_lsp_payload_bytes "
                f"({self.max_payload_bytes}).",
                context={"content_length": content_length, "limit": self.max_payload_bytes},
            )
        return content_length

    def _read_message(self, stdout: IO[bytes]) -> dict[str, Any]:
        """Read and validate one framed JSON-RPC message from stdout."""
        headers = self._read_headers(stdout)
        content_length = self._parse_content_length(headers)
        body = self._read_exact(stdout, content_length)
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LspError(f"Invalid JSON in LSP payload: {exc}") from exc

        if not isinstance(message, dict):
            raise LspError(f"Expected JSON object in LSP payload, got {type(message).__name__}.")

        return message

    def _write_message(self, payload: dict[str, Any]) -> None:
        """Serialize and write a framed JSON-RPC message to stdin under a write lock."""
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        frame = header + body

        with self._write_lock:
            proc = self.proc
            if proc is None or proc.stdin is None:
                raise LspError("LSP server stdin is unavailable.")
            try:
                proc.stdin.write(frame)
                proc.stdin.flush()
            except (OSError, ValueError, BrokenPipeError) as exc:
                raise LspError(f"Failed writing to LSP server stdin: {exc}") from exc

    def _send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send a JSON-RPC request and wait up to `timeout` seconds for its matching response."""
        if self.proc is None or self._closing:
            raise LspError("LSP client is not running.")
        if self._reader_error is not None:
            raise self._reader_error

        wait_timeout = self.timeout if timeout is None else float(timeout)
        waiter = _PendingRequest()

        with self._pending_lock:
            if self._reader_error is not None:
                raise self._reader_error
            msg_id = self._next_id
            self._next_id += 1
            self._pending[msg_id] = waiter

        try:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "method": method,
                    "params": params if params is not None else {},
                }
            )
        except Exception:
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise

        if not waiter.event.wait(timeout=wait_timeout):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise LspError(
                f"LSP request '{method}' timed out after {wait_timeout}s.",
                context={
                    "method": method,
                    "timeout": wait_timeout,
                    "stderr_tail": list(self._stderr_lines)[-10:],
                },
            )

        if waiter.error is not None:
            raise waiter.error

        response = waiter.response or {}
        if "error" in response and response["error"] is not None:
            raise LspError(
                f"LSP error for {method}: {response['error']}",
                context={"method": method, "error": response["error"]},
            )
        return response.get("result")

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send a JSON-RPC request after initialization and return its result."""
        if not self._started:
            raise LspError("LSP client has not been started.")
        return self._send_request(method, params, timeout=timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self.proc is None or (self._closing and method != "exit"):
            raise LspError("LSP client is not running.")
        self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params if params is not None else {},
            }
        )

    def did_open(self, uri: str, text: str, *, version: int = 1) -> None:
        """Send textDocument/didOpen notification."""
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": version,
                    "text": text,
                }
            },
        )
        self._document_versions[uri] = version
        self._document_hashes[uri] = hashlib.sha256(text.encode("utf-8")).hexdigest()

    def did_change(self, uri: str, text: str, *, version: int | None = None) -> None:
        """Send textDocument/didChange notification with full document sync."""
        next_version = (
            version if version is not None else self._document_versions.get(uri, 0) + 1
        )
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": next_version},
                "contentChanges": [{"text": text}],
            },
        )
        self._document_versions[uri] = next_version
        self._document_hashes[uri] = hashlib.sha256(text.encode("utf-8")).hexdigest()

    def did_open_or_change(self, uri: str, text: str) -> None:
        """Open a document if unseen, or send didChange if its contents changed."""
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if uri not in self._document_versions:
            self.did_open(uri, text, version=1)
        elif self._document_hashes.get(uri) != text_hash:
            self.did_change(uri, text)

    def is_document_open(self, uri: str) -> bool:
        """Return True if a document URI currently has an open version tracked."""
        return uri in self._document_versions

    def find_references(
        self,
        uri: str,
        line: int,
        character: int,
        include_declaration: bool = False,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Request references for a 0-based LSP position."""
        return self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": include_declaration},
            },
            timeout=timeout,
        )

    def _send_graceful_shutdown(self, proc: subprocess.Popen[bytes]) -> None:
        """Send best-effort LSP shutdown request and exit notification when healthy."""
        if proc.poll() is not None or self._reader_error is not None:
            return
        with contextlib.suppress(Exception):
            self._send_request("shutdown", {}, timeout=SHUTDOWN_TIMEOUT_SECONDS)
        with contextlib.suppress(Exception):
            self.notify("exit", {})

    @staticmethod
    def _terminate_and_reap_proc(proc: subprocess.Popen[bytes]) -> None:
        """Close stdin, terminate subprocess, and force-kill if wait times out."""
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()

        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

        try:
            proc.wait(timeout=TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            proc.wait()

    def _join_threads_and_close_pipes(self, proc: subprocess.Popen[bytes]) -> None:
        """Join reader daemon threads briefly and close stdout/stderr pipes."""
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)

        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()

    def _cleanup_process(self, *, send_shutdown: bool) -> None:
        """Terminate and reap the LSP subprocess without leaking pipes or zombies."""
        proc = self.proc
        if proc is None:
            self._started = False
            self._closing = False
            return

        if send_shutdown:
            self._send_graceful_shutdown(proc)

        self._closing = True
        self._fail_all_pending(LspError("LSP client closed."))
        self._terminate_and_reap_proc(proc)
        self._join_threads_and_close_pipes(proc)

        self.proc = None
        self._started = False
        self._stdout_thread = None
        self._stderr_thread = None
        self._document_versions.clear()
        self._document_hashes.clear()

    def close(self) -> None:
        """Shutdown, exit, and reap the LSP server process cleanly."""
        with self._lifecycle_lock:
            self._cleanup_process(send_shutdown=True)

    def __enter__(self) -> PyrightLspClient:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()


class _SessionSlot:
    """Per-repository synchronized wrapper around a cached PyrightLspClient."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.client: PyrightLspClient | None = None
        self.opened_uris: set[str] = set()


class LspSessionPool:
    """Thread-safe cache of warm PyrightLspClient sessions keyed by resolved repo_root."""

    def __init__(
        self,
        server_cmd: Sequence[str] | None = None,
        *,
        config: AppConfig | None = None,
        client_factory: Callable[[], PyrightLspClient] | None = None,
    ) -> None:
        self._server_cmd = list(server_cmd) if server_cmd is not None else None
        self._config = config
        self._client_factory = client_factory
        self._pool_lock = threading.Lock()
        self._slots: dict[Path, _SessionSlot] = {}

    @staticmethod
    def _normalize_repo_root(repo_root: str | Path) -> Path:
        return Path(repo_root).resolve()

    def _get_slot(self, root_key: Path) -> _SessionSlot:
        with self._pool_lock:
            slot = self._slots.get(root_key)
            if slot is None:
                slot = _SessionSlot()
                self._slots[root_key] = slot
            return slot

    def _build_client(self) -> PyrightLspClient:
        if self._client_factory is not None:
            return self._client_factory()
        return PyrightLspClient(server_cmd=self._server_cmd, config=self._config)

    def get_or_start(self, repo_root: str | Path) -> PyrightLspClient:
        """Return an active PyrightLspClient for `repo_root`, starting one if needed."""
        root_key = self._normalize_repo_root(repo_root)
        slot = self._get_slot(root_key)
        with slot.lock:
            if slot.client is not None and slot.client.is_running:
                return slot.client

            if slot.client is not None:
                with contextlib.suppress(Exception):
                    slot.client.close()
                slot.client = None
                slot.opened_uris.clear()

            client = self._build_client()
            client.start(str(root_key))
            slot.client = client
            slot.opened_uris.clear()
            return client

    def notify_did_open_or_change(
        self,
        repo_root: str | Path,
        uri: str,
        text: str | None = None,
    ) -> PyrightLspClient:
        """Notify the warm session for `repo_root` that a document opened or changed."""
        root_key = self._normalize_repo_root(repo_root)
        if text is None:
            parsed = urlparse(uri)
            if parsed.scheme not in ("file", "") or not parsed.path:
                raise LspError(f"Cannot read text from non-file URI: {uri}")
            safe_path = ensure_safe_repo_file(root_key, Path(unquote(parsed.path)))
            cfg = self._config or get_config()
            try:
                size_bytes = safe_path.stat().st_size
                if size_bytes > cfg.max_file_size_bytes:
                    raise LspError(
                        f"File {safe_path} ({size_bytes} bytes) exceeds "
                        f"max_file_size_bytes ({cfg.max_file_size_bytes})."
                    )
                text = safe_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise LspError(f"Unable to read file {safe_path}: {exc}") from exc

        slot = self._get_slot(root_key)
        with slot.lock:
            client = self.get_or_start(root_key)
            try:
                is_open = (
                    client.is_document_open(uri)
                    if hasattr(client, "is_document_open")
                    else (uri in slot.opened_uris)
                )
                if is_open:
                    client.did_change(uri, text)
                else:
                    client.did_open(uri, text, version=1)
                slot.opened_uris.add(uri)
                return client
            except Exception:
                with contextlib.suppress(Exception):
                    client.close()
                slot.client = None
                slot.opened_uris.clear()
                raise

    def close_repo(self, repo_root: str | Path) -> None:
        """Close and evict the warm LSP session for `repo_root`, if present."""
        root_key = self._normalize_repo_root(repo_root)
        with self._pool_lock:
            slot = self._slots.pop(root_key, None)
        if slot is None:
            return
        with slot.lock:
            if slot.client is not None:
                try:
                    slot.client.close()
                finally:
                    slot.client = None
                    slot.opened_uris.clear()

    @staticmethod
    def _close_slot(slot: _SessionSlot) -> None:
        """Close and clear a single session slot safely."""
        with slot.lock:
            if slot.client is None:
                return
            try:
                slot.client.close()
            except Exception as exc:
                logger.debug("Ignoring error closing pooled LSP client: %s", exc)
            finally:
                slot.client = None
                slot.opened_uris.clear()

    def close_all(self) -> None:
        """Close and evict all cached LSP sessions."""
        with self._pool_lock:
            slots = list(self._slots.values())
            self._slots.clear()

        for slot in slots:
            self._close_slot(slot)

    def __enter__(self) -> LspSessionPool:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close_all()
