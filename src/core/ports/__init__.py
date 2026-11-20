"""Public re-exports for ``core.ports``."""

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

__all__ = [
    "DiagnosticsRepositoryProtocol",
    "EdgeRepositoryProtocol",
    "EventCallback",
    "FileRepositoryProtocol",
    "IndexRunRepositoryProtocol",
    "LspClientProtocol",
    "LspPoolProtocol",
    "NodeRepositoryProtocol",
    "ReferenceRefreshRepositoryProtocol",
    "RepoRepositoryProtocol",
    "TextSearchRepositoryProtocol",
    "UnitOfWorkFactoryProtocol",
    "UnitOfWorkProtocol",
]
