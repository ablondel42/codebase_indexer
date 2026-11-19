"""Read-side symbol navigation, neighborhood subgraph extraction, and freshness service."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from core.models.config import AppConfig
from core.models.edge import Edge
from core.models.enums import PREFIX_SYMBOL, EdgeKind
from core.models.exceptions import SymbolNotFoundError, ValidationError
from core.models.file import FileRecord, FreshnessCheckResult
from core.models.node import SymbolNode, SymbolSummary
from core.ports.uow import UnitOfWorkProtocol
from core.utils.file_io import check_file_disk_freshness
from core.utils.logging import get_logger
from graph.models.subgraph import SubgraphOptions, SubgraphSlice

logger = get_logger("graph.services.navigator")


class GraphNavigator:
    """Pure read-side graph navigation, member lookup, subgraph, and freshness service."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the navigator with optional runtime configuration.

        Args:
            config: Optional ``AppConfig`` controlling file-size bounds on freshness checks.
        """
        self._config = config if config is not None else AppConfig()

    def get_symbol(self, uow: UnitOfWorkProtocol, symbol_id: str) -> SymbolNode | None:
        """Fetch a ``SymbolNode`` by its canonical identifier.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Canonical symbol identifier.

        Returns:
            Matching ``SymbolNode`` or ``None`` if absent.
        """
        return uow.nodes.get_by_id(symbol_id)

    def get_symbol_summary(self, uow: UnitOfWorkProtocol, symbol_id: str) -> SymbolSummary | None:
        """Fetch a ``SymbolSummary`` joined with file and module metadata.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Canonical symbol identifier.

        Returns:
            Matching ``SymbolSummary`` or ``None`` if absent.
        """
        return uow.nodes.get_summary_by_id(symbol_id)

    def get_parent(self, uow: UnitOfWorkProtocol, node: SymbolNode) -> SymbolNode | None:
        """Fetch the structural parent ``SymbolNode`` of ``node`` when present.

        Args:
            uow: Active read Unit of Work.
            node: Child ``SymbolNode``.

        Returns:
            Parent ``SymbolNode`` or ``None`` for root module declarations.
        """
        if node.parent_id is None:
            return None
        return uow.nodes.get_by_id(node.parent_id)

    def get_children(self, uow: UnitOfWorkProtocol, symbol_id: str) -> list[SymbolNode]:
        """List direct structural children of ``symbol_id`` ordered by source position.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Structural parent symbol identifier.

        Returns:
            Ordered list of child ``SymbolNode`` entities.
        """
        return uow.nodes.list_children(symbol_id)

    def get_lexical_parent(self, uow: UnitOfWorkProtocol, node: SymbolNode) -> SymbolNode | None:
        """Fetch the enclosing lexical parent ``SymbolNode`` of ``node`` when present.

        Args:
            uow: Active read Unit of Work.
            node: Nested ``SymbolNode``.

        Returns:
            Lexical parent ``SymbolNode`` or ``None`` if top-level.
        """
        if node.lexical_parent_id is None:
            return None
        return uow.nodes.get_by_id(node.lexical_parent_id)

    def get_lexical_children(self, uow: UnitOfWorkProtocol, symbol_id: str) -> list[SymbolNode]:
        """List direct lexical child declarations nested inside ``symbol_id``.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Enclosing lexical parent symbol identifier.

        Returns:
            Ordered list of lexical child ``SymbolNode`` entities.
        """
        return uow.nodes.list_lexical_children(symbol_id)

    def get_members(
        self,
        uow: UnitOfWorkProtocol,
        symbol_id: str,
        *,
        include_private: bool = True,
    ) -> list[SymbolSummary]:
        """List direct member ``SymbolSummary`` projections of an existing symbol.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Containing module or class symbol identifier.
            include_private: Whether private-like or local members should be included.

        Returns:
            Ordered list of member ``SymbolSummary`` projections.

        Raises:
            SymbolNotFoundError: If ``symbol_id`` does not exist in the graph.
        """
        if uow.nodes.get_by_id(symbol_id) is None:
            raise SymbolNotFoundError(
                f"Symbol not found: {symbol_id}",
                context={"symbol_id": symbol_id},
            )
        return uow.nodes.list_member_summaries(symbol_id, include_private=include_private)

    def list_file_symbols(self, uow: UnitOfWorkProtocol, file_id: str) -> list[SymbolNode]:
        """List all ``SymbolNode`` declarations belonging to ``file_id``.

        Args:
            uow: Active read Unit of Work.
            file_id: Containing ``FileRecord`` identifier.

        Returns:
            Ordered list of ``SymbolNode`` declarations.
        """
        return uow.nodes.list_for_file(file_id)

    def list_file_imports(self, uow: UnitOfWorkProtocol, file_id: str) -> list[Edge]:
        """List ``imports`` ``Edge`` items evidenced by ``file_id`` ordered by line.

        Args:
            uow: Active read Unit of Work.
            file_id: Containing ``FileRecord`` identifier.

        Returns:
            Ordered list of ``imports`` ``Edge`` entities.
        """
        return uow.edges.list_for_file(file_id, EdgeKind.IMPORTS)

    def list_incoming_edges(
        self,
        uow: UnitOfWorkProtocol,
        symbol_id: str,
        kind: EdgeKind | None = None,
    ) -> list[Edge]:
        """List incoming ``Edge`` items targeting ``symbol_id``.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Target symbol identifier.
            kind: Optional ``EdgeKind`` filter.

        Returns:
            Ordered list of incoming ``Edge`` entities.
        """
        return uow.edges.list_incoming(symbol_id, kind)

    def list_outgoing_edges(
        self,
        uow: UnitOfWorkProtocol,
        symbol_id: str,
        kind: EdgeKind | None = None,
    ) -> list[Edge]:
        """List outgoing ``Edge`` items originating from ``symbol_id``.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Source symbol identifier.
            kind: Optional ``EdgeKind`` filter.

        Returns:
            Ordered list of outgoing ``Edge`` entities.
        """
        return uow.edges.list_outgoing(symbol_id, kind)

    def get_file_for_outline(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        file_path: str,
    ) -> FileRecord | None:
        """Resolve a tracked ``FileRecord`` by ``repo_id`` and normalized relative path.

        Strips leading ``./`` prefixes while preserving exact fallback lookup.

        Args:
            uow: Active read Unit of Work.
            repo_id: Owning repository identifier.
            file_path: Candidate repository-relative POSIX file path.

        Returns:
            Matching ``FileRecord`` or ``None`` if absent.
        """
        raw = file_path.strip()
        normalized = raw
        while normalized.startswith("./"):
            normalized = normalized[2:]
        record = uow.files.get_by_path(repo_id, normalized)
        if record is None and normalized != raw:
            record = uow.files.get_by_path(repo_id, raw)
        return record

    def check_file_freshness(
        self,
        uow: UnitOfWorkProtocol,
        file_id: str,
        *,
        file_record: FileRecord | None = None,
        repo_root: Path | str | None = None,
    ) -> FreshnessCheckResult:
        """Verify an indexed file's live on-disk confinement, readability, and SHA-256 hash.

        Args:
            uow: Active read Unit of Work.
            file_id: Target ``FileRecord`` identifier.
            file_record: Optional pre-loaded ``FileRecord`` avoiding an extra lookup.
            repo_root: Optional repository root path override avoiding an extra lookup.

        Returns:
            ``FreshnessCheckResult`` with ``is_fresh``, ``stale_reason``, and ``freshness``.
        """
        record = file_record if file_record is not None else uow.files.get_by_id(file_id)
        if record is None:
            return check_file_disk_freshness(
                None,
                None,
                max_file_size_bytes=self._config.max_file_size_bytes,
            )

        resolved_root = repo_root
        if resolved_root is None:
            repo = uow.repos.get_by_id(record.repo_id)
            resolved_root = repo.root_path if repo is not None else None

        return check_file_disk_freshness(
            resolved_root,
            record,
            max_file_size_bytes=self._config.max_file_size_bytes,
        )

    @staticmethod
    def _collect_hop_candidates(
        step_edges: Sequence[Edge],
        visited_ids: Mapping[str, None],
    ) -> list[str]:
        """Extract deduplicated unvisited ``sym:...`` endpoint IDs from ``step_edges``.

        Args:
            step_edges: Incident edges discovered during the current BFS step.
            visited_ids: Mapping of already-admitted symbol identifiers.

        Returns:
            Ordered list of candidate neighbor symbol IDs.
        """
        candidates: dict[str, None] = {}
        for edge in step_edges:
            for endpoint in (edge.from_id, edge.to_id):
                if endpoint.startswith(PREFIX_SYMBOL) and endpoint not in visited_ids:
                    candidates[endpoint] = None
        return list(candidates)

    @staticmethod
    def _admit_neighbors(
        candidates: Sequence[str],
        found_summaries: Mapping[str, SymbolSummary],
        max_nodes: int,
        visited_ids: dict[str, None],
        summaries_by_id: dict[str, SymbolSummary],
    ) -> tuple[list[str], bool]:
        """Admit resolved neighbor symbols up to ``max_nodes`` and report truncation.

        Args:
            candidates: Ordered candidate symbol IDs for the current BFS step.
            found_summaries: Resolved ``SymbolSummary`` records keyed by ID.
            max_nodes: Maximum total nodes permitted in the subgraph slice.
            visited_ids: Mutable insertion-ordered set of admitted symbol IDs.
            summaries_by_id: Mutable lookup of admitted ``SymbolSummary`` items.

        Returns:
            Tuple ``(next_frontier, step_truncated)``.
        """
        next_frontier: list[str] = []
        step_truncated = False
        for candidate_id in candidates:
            summary = found_summaries.get(candidate_id)
            if summary is None:
                continue
            if len(visited_ids) >= max_nodes:
                step_truncated = True
                continue
            visited_ids[candidate_id] = None
            summaries_by_id[candidate_id] = summary
            next_frontier.append(candidate_id)
        return next_frontier, step_truncated

    @staticmethod
    def _validate_subgraph_roots(
        uow: UnitOfWorkProtocol,
        root_symbol_ids: Sequence[str],
    ) -> tuple[list[str], dict[str, SymbolSummary]]:
        """Validate non-empty root symbol IDs and batch-load their ``SymbolSummary`` items.

        Args:
            uow: Active read Unit of Work.
            root_symbol_ids: Caller-requested seed symbol identifiers.

        Returns:
            Tuple ``(unique_roots, summaries_by_id)``.

        Raises:
            ValidationError: If ``root_symbol_ids`` contains no usable IDs.
            SymbolNotFoundError: If any root symbol ID does not exist in the graph.
        """
        unique_roots = list(dict.fromkeys(sid.strip() for sid in root_symbol_ids if sid.strip()))
        if not unique_roots:
            raise ValidationError(
                "At least one root symbol ID must be provided.",
                context={"root_symbol_ids": list(root_symbol_ids)},
            )
        summaries_by_id = uow.nodes.get_summaries_by_ids(unique_roots)
        missing = [sid for sid in unique_roots if sid not in summaries_by_id]
        if missing:
            raise SymbolNotFoundError(
                f"Symbol not found: {missing[0]}",
                context={"symbol_id": missing[0], "missing_symbol_ids": missing},
            )
        return unique_roots, summaries_by_id

    @staticmethod
    def _assemble_subgraph_slice(
        kept_roots: list[str],
        visited_ids: Mapping[str, None],
        summaries_by_id: Mapping[str, SymbolSummary],
        collected_edges: Mapping[str, Edge],
        depth: int,
        truncated: bool,
    ) -> SubgraphSlice:
        """Assemble deterministically ordered ``SubgraphSlice`` from visited nodes and edges."""
        nodes = [summaries_by_id[sid] for sid in visited_ids]
        edges = sorted(
            (
                edge
                for edge in collected_edges.values()
                if edge.from_id in visited_ids and edge.to_id in visited_ids
            ),
            key=lambda e: (str(e.kind), e.from_id, e.to_id, e.id),
        )
        logger.debug(
            "Built subgraph for %d root(s): %d nodes, %d edges (depth=%d, truncated=%s)",
            len(kept_roots),
            len(nodes),
            len(edges),
            depth,
            truncated,
        )
        return SubgraphSlice(
            root_symbol_ids=kept_roots,
            nodes=nodes,
            edges=edges,
            depth=depth,
            truncated=truncated,
        )

    def build_subgraph(
        self,
        uow: UnitOfWorkProtocol,
        root_symbol_ids: Sequence[str],
        options: SubgraphOptions | None = None,
    ) -> SubgraphSlice:
        """Extract a bounded, deduplicated BFS neighborhood subgraph around ``root_symbol_ids``.

        Args:
            uow: Active read Unit of Work.
            root_symbol_ids: Seed symbol identifiers anchoring the traversal.
            options: Optional ``SubgraphOptions`` controlling ``depth``, ``max_nodes``,
                and ``kinds``.

        Returns:
            ``SubgraphSlice`` with closed node-edge referential integrity and truncation flag.

        Raises:
            ValidationError: If ``root_symbol_ids`` is empty.
            SymbolNotFoundError: If any requested root symbol does not exist.
        """
        opts = options if options is not None else SubgraphOptions()
        unique_roots, summaries_by_id = self._validate_subgraph_roots(uow, root_symbol_ids)

        kept_roots = unique_roots[: opts.max_nodes]
        truncated = len(unique_roots) > opts.max_nodes
        visited_ids: dict[str, None] = dict.fromkeys(kept_roots)
        collected_edges: dict[str, Edge] = {}
        frontier = list(kept_roots)

        for _ in range(opts.depth):
            if not frontier:
                break
            step_edges = uow.edges.list_neighborhood_edges(frontier, kinds=opts.kinds)
            for edge in step_edges:
                collected_edges[edge.id] = edge
            candidates = self._collect_hop_candidates(step_edges, visited_ids)
            found = uow.nodes.get_summaries_by_ids(candidates) if candidates else {}
            frontier, step_truncated = self._admit_neighbors(
                candidates,
                found,
                opts.max_nodes,
                visited_ids,
                summaries_by_id,
            )
            truncated = truncated or step_truncated

        return self._assemble_subgraph_slice(
            kept_roots,
            visited_ids,
            summaries_by_id,
            collected_edges,
            opts.depth,
            truncated,
        )


__all__ = [
    "GraphNavigator",
]
