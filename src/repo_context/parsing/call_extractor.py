"""Scope-aware call-site extraction from Python AST."""

import ast
import json
from collections import defaultdict, deque
from collections.abc import Sequence

from repo_context.constants import EDGE_KIND_CALLS
from repo_context.models.file import FileRecord
from repo_context.parsing.naming import build_nested_qualified_name, cap_signature, safe_unparse
from repo_context.parsing.ranges import make_declaration_lines, make_range

_CALLABLE_KINDS = {
    "function",
    "async_function",
    "method",
    "async_method",
    "local_function",
    "local_async_function",
}


def _extract_dotted_parts(expr: ast.AST) -> list[str] | None:
    """Extract dotted identifier parts from ``a.b.c`` expressions, or None if non-simple."""
    if isinstance(expr, ast.Name):
        return [expr.id]
    if isinstance(expr, ast.Attribute):
        prefix = _extract_dotted_parts(expr.value)
        if prefix is not None:
            return [*prefix, expr.attr]
    return None


def _classify_call_target(func_expr: ast.AST) -> tuple[str, str | None]:
    """Classify a call target into ``(target_hint, qualifier)``.

    ``qualifier`` is strictly one of ``"self"``, ``"cls"``, ``"module"``, or ``None``.
    """
    if isinstance(func_expr, ast.Name):
        return func_expr.id, None

    if isinstance(func_expr, ast.Attribute):
        parts = _extract_dotted_parts(func_expr)
        if parts is not None and len(parts) >= 2:
            qualifier = parts[0] if parts[0] in ("self", "cls") else "module"
            return ".".join(parts), qualifier

    return cap_signature(safe_unparse(func_expr), max_length=200), None


class _CallVisitor(ast.NodeVisitor):
    """AST visitor tracking enclosing declaration symbol IDs for every ``ast.Call``."""

    def __init__(
        self,
        repo_id: str,
        file_record: FileRecord,
        module_node_id: str,
        module_path: str,
        nodes: Sequence[dict],
        lines: Sequence[str] | None,
    ) -> None:
        self.repo_id = repo_id
        self.file_record = file_record
        self.module_path = module_path
        self.lines = lines
        self.edges: list[dict] = []
        self._scope_stack: list[str] = [module_node_id]
        self._name_chain: list[str] = []
        self._seen_edge_keys: dict[tuple[str, str, int, int], int] = {}

        self._by_exact: dict[tuple[bool, str, int], str] = {}
        self._by_qname: dict[tuple[bool, str], deque[str]] = defaultdict(deque)
        for node in nodes:
            kind = node.get("kind")
            if kind == "class":
                is_cls = True
            elif kind in _CALLABLE_KINDS:
                is_cls = False
            else:
                continue
            qname = node["qualified_name"]
            start_line = node.get("start_line")
            if start_line is not None:
                self._by_exact[(is_cls, qname, int(start_line))] = node["id"]
            self._by_qname[(is_cls, qname)].append(node["id"])

    def _lookup_symbol_id(self, is_class: bool, node: ast.AST, name: str) -> str:
        """Resolve the symbol node ID for a ClassDef or FunctionDef."""
        qname = build_nested_qualified_name(self.module_path, self._name_chain, name)
        start_line, _ = make_declaration_lines(node)
        sym_id = self._by_exact.get((is_class, qname, start_line))
        if sym_id is not None:
            return sym_id
        # Fallback only when caller passed nodes lacking start_line metadata.
        has_exact_for_qname = any(
            k_cls == is_class and k_qname == qname for (k_cls, k_qname, _) in self._by_exact
        )
        if not has_exact_for_qname:
            queue = self._by_qname.get((is_class, qname))
            if queue:
                return queue.popleft()
        return self._scope_stack[-1]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        for tp in getattr(node, "type_params", []):
            self.visit(tp)

        sym_id = self._lookup_symbol_id(True, node, node.name)
        self._scope_stack.append(sym_id)
        self._name_chain.append(node.name)
        try:
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self._name_chain.pop()
            self._scope_stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for tp in getattr(node, "type_params", []):
            self.visit(tp)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)

        sym_id = self._lookup_symbol_id(False, node, node.name)
        self._scope_stack.append(sym_id)
        self._name_chain.append(node.name)
        try:
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self._name_chain.pop()
            self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        from_id = self._scope_stack[-1]
        target, qualifier = _classify_call_target(node.func)
        to_id = f"unresolved_call:{target}"

        lineno = getattr(node, "lineno", 0)
        col_offset = getattr(node, "col_offset", 0)
        edge_key = (from_id, to_id, lineno, col_offset)
        dup_idx = self._seen_edge_keys.get(edge_key, 0)
        self._seen_edge_keys[edge_key] = dup_idx + 1

        edge_id = f"edge:{self.repo_id}:{EDGE_KIND_CALLS}:{from_id}->{to_id}:{lineno}:{col_offset}"
        if dup_idx > 0:
            edge_id = f"{edge_id}:dup{dup_idx}"

        call_range = make_range(node, lines=self.lines)
        self.edges.append(
            {
                "id": edge_id,
                "repo_id": self.repo_id,
                "kind": EDGE_KIND_CALLS,
                "from_id": from_id,
                "to_id": to_id,
                "source": "python-ast",
                "confidence": 0.7,
                "evidence_file_id": self.file_record.id,
                "evidence_uri": self.file_record.uri,
                "evidence_range_json": json.dumps(call_range, sort_keys=True)
                if call_range
                else None,
                "payload_json": json.dumps(
                    {"target": target, "qualifier": qualifier},
                    sort_keys=True,
                ),
                "last_indexed_at": self.file_record.last_indexed_at,
            }
        )
        self.generic_visit(node)


def extract_call_edges(
    repo_id: str,
    file_record: FileRecord,
    module_node_id: str,
    nodes: Sequence[dict],
    tree: ast.Module,
    *,
    lines: Sequence[str] | None = None,
) -> list[dict]:
    """Extract scope-aware ``calls`` edges for all call-sites in ``tree``.

    Args:
        repo_id: Repository ID.
        file_record: Source file record.
        module_node_id: Module symbol node ID.
        nodes: Extracted symbol nodes for the file.
        tree: Parsed AST module.
        lines: Optional pre-split source lines.

    Returns:
        List of ``calls`` edge dictionaries.
    """
    visitor = _CallVisitor(
        repo_id=repo_id,
        file_record=file_record,
        module_node_id=module_node_id,
        module_path=file_record.module_path,
        nodes=nodes,
        lines=lines,
    )
    visitor.visit(tree)
    return visitor.edges
