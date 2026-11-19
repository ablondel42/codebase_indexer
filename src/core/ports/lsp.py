"""Language Server Protocol client and session-pool ``Protocol`` ports."""

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.models.range import Position


@runtime_checkable
class LspClientProtocol(Protocol):
    """Driven port for a stateful language server JSON-RPC client session."""

    @property
    def is_running(self) -> bool:
        """Return ``True`` when the language server subprocess and session are active."""
        ...

    def start(self, repo_root: Path | str) -> None:
        """Spawn the language server subprocess and complete the ``initialize`` handshake.

        Args:
            repo_root: Repository root directory for workspace initialization.
        """
        ...

    def did_open_or_change(
        self,
        uri: str,
        text: str,
        language_id: str = "python",
    ) -> None:
        """Synchronize an in-memory document buffer via ``didOpen`` or ``didChange``.

        Args:
            uri: Document ``file://`` URI.
            text: Full current UTF-8 document text.
            language_id: LSP language identifier (default ``"python"``).
        """
        ...

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
            position: Zero-based UTF-16 position (line and UTF-16 character offset)
                of the queried symbol.
            include_declaration: Whether the declaration site itself should be returned.

        Returns:
            Raw LSP ``Location`` / ``LocationLink`` dictionaries returned by the server.
        """
        ...

    def close(self) -> None:
        """Gracefully shut down and terminate the underlying language server process."""
        ...


@runtime_checkable
class LspPoolProtocol(Protocol):
    """Driven port managing reusable per-repository ``LspClientProtocol`` sessions."""

    def acquire(self, repo_root: Path) -> LspClientProtocol:
        """Return a started ``LspClientProtocol`` session for ``repo_root``.

        Args:
            repo_root: Repository root directory.

        Returns:
            Started ``LspClientProtocol`` instance bound to ``repo_root``.
        """
        ...

    def evict(self, repo_root: Path) -> None:
        """Close and evict any cached LSP client session for ``repo_root``.

        Args:
            repo_root: Repository root directory to evict.
        """
        ...

    def close_all(self) -> None:
        """Close and clear all active LSP client sessions in the pool."""
        ...
