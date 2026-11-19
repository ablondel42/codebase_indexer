"""Stateful ``PyrightLspClient`` implementing ``LspClientProtocol``."""

import threading
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from core.models.config import AppConfig
from core.models.exceptions import LspError
from core.models.range import Position
from core.utils.hashing import sha256_text
from core.utils.logging import get_logger
from lsp.models.protocol import LspClientOptions
from lsp.services.process import PyrightProcess
from lsp.services.transport import JsonRpcTransport

logger = get_logger("lsp.services.client")


class PyrightLspClient:
    """Thread-safe LSP client session composing ``PyrightProcess`` and ``JsonRpcTransport``."""

    def __init__(
        self,
        options: LspClientOptions | None = None,
        *,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize an unstarted ``PyrightLspClient`` session.

        Args:
            options: Optional explicit ``LspClientOptions``. Takes precedence over ``config``.
            config: Optional ``AppConfig`` used to derive timeout and payload bounds when
                ``options`` is omitted.
        """
        if options is not None:
            resolved_options = options
        elif config is not None:
            resolved_options = LspClientOptions.from_config(config)
        else:
            resolved_options = LspClientOptions()

        self._options = resolved_options
        self._process = PyrightProcess(resolved_options)
        self._transport = JsonRpcTransport(self._process, resolved_options)
        self._lifecycle_lock = threading.RLock()
        self._started = False
        self._repo_root: Path | None = None
        self._document_versions: dict[str, int] = {}
        self._document_hashes: dict[str, str] = {}

    @property
    def options(self) -> LspClientOptions:
        """Return the immutable ``LspClientOptions`` configured for this client."""
        return self._options

    @property
    def pid(self) -> int | None:
        """Return the active LSP server subprocess PID, or ``None`` if not running."""
        return self._process.pid

    @property
    def is_running(self) -> bool:
        """Return ``True`` when the subprocess and JSON-RPC session are active."""
        return self._started and self._transport.is_healthy

    @property
    def stderr_lines(self) -> list[str]:
        """Return a snapshot of recent ``stderr`` lines emitted by the language server."""
        return self._process.stderr_lines

    def is_document_open(self, uri: str) -> bool:
        """Return ``True`` when ``uri`` has already been opened via ``didOpen``.

        Args:
            uri: Document ``file://`` URI.

        Returns:
            ``True`` if ``uri`` currently has a tracked open version.
        """
        return uri in self._document_versions

    def document_version(self, uri: str) -> int | None:
        """Return the current tracked integer version for ``uri``, or ``None`` if unopened.

        Args:
            uri: Document ``file://`` URI.

        Returns:
            Current integer document version or ``None``.
        """
        return self._document_versions.get(uri)

    def start(self, repo_root: Path | str) -> None:
        """Spawn the language server subprocess and complete the ``initialize`` handshake.

        No-ops when the client session is already running and healthy.

        Args:
            repo_root: Repository root directory for workspace initialization.

        Raises:
            LspError: If spawning the server or completing ``initialize`` fails.
        """
        with self._lifecycle_lock:
            if self.is_running:
                return
            if self._process.is_alive:
                self._teardown_session(send_shutdown=False)

            self._started = False
            self._document_versions.clear()
            self._document_hashes.clear()

            resolved_root = Path(repo_root).resolve()
            self._process.start()
            self._transport.start()
            self._initialize_workspace(resolved_root)

    def did_open(
        self,
        uri: str,
        text: str,
        language_id: str = "python",
        *,
        version: int = 1,
    ) -> None:
        """Send a ``textDocument/didOpen`` notification and record document hash/version.

        Args:
            uri: Document ``file://`` URI.
            text: Full UTF-8 document text.
            language_id: LSP language identifier (default ``"python"``).
            version: Initial document version number (default ``1``).

        Raises:
            LspError: If the client has not been started or transport write fails.
        """
        with self._lifecycle_lock:
            self._ensure_started()
            self._transport.send_notification(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": version,
                        "text": text,
                    }
                },
            )
            self._document_versions[uri] = version
            self._document_hashes[uri] = sha256_text(text)

    def did_change(
        self,
        uri: str,
        text: str,
        *,
        version: int | None = None,
    ) -> None:
        """Send a full-sync ``textDocument/didChange`` notification and bump document version.

        Args:
            uri: Document ``file://`` URI.
            text: Updated full UTF-8 document text.
            version: Optional explicit version override; defaults to ``current + 1``.

        Raises:
            LspError: If the client has not been started or transport write fails.
        """
        with self._lifecycle_lock:
            self._ensure_started()
            next_version = (
                version if version is not None else self._document_versions.get(uri, 0) + 1
            )
            self._transport.send_notification(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": next_version},
                    "contentChanges": [{"text": text}],
                },
            )
            self._document_versions[uri] = next_version
            self._document_hashes[uri] = sha256_text(text)

    def did_open_or_change(
        self,
        uri: str,
        text: str,
        language_id: str = "python",
    ) -> None:
        """Synchronize ``uri`` via ``didOpen`` or ``didChange`` when SHA-256 hash differs.

        Skips redundant ``didChange`` notifications when ``sha256_text(text)`` matches
        the last synchronized content hash for ``uri``.

        Args:
            uri: Document ``file://`` URI.
            text: Full current UTF-8 document text.
            language_id: LSP language identifier (default ``"python"``).

        Raises:
            LspError: If the client has not been started or transport write fails.
        """
        text_hash = sha256_text(text)
        with self._lifecycle_lock:
            if uri not in self._document_versions:
                self.did_open(uri, text, language_id=language_id, version=1)
            elif self._document_hashes.get(uri) != text_hash:
                self.did_change(uri, text)
            else:
                logger.debug("Skipping redundant didChange for unchanged document %s", uri)

    def find_references(
        self,
        uri: str,
        position: Position,
        *,
        include_declaration: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute ``textDocument/references`` at a zero-based UTF-16 ``Position``.

        Args:
            uri: Target document ``file://`` URI.
            position: Zero-based UTF-16 position (line and UTF-16 character offset).
            include_declaration: Whether the declaration site itself should be returned.

        Returns:
            List of raw LSP ``Location`` / ``LocationLink`` dictionaries.

        Raises:
            LspError: If the client is not started, the request fails/times out, or
                the server returns a malformed result type.
        """
        self._ensure_started()
        response = self._transport.send_request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": position.line, "character": position.character},
                "context": {"includeDeclaration": include_declaration},
            },
        )
        raw_result = response.result
        if raw_result is None:
            return []
        if not isinstance(raw_result, list):
            raise LspError(
                f"Expected list or null from textDocument/references, "
                f"got {type(raw_result).__name__}."
            )
        return [dict(item) for item in raw_result if isinstance(item, Mapping)]

    def close(self) -> None:
        """Gracefully send ``shutdown`` + ``exit`` and reap the language server process."""
        with self._lifecycle_lock:
            self._teardown_session(send_shutdown=True)

    def __enter__(self) -> Self:
        """Enter the client context manager.

        Returns:
            This ``PyrightLspClient`` instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the client context manager, closing the underlying LSP session.

        Args:
            exc_type: Exception class if raised inside the ``with`` block, else ``None``.
            exc_val: Exception instance if raised inside the ``with`` block, else ``None``.
            exc_tb: Traceback if an exception was raised, else ``None``.
        """
        self.close()

    def _ensure_started(self) -> None:
        """Verify that ``start()`` has completed before issuing LSP operations.

        Raises:
            LspError: If ``start()`` has not succeeded on this session.
        """
        if not self._started:
            raise LspError("LSP client has not been started.")

    def _initialize_workspace(self, resolved_root: Path) -> None:
        """Perform the LSP ``initialize`` + ``initialized`` handshake for ``resolved_root``.

        Args:
            resolved_root: Resolved repository root directory path.

        Raises:
            LspError: If the handshake fails or times out.
        """
        root_uri = resolved_root.as_uri()
        try:
            self._transport.send_request(
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
                timeout=self._options.request_timeout_seconds,
            )
            self._transport.send_notification("initialized", {})
            self._repo_root = resolved_root
            self._started = True
            logger.info(
                "Initialized Pyright LSP workspace for %s (pid=%s)",
                resolved_root,
                self._process.pid,
            )
        except LspError:
            self._teardown_session(send_shutdown=False)
            raise
        except (OSError, ValueError) as exc:
            self._teardown_session(send_shutdown=False)
            raise LspError(
                f"LSP initialize failed for {resolved_root}: {exc}",
                context={"repo_root": str(resolved_root)},
            ) from exc

    def _send_graceful_shutdown(self) -> None:
        """Send best-effort ``shutdown`` request and ``exit`` notification before stop."""
        try:
            self._transport.send_request(
                "shutdown",
                {},
                timeout=self._options.shutdown_timeout_seconds,
            )
        except LspError as exc:
            logger.debug("LSP shutdown request failed or timed out during close: %s", exc)

        try:
            self._transport.send_notification("exit", {}, allow_while_closing=True)
        except LspError as exc:
            logger.debug("LSP exit notification failed during close: %s", exc)

    def _teardown_session(self, *, send_shutdown: bool) -> None:
        """Shut down transport, stop subprocess, join reader thread, and clear buffers.

        Args:
            send_shutdown: Whether to attempt ``shutdown`` + ``exit`` when healthy.
        """
        if not self._process.is_alive and not self._started:
            self._document_versions.clear()
            self._document_hashes.clear()
            return

        if send_shutdown and self._transport.is_healthy:
            self._send_graceful_shutdown()

        self._transport.begin_close()
        self._process.stop()
        self._transport.join_reader()

        self._started = False
        self._repo_root = None
        self._document_versions.clear()
        self._document_hashes.clear()
