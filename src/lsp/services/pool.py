"""Thread-safe per-repository ``LspSessionPool`` implementing ``LspPoolProtocol``."""

import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import urlparse

from core.models.config import AppConfig
from core.models.exceptions import IndexerError, LspError
from core.ports.lsp import LspClientProtocol
from core.utils.logging import get_logger
from core.utils.pathing import ensure_safe_repo_file, uri_to_file_path
from lsp.models.protocol import LspClientOptions
from lsp.services.client import PyrightLspClient
from lsp.utils.locations import read_bounded_lsp_text

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
        """Initialize an empty ``LspSessionPool`` with no global state."""
        self._options = options
        self._config = config or AppConfig()
        self._client_factory = client_factory
        self._pool_lock = threading.Lock()
        self._slots: dict[Path, _SessionSlot] = {}

    def _get_or_spawn_locked(self, slot: _SessionSlot, root_key: Path) -> LspClientProtocol:
        """Return a running session inside ``slot`` (caller must hold ``slot.lock``)."""
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

    def acquire(self, repo_root: Path | str) -> LspClientProtocol:
        """Return a started ``LspClientProtocol`` session bound to ``repo_root``."""
        root_key = self._normalize_repo_root(repo_root)
        slot = self._get_slot(root_key)
        with slot.lock:
            return self._get_or_spawn_locked(slot, root_key)

    def notify_did_open_or_change(
        self,
        repo_root: Path | str,
        uri: str,
        text: str | None = None,
    ) -> LspClientProtocol:
        """Synchronize an open or modified document buffer on the warm session for ``repo_root``."""
        root_key = self._normalize_repo_root(repo_root)
        resolved_text = self._read_uri_text(root_key, uri) if text is None else text

        slot = self._get_slot(root_key)
        with slot.lock:
            client = self._get_or_spawn_locked(slot, root_key)
            try:
                client.did_open_or_change(uri, resolved_text)
                return client
            except (IndexerError, OSError, RuntimeError):
                self._safe_close_client(client, root_key)
                slot.client = None
                raise

    def evict(self, repo_root: Path | str) -> None:
        """Close and evict any cached LSP client session for ``repo_root``."""
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
        """Enter the session pool context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager, closing all cached LSP sessions."""
        self.close_all()

    @staticmethod
    def _normalize_repo_root(repo_root: Path | str) -> Path:
        """Resolve ``repo_root`` to a canonical absolute ``Path`` key."""
        return Path(repo_root).resolve()

    def _get_slot(self, root_key: Path) -> _SessionSlot:
        """Fetch or lazily create the ``_SessionSlot`` for ``root_key``."""
        with self._pool_lock:
            slot = self._slots.get(root_key)
            if slot is None:
                slot = _SessionSlot()
                self._slots[root_key] = slot
            return slot

    def _build_client(self) -> LspClientProtocol:
        """Construct a fresh unstarted ``LspClientProtocol`` instance."""
        if self._client_factory is not None:
            return self._client_factory()
        return PyrightLspClient(options=self._options, config=self._config)

    def _read_uri_text(self, root_key: Path, uri: str) -> str:
        """Validate ``uri`` confinement under ``root_key`` and read bounded UTF-8 text."""
        decoded_path = uri_to_file_path(uri)
        if decoded_path is None:
            parsed = urlparse(uri)
            if parsed.scheme != "" or not parsed.path:
                raise LspError(
                    f"Cannot read text from non-file URI: {uri}",
                    context={"uri": uri},
                )
            decoded_path = parsed.path
        safe_path = ensure_safe_repo_file(root_key, Path(decoded_path))
        return read_bounded_lsp_text(safe_path, self._config.max_file_size_bytes)

    @staticmethod
    def _safe_close_client(client: LspClientProtocol, root_key: Path) -> None:
        """Close ``client`` while logging any teardown failure at ``WARNING`` level."""
        try:
            client.close()
        except (IndexerError, OSError, RuntimeError) as exc:
            logger.warning("Error closing pooled LSP session for %s: %s", root_key, exc)
