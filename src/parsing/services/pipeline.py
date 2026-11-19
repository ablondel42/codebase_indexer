"""End-to-end single-file Python AST extraction pipeline service."""

import ast
from collections.abc import Sequence
from pathlib import Path

from core.models.config import AppConfig
from core.models.edge import Edge
from core.models.enums import CALLABLE_KINDS, SymbolKind
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.utils.logging import get_logger
from core.utils.pathing import ensure_safe_repo_file
from parsing.models.extraction import (
    DeclarationWalkResult,
    ExtractionMetrics,
    FileGraphExtraction,
)
from parsing.services.calls import CallExtractor
from parsing.services.imports import ImportExtractor
from parsing.services.inheritance import InheritanceExtractor
from parsing.services.modules import ModuleExtractor
from parsing.services.scope import FileExtractionSession
from parsing.services.walker import DeclarationWalker
from parsing.utils.ast_helpers import load_python_source, parse_python_ast

logger = get_logger("parsing.services.pipeline")


class AstExtractionPipeline:
    """Coordinates AST parsing, ``DeclarationWalker``, and edge extractors for a file."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the AST extraction pipeline.

        Args:
            config: Optional ``AppConfig`` supplying ``max_file_size_bytes``.
        """
        self._config = config if config is not None else AppConfig()
        self._module_extractor = ModuleExtractor()
        self._walker = DeclarationWalker()
        self._import_extractor = ImportExtractor()
        self._inheritance_extractor = InheritanceExtractor()
        self._call_extractor = CallExtractor()

    @staticmethod
    def _enrich_module_node(
        module_node: SymbolNode,
        nodes: Sequence[SymbolNode],
        imported_modules: list[str],
        imported_symbols: list[str],
    ) -> SymbolNode:
        """Return a copy of ``module_node`` with import and top-level child payload keys."""
        top_level_symbol_ids = [
            node.id for node in nodes if node.parent_id == module_node.id
        ]
        updated_payload = {
            **module_node.payload,
            "imported_modules": imported_modules,
            "imported_symbols": imported_symbols,
            "top_level_symbol_ids": top_level_symbol_ids,
        }
        return module_node.model_copy(update={"payload": updated_payload})

    @staticmethod
    def _build_metrics(
        file_record: FileRecord,
        nodes: Sequence[SymbolNode],
        edges: Sequence[Edge],
        class_count: int,
        call_edge_count: int,
    ) -> ExtractionMetrics:
        """Compute per-file ``ExtractionMetrics`` from extracted nodes and edges."""
        method_count = sum(
            1
            for node in nodes
            if node.kind in (SymbolKind.METHOD, SymbolKind.ASYNC_METHOD)
        )
        callable_count = sum(1 for node in nodes if node.kind in CALLABLE_KINDS)
        return ExtractionMetrics(
            file_id=file_record.id,
            file_path=file_record.file_path,
            node_count=len(nodes),
            edge_count=len(edges),
            module_count=1,
            class_count=class_count,
            callable_count=callable_count,
            method_count=method_count,
            constant_count=sum(1 for node in nodes if node.kind == SymbolKind.CONSTANT),
            attribute_count=sum(1 for node in nodes if node.kind == SymbolKind.ATTRIBUTE),
            type_alias_count=sum(
                1 for node in nodes if node.kind == SymbolKind.TYPE_ALIAS
            ),
            call_edge_count=call_edge_count,
        )

    def _extract_edges_and_enrich_module(
        self,
        session: FileExtractionSession,
        initial_module: SymbolNode,
        walk_result: DeclarationWalkResult,
        tree: ast.Module,
    ) -> tuple[SymbolNode, list[Edge], int]:
        """Extract AST dependency edges and enrich the module node's import payload."""
        file_rec = session.file_record
        import_result = self._import_extractor.extract(
            file_rec,
            initial_module.id,
            tree,
            lines=session.lines,
        )
        inheritance_edges = self._inheritance_extractor.extract(
            file_rec,
            walk_result.class_nodes,
            tree,
            lines=session.lines,
        )
        call_edges = self._call_extractor.extract(
            file_rec,
            initial_module.id,
            walk_result.nodes,
            tree,
            lines=session.lines,
        )
        enriched_module = self._enrich_module_node(
            initial_module,
            walk_result.nodes,
            import_result.imported_modules,
            import_result.imported_symbols,
        )
        all_edges = [
            *import_result.edges,
            *walk_result.structural_edges,
            *inheritance_edges,
            *call_edges,
        ]
        return enriched_module, all_edges, len(call_edges)

    def extract_source(
        self,
        file_record: FileRecord,
        source_text: str,
        *,
        filename: Path | str | None = None,
    ) -> FileGraphExtraction:
        """Extract all ``SymbolNode`` and ``Edge`` entities from in-memory ``source_text``.

        Args:
            file_record: Tracked ``FileRecord`` metadata for the source file.
            source_text: Decoded UTF-8 Python source text.
            filename: Optional diagnostic filename passed to ``ast.parse``.

        Returns:
            Immutable ``FileGraphExtraction`` containing nodes, edges, and metrics.

        Raises:
            ParseError: If Python AST parsing fails.
        """
        diag_name = filename if filename is not None else file_record.file_path
        tree = parse_python_ast(source_text, filename=diag_name)
        session = FileExtractionSession(
            file_record=file_record,
            lines=source_text.splitlines(),
        )
        initial_module = self._module_extractor.extract(session, tree, source_text)
        walk_result = self._walker.walk(session, initial_module, tree)
        enriched_module, final_edges, call_count = self._extract_edges_and_enrich_module(
            session,
            initial_module,
            walk_result,
            tree,
        )
        final_nodes = [enriched_module, *walk_result.nodes[1:]]
        metrics = self._build_metrics(
            file_record,
            final_nodes,
            final_edges,
            len(walk_result.class_nodes),
            call_count,
        )
        return FileGraphExtraction(
            file=file_record,
            nodes=final_nodes,
            edges=final_edges,
            metrics=metrics,
        )

    def extract_file(
        self,
        repo_root: Path | str,
        file_record: FileRecord,
    ) -> FileGraphExtraction:
        """Safely load ``file_record`` from ``repo_root`` and extract its AST graph.

        Args:
            repo_root: Repository root directory path.
            file_record: Tracked ``FileRecord`` to parse and extract.

        Returns:
            Immutable ``FileGraphExtraction`` containing nodes, edges, and metrics.

        Raises:
            PathSecurityError: If ``file_record.file_path`` escapes ``repo_root`` or is a symlink.
            FileNotFoundError: If the source file does not exist on disk.
            ParseError: If the file exceeds ``max_file_size_bytes``, is invalid UTF-8,
                or fails AST parsing.
        """
        safe_path = ensure_safe_repo_file(Path(repo_root), file_record.file_path)
        source_text = load_python_source(
            safe_path,
            max_file_size_bytes=self._config.max_file_size_bytes,
        )
        extraction = self.extract_source(file_record, source_text, filename=safe_path)
        logger.debug(
            "Extracted %d nodes and %d edges from %s",
            extraction.metrics.node_count,
            extraction.metrics.edge_count,
            file_record.file_path,
        )
        return extraction


__all__ = [
    "AstExtractionPipeline",
]
