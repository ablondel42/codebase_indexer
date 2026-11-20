"""Recursive AST declaration walker eliminating extractor circular dependencies."""

import ast
from collections.abc import Sequence

from core.models.edge import Edge
from core.models.enums import EdgeKind, SymbolKind, SymbolScope
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.utils.logging import get_logger
from parsing.models.extraction import DeclarationWalkResult, ScopeTarget
from parsing.services.assignments import AssignmentExtractor
from parsing.services.callables import CallableExtractor
from parsing.services.classes import ClassExtractor
from parsing.services.scope import FileExtractionSession
from parsing.utils.naming import make_structural_edge_id

logger = get_logger("parsing.services.walker")

_AstMaps = tuple[dict[int, SymbolNode], dict[str, ast.ClassDef]]


class DeclarationWalker:
    """Traverses module, class, and function bodies to build symbols and structural edges."""

    def __init__(
        self,
        class_extractor: ClassExtractor | None = None,
        callable_extractor: CallableExtractor | None = None,
        assignment_extractor: AssignmentExtractor | None = None,
    ) -> None:
        """Initialize the walker with leaf extractors."""
        self._class_extractor = class_extractor if class_extractor is not None else ClassExtractor()
        self._callable_extractor = (
            callable_extractor if callable_extractor is not None else CallableExtractor()
        )
        self._assignment_extractor = (
            assignment_extractor if assignment_extractor is not None else AssignmentExtractor()
        )

    def _walk_scope_body(
        self,
        session: FileExtractionSession,
        target: ScopeTarget,
        scope_info: tuple[str, SymbolScope],
        body: Sequence[ast.stmt],
        ast_maps: _AstMaps,
    ) -> list[SymbolNode]:
        """Walk nested classes and callables inside ``body`` under an active scope frame."""
        scope_name, scope_kind = scope_info
        children: list[SymbolNode] = []
        with session.scope_tracker.scope_context(target.parent_id, scope_name, scope_kind):
            for stmt in body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    children.extend(self._walk_callable_def(session, target, stmt, ast_maps))
                elif isinstance(stmt, ast.ClassDef):
                    children.extend(self._walk_class_def(session, stmt, (None, target), ast_maps))
        return children

    def _walk_class_def(
        self,
        session: FileExtractionSession,
        node: ast.ClassDef,
        enclosing: tuple[str | None, ScopeTarget | None],
        ast_maps: _AstMaps,
    ) -> list[SymbolNode]:
        """Extract one ``ast.ClassDef`` and all nested methods/classes in canonical order."""
        module_node_id, target = enclosing
        symbols_by_ast_id, class_ast_by_symbol_id = ast_maps
        placement = self._class_extractor.resolve_placement(
            session,
            node.name,
            module_node_id=module_node_id,
            target=target,
        )
        node_id = session.duplicate_tracker.get_symbol_id(
            session.repo_id,
            SymbolKind.CLASS,
            placement.qualified_name,
        )
        class_target = ScopeTarget(
            parent_id=node_id,
            parent_qualified_name=placement.qualified_name,
            scope=SymbolScope.CLASS,
        )
        child_nodes = self._walk_scope_body(
            session,
            class_target,
            (node.name, SymbolScope.CLASS),
            node.body,
            ast_maps,
        )
        method_ids = [
            child.id
            for child in child_nodes
            if child.parent_id == node_id
            and child.kind in (SymbolKind.METHOD, SymbolKind.ASYNC_METHOD)
        ]
        cls_node = self._class_extractor.extract(
            session,
            node,
            node_id,
            placement,
            method_ids=method_ids,
        )
        symbols_by_ast_id[id(node)] = cls_node
        class_ast_by_symbol_id[cls_node.id] = node
        return [cls_node, *child_nodes]

    def _walk_callable_def(
        self,
        session: FileExtractionSession,
        target: ScopeTarget,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        ast_maps: _AstMaps,
    ) -> list[SymbolNode]:
        """Extract one callable node and all nested local callables/classes in its body."""
        symbols_by_ast_id, _ = ast_maps
        callable_node = self._callable_extractor.extract(session, target, node)
        symbols_by_ast_id[id(node)] = callable_node
        func_target = ScopeTarget(
            parent_id=callable_node.id,
            parent_qualified_name=callable_node.qualified_name,
            scope=SymbolScope.FUNCTION,
            lexical_parent_id=callable_node.id,
        )
        nested = self._walk_scope_body(
            session,
            func_target,
            (node.name, SymbolScope.FUNCTION),
            node.body,
            ast_maps,
        )
        return [callable_node, *nested]

    @staticmethod
    def _make_contains_edge(
        repo_id: str,
        from_id: str,
        child_node: SymbolNode,
        file_record: FileRecord,
    ) -> Edge:
        """Create a direct structural ``contains`` edge from ``from_id`` to ``child_node``."""
        return Edge(
            id=make_structural_edge_id(repo_id, EdgeKind.CONTAINS, from_id, child_node.id),
            repo_id=repo_id,
            kind=EdgeKind.CONTAINS,
            from_id=from_id,
            to_id=child_node.id,
            source="python-ast",
            confidence=1.0,
            evidence_file_id=file_record.id,
            evidence_uri=file_record.uri,
            evidence_range=child_node.decl_range,
            payload={},
            last_indexed_at=file_record.last_indexed_at,
        )

    @staticmethod
    def _make_scope_parent_edge(
        repo_id: str,
        from_id: str,
        to_id: str,
        file_record: FileRecord,
    ) -> Edge:
        """Create a ``scope_parent`` edge for function-local lexical nesting."""
        return Edge(
            id=make_structural_edge_id(repo_id, EdgeKind.SCOPE_PARENT, from_id, to_id),
            repo_id=repo_id,
            kind=EdgeKind.SCOPE_PARENT,
            from_id=from_id,
            to_id=to_id,
            source="python-ast",
            confidence=1.0,
            evidence_file_id=file_record.id,
            evidence_uri=file_record.uri,
            evidence_range=None,
            payload={},
            last_indexed_at=file_record.last_indexed_at,
        )

    def _build_structural_edges(
        self,
        repo_id: str,
        file_record: FileRecord,
        nodes: Sequence[SymbolNode],
    ) -> list[Edge]:
        """Build direct ``contains`` and function-local ``scope_parent`` edges."""
        structural_edges: list[Edge] = []
        for node in nodes[1:]:
            if node.parent_id is not None:
                structural_edges.append(
                    self._make_contains_edge(repo_id, node.parent_id, node, file_record)
                )
            if node.lexical_parent_id is not None:
                structural_edges.append(
                    self._make_scope_parent_edge(
                        repo_id,
                        node.id,
                        node.lexical_parent_id,
                        file_record,
                    )
                )
        return structural_edges

    def walk(
        self,
        session: FileExtractionSession,
        module_node: SymbolNode,
        tree: ast.Module,
    ) -> DeclarationWalkResult:
        """Walk ``tree`` in canonical order and build symbol nodes and structural edges."""
        symbols_by_ast_id: dict[int, SymbolNode] = {id(tree): module_node}
        class_ast_by_symbol_id: dict[str, ast.ClassDef] = {}
        ast_maps: _AstMaps = (symbols_by_ast_id, class_ast_by_symbol_id)

        class_tree_nodes: list[SymbolNode] = []
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                class_tree_nodes.extend(
                    self._walk_class_def(session, stmt, (module_node.id, None), ast_maps)
                )

        module_target = ScopeTarget(
            parent_id=module_node.id,
            parent_qualified_name=session.module_path,
            scope=SymbolScope.MODULE,
        )
        top_level_callables: list[SymbolNode] = []
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                top_level_callables.extend(
                    self._walk_callable_def(session, module_target, stmt, ast_maps)
                )

        all_class_nodes = [
            node
            for node in [*class_tree_nodes, *top_level_callables]
            if node.kind == SymbolKind.CLASS
        ]
        assignment_nodes = self._assignment_extractor.extract_all(
            session,
            module_node.id,
            tree,
            class_nodes=all_class_nodes,
            class_ast_by_symbol_id=class_ast_by_symbol_id,
        )
        nodes = [module_node, *class_tree_nodes, *top_level_callables, *assignment_nodes]
        structural_edges = self._build_structural_edges(
            session.repo_id,
            session.file_record,
            nodes,
        )
        return DeclarationWalkResult(
            nodes=nodes,
            class_nodes=all_class_nodes,
            structural_edges=structural_edges,
            symbols_by_ast_id=symbols_by_ast_id,
            class_ast_by_symbol_id=class_ast_by_symbol_id,
        )


__all__ = [
    "DeclarationWalker",
]
