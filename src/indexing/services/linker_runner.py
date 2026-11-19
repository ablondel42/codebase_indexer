"""Repository symbol-linking coordinator bridging ``UnitOfWorkProtocol`` and ``SymbolLinker``."""

from core.models.enums import AST_DEPENDENCY_EDGE_KINDS
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from parsing.models.linking import LinkResolutionReport
from parsing.services.linker import SymbolLinker

logger = get_logger("indexing.services.linker_runner")


class RepoLinkCoordinator:
    """Loads repository symbols/edges from ``UnitOfWorkProtocol`` and persists link updates."""

    def __init__(self, linker: SymbolLinker | None = None) -> None:
        """Initialize the link coordinator with an optional ``SymbolLinker``.

        Args:
            linker: Optional injected ``SymbolLinker`` instance.
        """
        self._linker = linker if linker is not None else SymbolLinker()

    def link_repository(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        *,
        file_id: str | None = None,
    ) -> LinkResolutionReport:
        """Resolve intra-repository AST edges and persist target updates via ``uow``.

        Loads all active ``SymbolNode`` declarations and ``AST_DEPENDENCY_EDGE_KINDS``
        candidate edges across ``repo_id`` so cross-file ``__init__.py`` re-exports,
        base-class hierarchies, and broken cross-file placeholder resets work cleanly.

        Args:
            uow: Active ``UnitOfWorkProtocol`` providing ``repos``, ``nodes``, and ``edges``.
            repo_id: Target repository identifier.
            file_id: Optional ``file_id`` restricting staged edge updates to one file.

        Returns:
            Immutable ``LinkResolutionReport`` summarizing resolved and reset edges.
        """
        repo = uow.repos.get_by_id(repo_id)
        repo_name = repo.name if repo is not None else None
        nodes = uow.nodes.list_for_repo(repo_id)
        candidate_edges = uow.edges.list_for_repo(repo_id, kinds=AST_DEPENDENCY_EDGE_KINDS)

        report = self._linker.resolve(
            repo_id,
            repo_name,
            nodes,
            candidate_edges,
            file_id=file_id,
        )
        if report.updates:
            uow.edges.update_resolved_targets(report.updates)

        logger.debug(
            "Linked repository %s (checked=%d, resolved=%d, reset=%d, updates=%d)",
            repo_id,
            report.edges_checked,
            report.total_resolved,
            report.edges_reset,
            len(report.updates),
        )
        return report


__all__ = [
    "RepoLinkCoordinator",
]
