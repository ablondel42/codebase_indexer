"""Leaf callable ``SymbolNode`` and parameter extractor service."""

import ast
import hashlib
from collections.abc import Sequence
from types import MappingProxyType

from core.models.enums import PREFIX_SHA256, Language, SymbolKind, SymbolScope
from core.models.node import SymbolNode
from core.utils.hashing import compute_symbol_content_hash
from core.utils.logging import get_logger
from parsing.models.extraction import DeclarationPlacement, ParameterSpec, ScopeTarget
from parsing.services.scope import FileExtractionSession
from parsing.utils.ast_helpers import (
    cap_signature,
    compute_visibility_hint,
    get_doc_summary,
    is_generator_callable,
    safe_unparse,
)
from parsing.utils.naming import build_callable_qualified_name, build_nested_qualified_name
from parsing.utils.ranges import (
    make_declaration_lines,
    make_name_selection_range,
    make_range,
)

logger = get_logger("parsing.services.callables")

_CALLABLE_KIND_BY_SCOPE = MappingProxyType(
    {
        (SymbolScope.MODULE, False): SymbolKind.FUNCTION,
        (SymbolScope.MODULE, True): SymbolKind.ASYNC_FUNCTION,
        (SymbolScope.CLASS, False): SymbolKind.METHOD,
        (SymbolScope.CLASS, True): SymbolKind.ASYNC_METHOD,
    }
)


