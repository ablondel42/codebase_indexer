"""Hierarchical file outline builder service."""

from collections import defaultdict
from collections.abc import Mapping, Sequence

from context.models.outline import FileOutline, OutlineNode
from context.utils.formatting import deduplicate_imports, is_private_symbol
from core.models.config import AppConfig
from core.models.enums import SymbolKind
from core.models.exceptions import IndexerError, ValidationError
from core.models.node import SymbolNode
from core.ports.uow import UnitOfWorkProtocol
from core.utils.file_io import format_line_span
from core.utils.logging import get_logger
from graph.services.navigator import GraphNavigator
from graph.utils.predicates import derive_target_lines

logger = get_logger("context.services.outline")


class OutlineBuilder:
    """Constructs depth-bounded hierarchical ``FileOutline`` views for indexed files."""

    def __init__(
        self,
        config: AppConfig | None = None,
        navigator: GraphNavigator | None = None,
    ) -> None:
        """Initialize the outline builder with optional configuration and navigator.

        Args:
            config: Optional ``AppConfig`` instance.
            navigator: Optional ``GraphNavigator`` collaborator.
        """
        self._config = config if config is not None else AppConfig()
        self._navigator = navigator if navigator is not None else GraphNavigator(self._config)

    @staticmethod
    def _partition_outline_nodes(
        nodes: Sequence[SymbolNode],
    ) -> tuple[SymbolNode | None, dict[str | None, list[SymbolNode]]]:
        """Separate the root ``module`` symbol from nested declarations grouped by parent ID.

        Args:
            nodes: Start-line-ordered ``SymbolNode`` declarations for a file.

        Returns:
            Tuple ``(module_node, children_by_parent_id)``.
        """
        module_node: SymbolNode | None = None
        non_module_nodes: list[SymbolNode] = []
        for node in nodes:
            if node.kind == SymbolKind.MODULE and module_node is None:
                module_node = node
            else:
                non_module_nodes.append(node)

        module_node_id = module_node.id if module_node is not None else None
        non_module_ids = {node.id for node in non_module_nodes}
        children_by_parent: dict[str | None, list[SymbolNode]] = defaultdict(list)

        for node in non_module_nodes:
            if (
                node.parent_id == module_node_id
                or node.parent_id is None
                or node.parent_id not in non_module_ids
            ):
                children_by_parent[None].append(node)
            else:
                children_by_parent[node.parent_id].append(node)

        return module_node, children_by_parent

    @classmethod
    def _build_subtree(
        cls,
        children_by_parent: Mapping[str | None, Sequence[SymbolNode]],
        parent_key: str | None,
        *,
        depth: int,
        max_depth: int,
        include_private: bool,
    ) -> list[OutlineNode]:
        """Recursively assemble ``OutlineNode`` subtrees up to ``max_depth``.

        Args:
            children_by_parent: Declarations grouped by structural parent ID.
            parent_key: Parent symbol ID (``None`` for top-level declarations).
            depth: Current 1-based recursion depth.
            max_depth: Maximum permitted nesting depth.
            include_private: Whether private-like or local symbols should be included.

        Returns:
            Ordered list of ``OutlineNode`` items.
        """
        if depth > max_depth:
            return []
        items: list[OutlineNode] = []
        for child in children_by_parent.get(parent_key, ()):
            if not include_private and is_private_symbol(child):
                continue
            nested = (
                cls._build_subtree(
                    children_by_parent,
                    child.id,
                    depth=depth + 1,
                    max_depth=max_depth,
                    include_private=include_private,
                )
                if depth < max_depth
                else []
            )
            start_line, end_line = derive_target_lines(
                child.start_line,
                child.end_line,
                child.decl_range,
            )
            items.append(
                OutlineNode(
                    id=child.id,
                    kind=child.kind,
                    name=child.name,
                    lines=format_line_span(start_line, end_line),
                    signature=child.signature,
                    doc=child.doc_summary,
                    children=nested,
                )
            )
        return items

    def build_outline(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        file_path: str,
        *,
        include_private: bool = True,
        max_depth: int = 3,
    ) -> FileOutline:
        """Build a hierarchical ``FileOutline`` for ``file_path`` inside ``repo_id``.

        Args:
            uow: Active read Unit of Work.
            repo_id: Owning repository identifier.
            file_path: Repository-relative POSIX file path.
            include_private: Whether private-like and local symbols should be included.
            max_depth: Maximum nesting depth (``1`` = top-level only; ``0`` = metadata only).

        Returns:
            Populated ``FileOutline`` model.

        Raises:
            ValidationError: If ``max_depth < 0``.
            IndexerError: With ``error_code="file_not_found"`` when ``file_path`` is unindexed.
        """
        if max_depth < 0:
            raise ValidationError("max_depth must be >= 0", context={"max_depth": max_depth})

        file_record = self._navigator.get_file_for_outline(uow, repo_id, file_path)
        if file_record is None:
            raise IndexerError(
                f"File not found in index: {file_path}",
                error_code="file_not_found",
                context={"repo_id": repo_id, "file_path": file_path},
            )

        freshness = self._navigator.check_file_freshness(
            uow,
            file_record.id,
            file_record=file_record,
        )
        nodes = self._navigator.list_file_symbols(uow, file_record.id)
        import_edges = self._navigator.list_file_imports(uow, file_record.id)
        module_node, children_by_parent = self._partition_outline_nodes(nodes)

        symbols = (
            self._build_subtree(
                children_by_parent,
                None,
                depth=1,
                max_depth=max_depth,
                include_private=include_private,
            )
            if max_depth >= 1
            else []
        )
        logger.debug(
            "Built outline for %s (%d top-level symbols, fresh=%s)",
            file_record.file_path,
            len(symbols),
            freshness.is_fresh,
        )
        return FileOutline(
            file_path=file_record.file_path,
            module_path=file_record.module_path,
            line_count=file_record.line_count,
            is_fresh=freshness.is_fresh,
            stale_reason=freshness.stale_reason if not freshness.is_fresh else None,
            doc=module_node.doc_summary if module_node is not None else None,
            imports=deduplicate_imports(import_edges, module_node),
            symbols=symbols,
        )


__all__ = [
    "OutlineBuilder",
]
