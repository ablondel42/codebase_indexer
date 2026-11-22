"""Public re-exports for ``core.ports``."""

from core.ports.chat import ChatStoreProtocol
from core.ports.events import EventCallback
from core.ports.lsp import LspClientProtocol, LspPoolProtocol
from core.ports.repositories import (
    DiagnosticsRepositoryProtocol,
    EdgeRepositoryProtocol,
    FileRepositoryProtocol,
    IndexRunRepositoryProtocol,
    NodeRepositoryProtocol,
    ReferenceRefreshRepositoryProtocol,
    RepoRepositoryProtocol,
    TextSearchRepositoryProtocol,
)
from core.ports.uow import UnitOfWorkFactoryProtocol, UnitOfWorkProtocol
from core.ports.watch import (
    DirectoryWatchHandleProtocol,
    PathChangeCallback,
    WatchEventKind,
)

__all__ = [
    "ChatStoreProtocol",
    "DiagnosticsRepositoryProtocol",
    "DirectoryWatchHandleProtocol",
    "EdgeRepositoryProtocol",
    "EventCallback",
    "FileRepositoryProtocol",
    "IndexRunRepositoryProtocol",
    "LspClientProtocol",
    "LspPoolProtocol",
    "NodeRepositoryProtocol",
    "PathChangeCallback",
    "ReferenceRefreshRepositoryProtocol",
    "RepoRepositoryProtocol",
    "TextSearchRepositoryProtocol",
    "UnitOfWorkFactoryProtocol",
    "UnitOfWorkProtocol",
    "WatchEventKind",
]
