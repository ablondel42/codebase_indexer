"""Leaf ``SymbolKind.CLASS`` node extractor service."""

import ast
import hashlib
from collections.abc import Sequence

from core.models.enums import PREFIX_SHA256, Language, SymbolKind, SymbolScope
from core.models.node import SymbolNode
from core.utils.hashing import compute_symbol_content_hash
from core.utils.logging import get_logger
from parsing.models.extraction import DeclarationPlacement, ScopeTarget
from parsing.services.scope import FileExtractionSession
from parsing.utils.ast_helpers import (
    cap_signature,
    compute_visibility_hint,
    get_doc_summary,
    safe_unparse,
)
from parsing.utils.naming import build_class_qualified_name, build_nested_qualified_name
from parsing.utils.ranges import (
    make_declaration_lines,
    make_name_selection_range,
    make_range,
)

logger = get_logger("parsing.services.classes")


class ClassExtractor:
    """Leaf builder for single ``SymbolKind.CLASS`` ``SymbolNode`` declarations."""

    @staticmethod
    def _build_signature(class_name: str, base_names: Sequence[str]) -> str:
        """Build a compact single-line class signature capped at 240 characters.

        Args:
            class_name: Declared class identifier.
            base_names: Unparsed base class expressions in declaration order.

        Returns:
            Normalized capped signature string.
        """
        raw = (
            f"class {class_name}({', '.join(base_names)})"
            if base_names
            else f"class {class_name}"
        )
        return cap_signature(raw)

    @staticmethod
    def _compute_semantic_hash(
        qualified_name: str,
        base_names: list[str],
        decorators: list[str],
    ) -> str:
        """Compute a deterministic semantic hash for a class declaration.

        Args:
            qualified_name: Full class qualified name.
            base_names: Unparsed base class names.
            decorators: Unparsed decorator expressions.

        Returns:
            ``sha256:``-prefixed digest string.
        """
        hash_obj = hashlib.sha256()
        hash_obj.update(b"kind:class")
        hash_obj.update(f"qualified_name:{qualified_name}".encode())
        hash_obj.update(f"bases:{base_names}".encode())
        hash_obj.update(f"decorators:{decorators}".encode())
        return f"{PREFIX_SHA256}{hash_obj.hexdigest()}"

    def resolve_placement(
        self,
        session: FileExtractionSession,
        class_name: str,
        module_node_id: str | None = None,
        target: ScopeTarget | None = None,
    ) -> DeclarationPlacement:
        """Resolve structural/lexical parent IDs, scope, and qualified name for a class.

        Args:
            session: Active per-file extraction session.
            class_name: Simple declared class name.
            module_node_id: Module node ID fallback for top-level classes.
            target: Optional explicit ``ScopeTarget`` from enclosing walker frame.

        Returns:
            Resolved ``DeclarationPlacement`` for the ``ast.ClassDef``.
        """
        current_scope = session.scope_tracker.get_current_scope()
        enclosing_id = session.scope_tracker.get_lexical_parent_id()
        explicit_parent = target.parent_id if target is not None else None
        fallback_parent = (
            module_node_id if current_scope == SymbolScope.MODULE else enclosing_id
        )
        actual_parent_id = explicit_parent if explicit_parent is not None else fallback_parent
        lexical_parent_id = (
            enclosing_id
            if current_scope not in (SymbolScope.MODULE, SymbolScope.CLASS)
            else None
        )

        if not session.scope_tracker.is_empty():
            qualified_name = build_nested_qualified_name(
                session.module_path,
                session.scope_tracker.get_lexical_chain(),
                class_name,
            )
        elif target is not None and target.parent_qualified_name:
            qualified_name = f"{target.parent_qualified_name}.{class_name}"
        else:
            qualified_name = build_class_qualified_name(session.module_path, class_name)

        return DeclarationPlacement(
            kind=SymbolKind.CLASS,
            parent_id=actual_parent_id,
            lexical_parent_id=lexical_parent_id,
            qualified_name=qualified_name,
            scope=current_scope,
        )

    def extract(
        self,
        session: FileExtractionSession,
        node: ast.ClassDef,
        node_id: str,
        placement: DeclarationPlacement,
        method_ids: Sequence[str] = (),
    ) -> SymbolNode:
        """Build an immutable ``SymbolNode`` for a single ``ast.ClassDef`` without recursion.

        Args:
            session: Active per-file extraction session.
            node: ``ast.ClassDef`` declaration node.
            node_id: Pre-allocated symbol identifier for this class.
            placement: Resolved ``DeclarationPlacement`` for this class.
            method_ids: Direct method symbol IDs extracted by ``DeclarationWalker``.

        Returns:
            Constructed immutable ``SymbolNode`` of kind ``SymbolKind.CLASS``.
        """
        file_rec = session.file_record
        base_names = [safe_unparse(base) for base in node.bases]
        decorators = [safe_unparse(decorator) for decorator in node.decorator_list]
        span = make_declaration_lines(node)

        return SymbolNode(
            id=node_id,
            repo_id=session.repo_id,
            file_id=file_rec.id,
            language=Language.PYTHON,
            kind=SymbolKind.CLASS,
            name=node.name,
            qualified_name=placement.qualified_name,
            uri=file_rec.uri,
            decl_range=make_range(node, lines=session.lines),
            selection_range=make_name_selection_range(node, lines=session.lines),
            start_line=span.start_line,
            end_line=span.end_line,
            signature=self._build_signature(node.name, base_names),
            parent_id=placement.parent_id,
            visibility_hint=compute_visibility_hint(node.name, placement.scope),
            doc_summary=get_doc_summary(node),
            content_hash=compute_symbol_content_hash(
                node,
                session.lines,
                span.start_line,
                span.end_line,
            ),
            semantic_hash=self._compute_semantic_hash(
                placement.qualified_name,
                base_names,
                decorators,
            ),
            source="python-ast",
            confidence=1.0,
            scope=placement.scope,
            lexical_parent_id=placement.lexical_parent_id,
            payload={
                "base_names": base_names,
                "decorators": decorators,
                "method_ids": list(method_ids),
            },
            last_indexed_at=file_rec.last_indexed_at,
        )


__all__ = [
    "ClassExtractor",
]
