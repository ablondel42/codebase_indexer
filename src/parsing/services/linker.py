"""Pure in-memory 3-pass ``SymbolLinker`` orchestrator service."""

from collections.abc import Sequence

from core.models.edge import Edge
from core.models.enums import EdgeKind
from core.models.node import SymbolNode
from core.utils.logging import get_logger
from parsing.models.linking import LinkResolutionReport, ResolutionState
from parsing.services.call_resolver import CallLinkResolver
from parsing.services.import_resolver import ImportLinkResolver
from parsing.services.inheritance_resolver import InheritanceLinkResolver
from parsing.services.symbol_index import RepoSymbolIndex

logger = get_logger("parsing.services.linker")


class SymbolLinker:
    """Resolves intra-repository ``imports``, ``inherits``, and ``calls`` edges in memory."""

    def __init__(
        self,
        *,
        import_resolver: ImportLinkResolver | None = None,
        inheritance_resolver: InheritanceLinkResolver | None = None,
        call_resolver: CallLinkResolver | None = None,
    ) -> None:
        """Initialize the 3-pass symbol linker.

        Args:
            import_resolver: Optional injected ``ImportLinkResolver`` (Pass 1).
            inheritance_resolver: Optional injected ``InheritanceLinkResolver`` (Pass 2).
            call_resolver: Optional injected ``CallLinkResolver`` (Pass 3).
        """
        self._import_resolver = (
            import_resolver if import_resolver is not None else ImportLinkResolver()
        )
        self._inheritance_resolver = (
            inheritance_resolver if inheritance_resolver is not None else InheritanceLinkResolver()
        )
        self._call_resolver = call_resolver if call_resolver is not None else CallLinkResolver()

    def resolve(
        self,
        repo_id: str,
        repo_name: str | None,
        nodes: Sequence[SymbolNode],
        candidate_edges: Sequence[Edge],
        *,
        file_id: str | None = None,
    ) -> LinkResolutionReport:
        """Execute Passes 1–3 in memory and return staged ``EdgeTargetUpdate`` records.

        Args:
            repo_id: Owning repository identifier.
            repo_name: Optional repository root directory name for ``repo_name.`` fallback.
            nodes: All active ``SymbolNode`` entities in the repository.
            candidate_edges: Candidate ``imports``, ``inherits``, and ``calls`` edges.
            file_id: Optional single ``file_id`` filter restricting staged updates.

        Returns:
            Immutable ``LinkResolutionReport`` with resolution counts and updates.
        """
        index = RepoSymbolIndex(repo_name, nodes)
        state = ResolutionState(file_id=file_id)

        ordered_edges = sorted(
            candidate_edges,
            key=lambda item: (item.kind.value, item.id),
        )
        import_edges = [e for e in ordered_edges if e.kind == EdgeKind.IMPORTS]
        inherit_edges = [e for e in ordered_edges if e.kind == EdgeKind.INHERITS]
        call_edges = [e for e in ordered_edges if e.kind == EdgeKind.CALLS]

        self._import_resolver.resolve(index, state, import_edges)
        self._inheritance_resolver.resolve(index, state, inherit_edges)
        self._call_resolver.resolve(index, state, call_edges)

        total_resolved = state.imports_resolved + state.inherits_resolved + state.calls_resolved
        logger.debug(
            "Linked repo %s (checked=%d, resolved=%d, reset=%d)",
            repo_id,
            state.edges_checked,
            total_resolved,
            state.edges_reset,
        )
        return LinkResolutionReport(
            repo_id=repo_id,
            file_id=file_id,
            edges_checked=state.edges_checked,
            imports_resolved=state.imports_resolved,
            inherits_resolved=state.inherits_resolved,
            calls_resolved=state.calls_resolved,
            total_resolved=total_resolved,
            edges_reset=state.edges_reset,
            updates=state.updates,
        )


__all__ = [
    "SymbolLinker",
]
