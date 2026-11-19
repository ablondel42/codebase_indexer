"""Module constant, ``TypeAlias``, and class-level attribute extractor service."""

import ast
import hashlib
import sys
from collections.abc import Sequence
from typing import Any

from core.models.enums import PREFIX_SHA256, Language, SymbolKind, SymbolScope
from core.models.node import SymbolNode
from core.utils.hashing import compute_symbol_content_hash
from core.utils.logging import get_logger
from parsing.models.extraction import AssignmentSpec, ScopeTarget
from parsing.services.scope import FileExtractionSession
from parsing.utils.ast_helpers import cap_signature, compute_visibility_hint, safe_unparse
from parsing.utils.naming import build_nested_qualified_name
from parsing.utils.ranges import (
    make_declaration_lines,
    make_name_selection_range,
    make_range,
)

logger = get_logger("parsing.services.assignments")

_ClassLookup = tuple[dict[tuple[str, int], SymbolNode], dict[str, SymbolNode]]


class AssignmentExtractor:
    """Extracts module constants, ``TypeAlias`` symbols, and class body attributes."""

    @staticmethod
    def _is_type_alias_annotation(annotation_str: str) -> bool:
        """Check whether an annotation string denotes ``typing.TypeAlias``.

        Args:
            annotation_str: Unparsed annotation expression text.

        Returns:
            ``True`` when ``annotation_str`` represents a ``TypeAlias`` declaration.
        """
        stripped = annotation_str.strip()
        return (
            stripped in ("TypeAlias", "typing.TypeAlias")
            or stripped.startswith("TypeAlias[")
            or stripped.startswith("typing.TypeAlias[")
        )

    @classmethod
    def _extract_target_names(cls, target: ast.AST) -> list[ast.Name]:
        """Extract non-wildcard ``ast.Name`` targets from an assignment target expression.

        Args:
            target: Assignment target AST node (``Name``, ``Tuple``, ``List``, etc.).

        Returns:
            Ordered list of simple ``ast.Name`` nodes excluding ``_``.
        """
        if isinstance(target, ast.Name):
            return [target] if target.id != "_" else []
        if isinstance(target, (ast.Tuple, ast.List)):
            names: list[ast.Name] = []
            for elt in target.elts:
                names.extend(cls._extract_target_names(elt))
            return names
        return []

    @staticmethod
    def _compute_semantic_hash(
        kind: SymbolKind,
        qualified_name: str,
        annotation: str | None,
        value_hint: str | None,
    ) -> str:
        """Compute a deterministic semantic hash for an assignment-derived symbol.

        Args:
            kind: ``SymbolKind`` (``constant``, ``attribute``, or ``type_alias``).
            qualified_name: Full symbol qualified name.
            annotation: Optional unparsed type annotation.
            value_hint: Optional capped RHS value representation.

        Returns:
            ``sha256:``-prefixed digest string.
        """
        hash_obj = hashlib.sha256()
        hash_obj.update(f"kind:{kind.value}".encode())
        hash_obj.update(f"qualified_name:{qualified_name}".encode())
        hash_obj.update(f"annotation:{annotation}".encode())
        hash_obj.update(f"value_hint:{value_hint}".encode())
        return f"{PREFIX_SHA256}{hash_obj.hexdigest()}"

    @staticmethod
    def _make_spec(
        lines: Sequence[str] | None,
        stmt: ast.AST,
        name_node: ast.Name,
        kind: SymbolKind,
        *,
        signature: str,
        hints: tuple[str | None, str | None] = (None, None),
    ) -> AssignmentSpec:
        """Construct an immutable ``AssignmentSpec`` with computed ranges and content hash."""
        annotation, value_hint = hints
        span = make_declaration_lines(stmt)
        return AssignmentSpec(
            kind=kind,
            name=name_node.id,
            signature=signature,
            start_line=span.start_line,
            end_line=span.end_line,
            decl_range=make_range(stmt, lines=lines),
            selection_range=make_name_selection_range(name_node, lines=lines),
            content_hash=compute_symbol_content_hash(
                stmt,
                lines,
                span.start_line,
                span.end_line,
            ),
            annotation=annotation,
            value_hint=value_hint,
        )

    def _build_node(
        self,
        session: FileExtractionSession,
        target: ScopeTarget,
        spec: AssignmentSpec,
    ) -> SymbolNode:
        """Convert an ``AssignmentSpec`` into a canonical ``SymbolNode``.

        Args:
            session: Active per-file extraction session.
            target: Enclosing module or class ``ScopeTarget``.
            spec: Extracted ``AssignmentSpec`` metadata.

        Returns:
            Immutable ``SymbolNode`` instance.
        """
        file_rec = session.file_record
        qualified_name = (
            f"{target.parent_qualified_name}.{spec.name}"
            if target.parent_qualified_name
            else spec.name
        )
        node_id = session.duplicate_tracker.get_symbol_id(
            session.repo_id,
            spec.kind,
            qualified_name,
        )

        payload: dict[str, Any] = {}
        if spec.annotation is not None:
            payload["annotation"] = spec.annotation
        if spec.value_hint is not None:
            payload["value_hint"] = spec.value_hint

        return SymbolNode(
            id=node_id,
            repo_id=session.repo_id,
            file_id=file_rec.id,
            language=Language.PYTHON,
            kind=spec.kind,
            name=spec.name,
            qualified_name=qualified_name,
            uri=file_rec.uri,
            decl_range=spec.decl_range,
            selection_range=spec.selection_range,
            start_line=spec.start_line,
            end_line=spec.end_line,
            signature=spec.signature,
            parent_id=target.parent_id,
            visibility_hint=compute_visibility_hint(spec.name, target.scope),
            doc_summary=None,
            content_hash=spec.content_hash,
            semantic_hash=self._compute_semantic_hash(
                spec.kind,
                qualified_name,
                spec.annotation,
                spec.value_hint,
            ),
            source="python-ast",
            confidence=1.0,
            scope=target.scope,
            lexical_parent_id=None,
            payload=payload,
            last_indexed_at=file_rec.last_indexed_at,
        )

    def _specs_for_type_alias(
        self,
        session: FileExtractionSession,
        stmt: ast.stmt,
    ) -> list[AssignmentSpec]:
        """Extract ``AssignmentSpec`` items from Python 3.12+ ``ast.TypeAlias`` statements."""
        if sys.version_info < (3, 12) or not isinstance(stmt, ast.TypeAlias):
            return []
        name_node = stmt.name
        if not isinstance(name_node, ast.Name) or name_node.id == "_":
            return []
        return [
            self._make_spec(
                session.lines,
                stmt,
                name_node,
                SymbolKind.TYPE_ALIAS,
                signature=cap_signature(safe_unparse(stmt)),
                hints=(None, cap_signature(safe_unparse(stmt.value))),
            )
        ]

    def _specs_for_ann_assign(
        self,
        session: FileExtractionSession,
        stmt: ast.AnnAssign,
        default_kind: SymbolKind,
    ) -> list[AssignmentSpec]:
        """Extract ``AssignmentSpec`` items from an ``ast.AnnAssign`` statement."""
        if not isinstance(stmt.target, ast.Name) or stmt.target.id == "_":
            return []
        annotation = safe_unparse(stmt.annotation)
        kind = (
            SymbolKind.TYPE_ALIAS
            if self._is_type_alias_annotation(annotation)
            else default_kind
        )
        value_hint = cap_signature(safe_unparse(stmt.value)) if stmt.value is not None else None
        return [
            self._make_spec(
                session.lines,
                stmt,
                stmt.target,
                kind,
                signature=cap_signature(safe_unparse(stmt)),
                hints=(annotation, value_hint),
            )
        ]

    def _specs_for_assign(
        self,
        session: FileExtractionSession,
        stmt: ast.Assign,
        default_kind: SymbolKind,
    ) -> list[AssignmentSpec]:
        """Extract ``AssignmentSpec`` items from an ``ast.Assign`` statement."""
        value_hint = cap_signature(safe_unparse(stmt.value))
        stmt_signature = cap_signature(safe_unparse(stmt))
        is_single_name = len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
        specs: list[AssignmentSpec] = []

        for assign_target in stmt.targets:
            for name_node in self._extract_target_names(assign_target):
                sig = (
                    stmt_signature
                    if is_single_name
                    else cap_signature(f"{name_node.id} = {value_hint}")
                )
                specs.append(
                    self._make_spec(
                        session.lines,
                        stmt,
                        name_node,
                        default_kind,
                        signature=sig,
                        hints=(None, value_hint),
                    )
                )
        return specs

    def extract_body(
        self,
        session: FileExtractionSession,
        target: ScopeTarget,
        body: Sequence[ast.stmt],
        default_kind: SymbolKind,
    ) -> list[SymbolNode]:
        """Extract assignment-based ``SymbolNode`` items directly inside ``body``.

        Args:
            session: Active per-file extraction session.
            target: Enclosing module or class ``ScopeTarget``.
            body: AST statement list of the module or class body.
            default_kind: Default ``SymbolKind`` (``CONSTANT`` or ``ATTRIBUTE``).

        Returns:
            Ordered list of extracted ``SymbolNode`` declarations.
        """
        specs: list[AssignmentSpec] = []
        for stmt in body:
            if isinstance(stmt, ast.AnnAssign):
                specs.extend(self._specs_for_ann_assign(session, stmt, default_kind))
            elif isinstance(stmt, ast.Assign):
                specs.extend(self._specs_for_assign(session, stmt, default_kind))
            else:
                specs.extend(self._specs_for_type_alias(session, stmt))
        return [self._build_node(session, target, spec) for spec in specs]

    @staticmethod
    def _build_class_lookup(class_nodes: Sequence[SymbolNode]) -> _ClassLookup:
        """Build exact ``(qualified_name, start_line)`` and fallback class lookups."""
        by_qname_line: dict[tuple[str, int], SymbolNode] = {}
        by_qname: dict[str, SymbolNode] = {}
        for cls_node in class_nodes:
            if cls_node.kind != SymbolKind.CLASS:
                continue
            by_qname.setdefault(cls_node.qualified_name, cls_node)
            if cls_node.start_line is not None:
                by_qname_line[(cls_node.qualified_name, cls_node.start_line)] = cls_node
        return by_qname_line, by_qname

    @staticmethod
    def _find_matching_class(
        stmt: ast.ClassDef,
        module_path: str,
        lexical_chain: Sequence[str],
        class_lookup: _ClassLookup,
    ) -> SymbolNode | None:
        """Find the extracted ``SymbolNode`` corresponding to ``stmt``."""
        by_qname_line, by_qname = class_lookup
        qname = build_nested_qualified_name(module_path, lexical_chain, stmt.name)
        span = make_declaration_lines(stmt)
        return by_qname_line.get((qname, span.start_line)) or by_qname.get(qname)

    def _walk_class_attributes(
        self,
        session: FileExtractionSession,
        stmts: Sequence[ast.stmt],
        lexical_chain: list[str],
        class_lookup: _ClassLookup,
    ) -> list[SymbolNode]:
        """Recursively collect class-level attributes across all classes."""
        extracted: list[SymbolNode] = []
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                cls_node = self._find_matching_class(
                    stmt,
                    session.module_path,
                    lexical_chain,
                    class_lookup,
                )
                if cls_node is not None:
                    cls_target = ScopeTarget(
                        parent_id=cls_node.id,
                        parent_qualified_name=cls_node.qualified_name,
                        scope=SymbolScope.CLASS,
                    )
                    extracted.extend(
                        self.extract_body(session, cls_target, stmt.body, SymbolKind.ATTRIBUTE)
                    )
                lexical_chain.append(stmt.name)
                extracted.extend(
                    self._walk_class_attributes(session, stmt.body, lexical_chain, class_lookup)
                )
                lexical_chain.pop()
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lexical_chain.append(stmt.name)
                extracted.extend(
                    self._walk_class_attributes(session, stmt.body, lexical_chain, class_lookup)
                )
                lexical_chain.pop()
        return extracted

    def extract_all(
        self,
        session: FileExtractionSession,
        module_node_id: str,
        tree: ast.Module,
        class_nodes: Sequence[SymbolNode] = (),
    ) -> list[SymbolNode]:
        """Extract all module constants/type aliases and class-level attributes in ``tree``.

        Args:
            session: Active per-file extraction session.
            module_node_id: Root module ``SymbolNode`` identifier.
            tree: Parsed ``ast.Module`` root node.
            class_nodes: Extracted ``SymbolKind.CLASS`` nodes across ``tree``.

        Returns:
            Ordered list of ``constant``, ``type_alias``, and ``attribute`` nodes.
        """
        module_target = ScopeTarget(
            parent_id=module_node_id,
            parent_qualified_name=session.module_path,
            scope=SymbolScope.MODULE,
        )
        extracted = self.extract_body(session, module_target, tree.body, SymbolKind.CONSTANT)
        if not class_nodes:
            return extracted

        class_lookup = self._build_class_lookup(class_nodes)
        extracted.extend(self._walk_class_attributes(session, tree.body, [], class_lookup))
        return extracted


__all__ = [
    "AssignmentExtractor",
]
