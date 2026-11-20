"""Scope-aware Python AST ``calls`` edge extractor service."""

import ast
from collections import defaultdict, deque
from collections.abc import Sequence

from core.models.edge import Edge
from core.models.enums import CALLABLE_KINDS, PREFIX_EDGE, EdgeKind, SymbolKind
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.utils.logging import get_logger
from parsing.utils.ast_helpers import cap_signature, extract_dotted_parts, safe_unparse
from parsing.utils.naming import build_nested_qualified_name
from parsing.utils.ranges import make_declaration_lines, make_range

logger = get_logger("parsing.services.calls")


class _CallVisitor(ast.NodeVisitor):
    """AST visitor tracking enclosing declaration symbol IDs for every ``ast.Call``."""

    def __init__(
        self,
        file_record: FileRecord,
        module_node_id: str,
        nodes: Sequence[SymbolNode],
        lines: Sequence[str] | None = None,
    ) -> None:
        """Initialize the per-file call visitor.

        Args:
            file_record: Tracked ``FileRecord`` of the source module.
            module_node_id: Root module ``SymbolNode`` identifier.
            nodes: Extracted ``SymbolNode`` sequence for the file.
            lines: Optional pre-split source lines for UTF-8 call ranges.
        """
        self._file_record = file_record
        self._lines = lines
        self.edges: list[Edge] = []
        self._scope_stack: list[str] = [module_node_id]
        self._name_chain: list[str] = []
        self._seen_edge_keys: dict[tuple[str, str, int, int], int] = {}
        self._by_exact: dict[tuple[bool, str, int], str] = {}
        self._by_qname: dict[tuple[bool, str], deque[str]] = defaultdict(deque)
        self._index_nodes(nodes)

    def _index_nodes(self, nodes: Sequence[SymbolNode]) -> None:
        """Index class and callable ``SymbolNode`` IDs by ``(is_class, qname, start_line)``."""
        for node in nodes:
            if node.kind == SymbolKind.CLASS:
                is_cls = True
            elif node.kind in CALLABLE_KINDS:
                is_cls = False
            else:
                continue
            if node.start_line is not None:
                self._by_exact[(is_cls, node.qualified_name, node.start_line)] = node.id
            self._by_qname[(is_cls, node.qualified_name)].append(node.id)

    @staticmethod
    def _classify_call_target(func_expr: ast.AST) -> tuple[str, str | None]:
        """Classify a call callee expression into ``(target_hint, qualifier)``.

        Args:
            func_expr: ``node.func`` expression of an ``ast.Call``.

        Returns:
            Tuple ``(target_hint, qualifier)`` where ``qualifier`` is ``"self"``,
            ``"cls"``, ``"module"``, or ``None``.
        """
        if isinstance(func_expr, ast.Name):
            return func_expr.id, None

        if isinstance(func_expr, ast.Attribute):
            parts = extract_dotted_parts(func_expr)
            if parts is not None and len(parts) >= 2:
                qualifier = parts[0] if parts[0] in ("self", "cls") else "module"
                return ".".join(parts), qualifier

        return cap_signature(safe_unparse(func_expr), max_length=200), None

    def _lookup_symbol_id(self, *, is_class: bool, node: ast.AST, name: str) -> str:
        """Resolve the ``SymbolNode`` ID for an ``ast.ClassDef`` or function definition."""
        qname = build_nested_qualified_name(
            self._file_record.module_path,
            self._name_chain,
            name,
        )
        span = make_declaration_lines(node)
        sym_id = self._by_exact.get((is_class, qname, span.start_line))
        if sym_id is not None:
            return sym_id

        has_exact_for_qname = any(
            k_cls == is_class and k_qname == qname
            for (k_cls, k_qname, _) in self._by_exact
        )
        if not has_exact_for_qname:
            queue = self._by_qname.get((is_class, qname))
            if queue:
                return queue.popleft()
        return self._scope_stack[-1]

    def _visit_scoped_body(
        self,
        sym_id: str,
        name: str,
        body: Sequence[ast.stmt],
    ) -> None:
        """Push ``(sym_id, name)`` around visiting ``body`` statements."""
        self._scope_stack.append(sym_id)
        self._name_chain.append(name)
        try:
            for stmt in body:
                self.visit(stmt)
        finally:
            self._name_chain.pop()
            self._scope_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit class decorators/bases in outer scope and body in class scope.

        Args:
            node: ``ast.ClassDef`` node.
        """
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        for tp in getattr(node, "type_params", []):
            self.visit(tp)

        sym_id = self._lookup_symbol_id(is_class=True, node=node, name=node.name)
        self._visit_scoped_body(sym_id, node.name, node.body)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Visit function signature expressions in outer scope and body in function scope."""
        for dec in node.decorator_list:
            self.visit(dec)
        for tp in getattr(node, "type_params", []):
            self.visit(tp)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)

        sym_id = self._lookup_symbol_id(is_class=False, node=node, name=node.name)
        self._visit_scoped_body(sym_id, node.name, node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit a synchronous function definition.

        Args:
            node: ``ast.FunctionDef`` node.
        """
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Visit an asynchronous function definition.

        Args:
            node: ``ast.AsyncFunctionDef`` node.
        """
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Emit a ``calls`` ``Edge`` for ``node`` and recurse into child expressions.

        Args:
            node: ``ast.Call`` expression node.
        """
        from_id = self._scope_stack[-1]
        target, qualifier = self._classify_call_target(node.func)
        placeholder_id = f"unresolved_call:{target}"

        lineno = getattr(node, "lineno", 0)
        col_offset = getattr(node, "col_offset", 0)
        site_col = col_offset + 1
        edge_key = (from_id, placeholder_id, lineno, col_offset)
        dup_idx = self._seen_edge_keys.get(edge_key, 0)
        self._seen_edge_keys[edge_key] = dup_idx + 1

        repo_id = self._file_record.repo_id
        edge_id = (
            f"{PREFIX_EDGE}{repo_id}:{EdgeKind.CALLS.value}:"
            f"{from_id}@{lineno}:{site_col}->{placeholder_id}"
        )
        if dup_idx > 0:
            edge_id = f"{edge_id}:dup{dup_idx}"

        self.edges.append(
            Edge(
                id=edge_id,
                repo_id=repo_id,
                kind=EdgeKind.CALLS,
                from_id=from_id,
                to_id=placeholder_id,
                source="python-ast",
                confidence=0.7,
                evidence_file_id=self._file_record.id,
                evidence_uri=self._file_record.uri,
                evidence_range=make_range(node, lines=self._lines),
                payload={
                    "target": target,
                    "qualifier": qualifier,
                    "unresolved_target": placeholder_id,
                },
                last_indexed_at=self._file_record.last_indexed_at,
            )
        )
        self.generic_visit(node)


class CallExtractor:
    """Walks AST call sites and emits scope-aware ``calls`` ``Edge`` instances."""

    def extract(
        self,
        file_record: FileRecord,
        module_node_id: str,
        nodes: Sequence[SymbolNode],
        tree: ast.Module,
        *,
        lines: Sequence[str] | None = None,
    ) -> list[Edge]:
        """Extract all ``calls`` ``Edge`` items inside ``tree``.

        Args:
            file_record: Tracked ``FileRecord`` of the source module.
            module_node_id: Root module ``SymbolNode`` identifier.
            nodes: Extracted ``SymbolNode`` declarations for ``file_record``.
            tree: Parsed ``ast.Module`` root node.
            lines: Optional pre-split source lines for UTF-8 call ranges.

        Returns:
            Ordered list of ``calls`` ``Edge`` instances.
        """
        visitor = _CallVisitor(
            file_record=file_record,
            module_node_id=module_node_id,
            nodes=nodes,
            lines=lines,
        )
        visitor.visit(tree)
        return visitor.edges


__all__ = [
    "CallExtractor",
]
