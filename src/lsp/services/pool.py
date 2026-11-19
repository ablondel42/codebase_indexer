"""Thread-safe per-repository ``LspSessionPool`` implementing ``LspPoolProtocol``."""

import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import unquote, urlparse

from core.models.config import AppConfig
from core.models.exceptions import IndexerError, LspError
from core.ports.lsp import LspClientProtocol
from core.utils.logging import get_logger
from core.utils.pathing import ensure_safe_repo_file
from lsp.models.protocol import LspClientOptions
from lsp.services.client import PyrightLspClient

logger = get_logger("lsp.services.pool")


class _SessionSlot:
    """Per-repository synchronized slot holding a cached ``LspClientProtocol`` session."""

    __slots__ = ("client", "lock")

    def __init__(self) -> None:
        """Initialize an empty session slot with a reentrant lock."""
        self.lock = threading.RLock()
        self.client: LspClientProtocol | None = None


class LspSessionPool:
    """Thread-safe cache of warm ``LspClientProtocol`` sessions keyed by ``repo_root``."""

    def __init__(
        self,
        options: LspClientOptions | None = None,
        *,
        config: AppConfig | None = None,
        client_factory: Callable[[], LspClientProtocol] | None = None,
    ) -> None:
        """Initialize an empty ``LspSessionPool`` with no global state.

        Args:
            options: Optional explicit ``LspClientOptions`` for default client creation.
            config: Optional ``AppConfig`` supplying timeout and file-size bounds.
            client_factory: Optional zero-argument factory returning ``LspClientProtocol``
                instances (primarily for dependency injection and testing).
        """
        self._options = options
        self._config = config or AppConfig()
        self._client_factory = client_factory
        self._pool_lock = threading.Lock()
        self._slots: dict[Path, _SessionSlot] = {}

    def acquire(self, repo_root: Path | str) -> LspClientProtocol:
        """Return a started ``LspClientProtocol`` session bound to ``repo_root``.

        Reuses an existing healthy session when present, or replaces a terminated
        session cleanly.

        Args:
            repo_root: Repository root directory.

        Returns:
            Started ``LspClientProtocol`` instance bound to ``repo_root``.

        Raises:
            LspError: If spawning or initializing the client session fails.
        """
        root_key = self._normalize_repo_root(repo_root)
        slot = self._get_slot(root_key)
        with slot.lock:
            if slot.client is not None and slot.client.is_running:
                logger.debug("Reusing warm LSP client session for %s", root_key)
                return slot.client

            if slot.client is not None:
                logger.debug("Replacing inactive cached LSP session for %s", root_key)
                self._safe_close_client(slot.client, root_key)
                slot.client = None

            client = self._build_client()
            client.start(root_key)
            slot.client = client
            logger.info("Started pooled LSP client session for %s", root_key)
            return client

    def notify_did_open_or_change(
        self,
        repo_root: Path | str,
        uri: str,
        text: str | None = None,
    ) -> LspClientProtocol:
        """Synchronize an open or modified document buffer on the warm session for ``repo_root``.

        When ``text`` is ``None``, validates path confinement under ``repo_root`` and reads
        the bounded UTF-8 file text from disk.

        Args:
            repo_root: Repository root directory.
            uri: Document ``file://`` URI.
            text: Optional in-memory UTF-8 document text; read from disk if ``None``.

        Returns:
            Active ``LspClientProtocol`` session after synchronizing ``uri``.

        Raises:
            PathSecurityError: If ``uri`` resolves outside ``repo_root`` or traverses symlinks.
            LspError: If ``uri`` cannot be read or LSP synchronization fails.
        """
        root_key = self._normalize_repo_root(repo_root)
        resolved_text = self._read_uri_text(root_key, uri) if text is None else text

        slot = self._get_slot(root_key)
        with slot.lock:
            client = self.acquire(root_key)
            try:
                client.did_open_or_change(uri, resolved_text)
                return client
            except (IndexerError, OSError, RuntimeError):
                self._safe_close_client(client, root_key)
                slot.client = None
                raise

    def evict(self, repo_root: Path | str) -> None:
        """Close and evict any cached LSP client session for ``repo_root``.

        Args:
            repo_root: Repository root directory to evict.
        """
        root_key = self._normalize_repo_root(repo_root)
        with self._pool_lock:
            slot = self._slots.pop(root_key, None)
        if slot is None:
            return
        with slot.lock:
            if slot.client is not None:
                client = slot.client
                slot.client = None
                self._safe_close_client(client, root_key)
                logger.info("Evicted pooled LSP session for %s", root_key)

    def close_all(self) -> None:
        """Close and clear all active LSP client sessions in the pool."""
        with self._pool_lock:
            items = list(self._slots.items())
            self._slots.clear()

        for root_key, slot in items:
            with slot.lock:
                if slot.client is not None:
                    client = slot.client
                    slot.client = None
                    self._safe_close_client(client, root_key)

    def __enter__(self) -> Self:
        """Enter the session pool context manager.

        Returns:
            This ``LspSessionPool`` instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager, closing all cached LSP sessions.

        Args:
            exc_type: Exception class if raised inside the ``with`` block, else ``None``.
            exc_val: Exception instance if raised inside the ``with`` block, else ``None``.
            exc_tb: Traceback if an exception was raised, else ``None``.
        """
        self.close_all()

    @staticmethod
    def _normalize_repo_root(repo_root: Path | str) -> Path:
        """Resolve ``repo_root`` to a canonical absolute ``Path`` key.

        Args:
            repo_root: Candidate repository root path.

        Returns:
            Resolved ``Path`` key.
        """
        return Path(repo_root).resolve()

    def _get_slot(self, root_key: Path) -> _SessionSlot:
        """Fetch or lazily create the ``_SessionSlot`` for ``root_key``.

        Args:
            root_key: Canonical resolved repository root path.

        Returns:
            Synchronized ``_SessionSlot`` instance.
        """
        with self._pool_lock:
            slot = self._slots.get(root_key)
            if slot is None:
                slot = _SessionSlot()
                self._slots[root_key] = slot
            return slot

    def _build_client(self) -> LspClientProtocol:
        """Construct a fresh unstarted ``LspClientProtocol`` instance.

        Returns:
            Unstarted ``LspClientProtocol`` instance.
        """
        if self._client_factory is not None:
            return self._client_factory()
        return PyrightLspClient(options=self._options, config=self._config)

    def _read_uri_text(self, root_key: Path, uri: str) -> str:
        """Validate ``uri`` confinement under ``root_key`` and read bounded UTF-8 text.

        Args:
            root_key: Resolved repository root directory.
            uri: Candidate ``file://`` URI string.

        Returns:
            Decoded UTF-8 file text.

        Raises:
            PathSecurityError: If ``uri`` escapes ``root_key`` or traverses symlinks.
            LspError: If ``uri`` is not a ``file://`` URI, exceeds size bounds, or fails read.
        """
        parsed = urlparse(uri)
        if parsed.scheme not in ("file", "") or not parsed.path:
            raise LspError(
                f"Cannot read text from non-file URI: {uri}",
                context={"uri": uri},
            )
        safe_path = ensure_safe_repo_file(root_key, Path(unquote(parsed.path)))
        max_bytes = self._config.max_file_size_bytes
        try:
            size_bytes = safe_path.stat().st_size
            if size_bytes > max_bytes:
                raise LspError(
                    f"File {safe_path} ({size_bytes} bytes) exceeds "
                    f"max_file_size_bytes ({max_bytes}).",
                    context={
                        "file_path": str(safe_path),
                        "size_bytes": size_bytes,
                        "limit": max_bytes,
                    },
                )
            return safe_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LspError(
                f"Unable to read file {safe_path}: {exc}",
                context={"file_path": str(safe_path)},
            ) from exc

    @staticmethod
    def _safe_close_client(client: LspClientProtocol, root_key: Path) -> None:
        """Close ``client`` while logging any teardown failure at ``WARNING`` level.

        Args:
            client: Pooled ``LspClientProtocol`` session to close.
            root_key: Associated repository root for structured log context.
        """
        try:
            client.close()
        except (IndexerError, OSError, RuntimeError) as exc:
            logger.warning("Error closing pooled LSP client for %s: %s", root_key, exc)