class CallableExtractor:
    """Leaf builder for function, method, and local-function ``SymbolNode`` declarations."""

    @staticmethod
    def _build_param_spec(
        arg: ast.arg,
        kind: str,
        default_node: ast.expr | None = None,
    ) -> ParameterSpec:
        """Build a ``ParameterSpec`` for one AST parameter node.

        Args:
            arg: ``ast.arg`` parameter node.
            kind: Parameter category (``positional_only``, ``positional_or_keyword``,
                ``var_positional``, ``keyword_only``, or ``var_keyword``).
            default_node: Optional AST expression providing the default value.

        Returns:
            Immutable ``ParameterSpec`` model.
        """
        annotation = safe_unparse(arg.annotation) if arg.annotation is not None else None
        default_hint = safe_unparse(default_node) if default_node is not None else None
        return ParameterSpec(
            name=arg.arg,
            kind=kind,
            annotation=annotation,
            default_value_hint=default_hint,
        )

    def extract_parameters(self, args: ast.arguments) -> list[ParameterSpec]:
        """Extract ordered ``ParameterSpec`` models from ``ast.arguments``.

        Args:
            args: Python AST callable arguments node.

        Returns:
            Ordered list of ``ParameterSpec`` models.
        """
        parameters: list[ParameterSpec] = []
        posonlyargs = list(getattr(args, "posonlyargs", []))
        positional = posonlyargs + list(args.args)
        defaults = list(args.defaults)
        first_default_idx = len(positional) - len(defaults)

        for idx, arg in enumerate(positional):
            kind = "positional_only" if idx < len(posonlyargs) else "positional_or_keyword"
            default_node = defaults[idx - first_default_idx] if idx >= first_default_idx else None
            parameters.append(self._build_param_spec(arg, kind, default_node))

        if args.vararg is not None:
            parameters.append(self._build_param_spec(args.vararg, "var_positional"))

        kw_defaults = list(getattr(args, "kw_defaults", []))
        for idx, arg in enumerate(args.kwonlyargs):
            default_node = kw_defaults[idx] if idx < len(kw_defaults) else None
            parameters.append(self._build_param_spec(arg, "keyword_only", default_node))

        if args.kwarg is not None:
            parameters.append(self._build_param_spec(args.kwarg, "var_keyword"))

        return parameters

    @staticmethod
    def _format_single_param(param: ParameterSpec) -> str:
        """Format a single ``ParameterSpec`` for a compact Python signature.

        Args:
            param: Normalized parameter metadata.

        Returns:
            Rendered parameter segment string.
        """
        if param.kind == "var_positional":
            prefix = f"*{param.name}"
        elif param.kind == "var_keyword":
            prefix = f"**{param.name}"
        else:
            prefix = param.name

        if param.annotation is not None:
            rendered = f"{prefix}: {param.annotation}"
            if param.default_value_hint is not None:
                return f"{rendered} = {param.default_value_hint}"
            return rendered
        if param.default_value_hint is not None:
            return f"{prefix}={param.default_value_hint}"
        return prefix

    def _build_signature(
        self,
        callable_name: str,
        parameters: Sequence[ParameterSpec],
        return_annotation: str | None,
        *,
        is_async: bool,
    ) -> str:
        """Build a compact single-line callable signature capped at 240 characters.

        Args:
            callable_name: Declared callable identifier.
            parameters: Ordered ``ParameterSpec`` sequence.
            return_annotation: Unparsed return type annotation, or ``None``.
            is_async: Whether the callable is ``async def``.

        Returns:
            Normalized capped signature string.
        """
        by_kind: dict[str, list[str]] = {
            "positional_only": [],
            "positional_or_keyword": [],
            "var_positional": [],
            "keyword_only": [],
            "var_keyword": [],
        }
        for param in parameters:
            by_kind[param.kind].append(self._format_single_param(param))

        parts: list[str] = []
        if by_kind["positional_only"]:
            parts.extend(by_kind["positional_only"])
            parts.append("/")
        parts.extend(by_kind["positional_or_keyword"])
        if by_kind["var_positional"]:
            parts.extend(by_kind["var_positional"])
        elif by_kind["keyword_only"]:
            parts.append("*")
        parts.extend(by_kind["keyword_only"])
        parts.extend(by_kind["var_keyword"])

        prefix = "async def " if is_async else "def "
        ret_part = f" -> {return_annotation}" if return_annotation is not None else ""
        return cap_signature(f"{prefix}{callable_name}({', '.join(parts)}){ret_part}")

    @staticmethod
    def _compute_semantic_hash(
        placement: DeclarationPlacement,
        parameters: Sequence[ParameterSpec],
        return_annotation: str | None,
        decorators: list[str],
        *,
        is_async: bool,
    ) -> str:
        """Compute a semantic hash preserving parameter order, annotations, and defaults.

        Args:
            placement: Resolved ``DeclarationPlacement`` of the callable.
            parameters: Ordered ``ParameterSpec`` sequence.
            return_annotation: Unparsed return annotation or ``None``.
            decorators: Unparsed decorator expressions.
            is_async: Whether the callable is ``async def``.

        Returns:
            ``sha256:``-prefixed digest string.
        """
        hash_obj = hashlib.sha256()
        hash_obj.update(f"kind:{placement.kind.value}".encode())
        hash_obj.update(f"qualified_name:{placement.qualified_name}".encode())
        hash_obj.update(f"is_async:{is_async}".encode())
        param_data = [
            (p.name, p.kind, p.annotation, p.default_value_hint)
            for p in parameters
        ]
        hash_obj.update(f"parameters:{param_data}".encode())
        hash_obj.update(f"return:{return_annotation}".encode())
        hash_obj.update(f"decorators:{decorators}".encode())
        return f"{PREFIX_SHA256}{hash_obj.hexdigest()}"

    def resolve_placement(
        self,
        session: FileExtractionSession,
        target: ScopeTarget,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> DeclarationPlacement:
        """Resolve kind, structural/lexical parent IDs, and qualified name for a callable.

        Args:
            session: Active per-file extraction session.
            target: Enclosing ``ScopeTarget`` metadata.
            node: ``ast.FunctionDef`` or ``ast.AsyncFunctionDef`` node.

        Returns:
            Resolved ``DeclarationPlacement`` for the callable.
        """
        is_async = isinstance(node, ast.AsyncFunctionDef)
        current_scope = session.scope_tracker.get_current_scope()
        enclosing_id = session.scope_tracker.get_lexical_parent_id()

        default_local = (
            SymbolKind.LOCAL_ASYNC_FUNCTION if is_async else SymbolKind.LOCAL_FUNCTION
        )
        kind = _CALLABLE_KIND_BY_SCOPE.get((current_scope, is_async), default_local)
        actual_parent_id = (
            target.parent_id
            if current_scope == SymbolScope.MODULE or enclosing_id is None
            else enclosing_id
        )
        lexical_parent_id = (
            enclosing_id
            if current_scope not in (SymbolScope.MODULE, SymbolScope.CLASS)
            else None
        )

        if session.scope_tracker.is_empty():
            qualified_name = build_callable_qualified_name(
                target.parent_qualified_name,
                node.name,
            )
        else:
            qualified_name = build_nested_qualified_name(
                session.module_path,
                session.scope_tracker.get_lexical_chain(),
                node.name,
            )

        return DeclarationPlacement(
            kind=kind,
            parent_id=actual_parent_id,
            lexical_parent_id=lexical_parent_id,
            qualified_name=qualified_name,
            scope=current_scope,
        )

    def extract(
        self,
        session: FileExtractionSession,
        target: ScopeTarget,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> SymbolNode:
        """Build an immutable ``SymbolNode`` for one callable AST node without recursion.

        Args:
            session: Active per-file extraction session.
            target: Enclosing ``ScopeTarget`` metadata.
            node: ``ast.FunctionDef`` or ``ast.AsyncFunctionDef`` node.

        Returns:
            Constructed immutable ``SymbolNode`` for the callable.
        """
        file_rec = session.file_record
        placement = self.resolve_placement(session, target, node)
        node_id = session.duplicate_tracker.get_symbol_id(
            session.repo_id,
            placement.kind,
            placement.qualified_name,
        )
        span = make_declaration_lines(node)
        is_async = isinstance(node, ast.AsyncFunctionDef)
        decorators = [safe_unparse(dec) for dec in node.decorator_list]
        parameters = self.extract_parameters(node.args)
        return_annotation = safe_unparse(node.returns) if node.returns is not None else None
        signature = self._build_signature(
            node.name,
            parameters,
            return_annotation,
            is_async=is_async,
        )
        semantic_hash = self._compute_semantic_hash(
            placement,
            parameters,
            return_annotation,
            decorators,
            is_async=is_async,
        )

        return SymbolNode(
            id=node_id,
            repo_id=session.repo_id,
            file_id=file_rec.id,
            language=Language.PYTHON,
            kind=placement.kind,
            name=node.name,
            qualified_name=placement.qualified_name,
            uri=file_rec.uri,
            decl_range=make_range(node, lines=session.lines),
            selection_range=make_name_selection_range(node, lines=session.lines),
            start_line=span.start_line,
            end_line=span.end_line,
            signature=signature,
            parent_id=placement.parent_id,
            visibility_hint=compute_visibility_hint(node.name, placement.scope),
            doc_summary=get_doc_summary(node),
            content_hash=compute_symbol_content_hash(
                node,
                session.lines,
                span.start_line,
                span.end_line,
            ),
            semantic_hash=semantic_hash,
            source="python-ast",
            confidence=1.0,
            scope=placement.scope,
            lexical_parent_id=placement.lexical_parent_id,
            payload={
                "parameters": [param.to_payload() for param in parameters],
                "return_annotation": return_annotation,
                "decorators": decorators,
                "is_async": is_async,
                "is_method": placement.scope == SymbolScope.CLASS,
                "is_generator": is_generator_callable(node),
            },
            last_indexed_at=file_rec.last_indexed_at,
        )


__all__ = [
    "CallableExtractor",
]
