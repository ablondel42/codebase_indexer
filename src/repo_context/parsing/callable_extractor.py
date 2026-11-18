"""Callable node extraction from Python AST."""

import ast
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

import repo_context.parsing.class_extractor
from repo_context.parsing.docstrings import get_doc_summary
from repo_context.parsing.extraction_context import ExtractionContext, ScopeTarget
from repo_context.parsing.hashing import compute_symbol_content_hash
from repo_context.parsing.naming import (
    build_callable_qualified_name,
    build_nested_qualified_name,
    cap_signature,
    compute_visibility_hint,
    safe_unparse,
)
from repo_context.parsing.ranges import (
    make_declaration_lines,
    make_name_selection_range,
    make_range,
)

_NESTED_SCOPE_BOUNDARY_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)

_CALLABLE_KIND_BY_SCOPE: dict[tuple[str, bool], str] = {
    ("module", False): "function",
    ("module", True): "async_function",
    ("class", False): "method",
    ("class", True): "async_method",
}


@dataclass(frozen=True)
class _CallablePlacement:
    """Resolved structural and lexical placement for a callable node."""

    kind: str
    parent_id: str
    lexical_parent_id: str | None
    qualified_name: str
    scope: str


def _format_single_param(param: dict) -> str:
    """Format one parameter dict for a compact Python signature."""
    kind = param["kind"]
    name = param["name"]
    annotation = param.get("annotation")
    default = param.get("default_value_hint")

    if kind == "var_positional":
        prefix = f"*{name}"
    elif kind == "var_keyword":
        prefix = f"**{name}"
    else:
        prefix = name

    if annotation is not None:
        rendered = f"{prefix}: {annotation}"
        if default is not None:
            rendered = f"{rendered} = {default}"
    elif default is not None:
        rendered = f"{prefix}={default}"
    else:
        rendered = prefix

    return rendered


def _build_callable_signature(
    callable_name: str,
    parameters: list[dict],
    return_annotation: str | None,
    is_async: bool,
) -> str:
    """Build a compact single-line callable signature capped at 240 chars."""
    posonly_parts = [_format_single_param(p) for p in parameters if p["kind"] == "positional_only"]
    pos_kw_parts = [
        _format_single_param(p) for p in parameters if p["kind"] == "positional_or_keyword"
    ]
    vararg_parts = [_format_single_param(p) for p in parameters if p["kind"] == "var_positional"]
    kwonly_parts = [_format_single_param(p) for p in parameters if p["kind"] == "keyword_only"]
    kwarg_parts = [_format_single_param(p) for p in parameters if p["kind"] == "var_keyword"]

    parts: list[str] = []
    if posonly_parts:
        parts.extend(posonly_parts)
        parts.append("/")
    parts.extend(pos_kw_parts)
    if vararg_parts:
        parts.extend(vararg_parts)
    elif kwonly_parts:
        parts.append("*")
    parts.extend(kwonly_parts)
    parts.extend(kwarg_parts)

    prefix = "async def " if is_async else "def "
    ret_part = f" -> {return_annotation}" if return_annotation is not None else ""
    return cap_signature(f"{prefix}{callable_name}({', '.join(parts)}){ret_part}")


def _resolve_callable_placement(
    ctx: ExtractionContext,
    target: ScopeTarget,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> _CallablePlacement:
    """Resolve kind, parent IDs, and qualified name for a callable AST node."""
    is_async = isinstance(node, ast.AsyncFunctionDef)
    current_scope = ctx.scope_tracker.get_current_scope()
    enclosing_id = ctx.scope_tracker.get_lexical_parent_id()

    kind = _CALLABLE_KIND_BY_SCOPE.get(
        (current_scope, is_async),
        "local_async_function" if is_async else "local_function",
    )
    actual_parent_id = (
        target.parent_id
        if current_scope == "module" or enclosing_id is None
        else enclosing_id
    )
    lexical_parent_id = enclosing_id if current_scope not in ("module", "class") else None

    if ctx.scope_tracker.is_empty():
        qualified_name = build_callable_qualified_name(target.parent_qualified_name, node.name)
    else:
        qualified_name = build_nested_qualified_name(
            ctx.module_path, ctx.scope_tracker.get_lexical_chain(), node.name
        )

    return _CallablePlacement(
        kind=kind,
        parent_id=actual_parent_id,
        lexical_parent_id=lexical_parent_id,
        qualified_name=qualified_name,
        scope=current_scope,
    )


def _build_callable_metadata(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified_name: str,
    kind: str,
    current_scope: str,
) -> tuple[str, str, str]:
    """Compute ``(signature, semantic_hash, payload_json)`` for a callable node."""
    is_async = isinstance(node, ast.AsyncFunctionDef)
    decorators = [safe_unparse(decorator) for decorator in node.decorator_list]
    parameters = extract_parameters(node.args)
    return_annotation = safe_unparse(node.returns) if node.returns is not None else None
    signature = _build_callable_signature(node.name, parameters, return_annotation, is_async)
    semantic_hash = _compute_callable_semantic_hash(
        qualified_name, kind, parameters, return_annotation, decorators, is_async
    )
    payload_json = json.dumps(
        {
            "parameters": parameters,
            "return_annotation": return_annotation,
            "decorators": decorators,
            "is_async": is_async,
            "is_method": current_scope == "class",
            "is_generator": _is_generator(node),
        },
        sort_keys=True,
    )
    return signature, semantic_hash, payload_json


def _build_callable_node(
    ctx: ExtractionContext,
    target: ScopeTarget,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict:
    """Build a single callable symbol dictionary."""
    placement = _resolve_callable_placement(ctx, target, node)
    node_id = ctx.duplicate_tracker.get_symbol_id(
        ctx.repo_id, placement.kind, placement.qualified_name
    )
    start_line, end_line = make_declaration_lines(node)
    signature, semantic_hash, payload_json = _build_callable_metadata(
        node, placement.qualified_name, placement.kind, placement.scope
    )

    return {
        "id": node_id,
        "repo_id": ctx.repo_id,
        "file_id": ctx.file_record.id,
        "language": "python",
        "kind": placement.kind,
        "name": node.name,
        "qualified_name": placement.qualified_name,
        "uri": ctx.file_record.uri,
        "range_json": make_range(node, lines=ctx.lines),
        "selection_range_json": make_name_selection_range(node, lines=ctx.lines),
        "start_line": start_line,
        "end_line": end_line,
        "signature": signature,
        "parent_id": placement.parent_id,
        "visibility_hint": compute_visibility_hint(node.name, placement.scope),
        "doc_summary": get_doc_summary(node),
        "content_hash": compute_symbol_content_hash(node, ctx.lines, start_line, end_line),
        "semantic_hash": semantic_hash,
        "source": "python-ast",
        "confidence": 1.0,
        "scope": placement.scope,
        "lexical_parent_id": placement.lexical_parent_id,
        "payload_json": payload_json,
        "last_indexed_at": ctx.file_record.last_indexed_at,
    }


def _extract_nested_declarations(
    ctx: ExtractionContext,
    func_target: ScopeTarget,
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[dict]:
    """Extract nested functions and local classes directly within a function body."""
    extract_class_nodes = repo_context.parsing.class_extractor.extract_class_nodes
    nested: list[dict] = []

    with ctx.scope_tracker.scope_context(func_target.parent_id, func_node.name, "function"):
        for stmt in func_node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested.extend(extract_callable_nodes(ctx, func_target, [stmt]))
            elif isinstance(stmt, ast.ClassDef):
                nested_class_nodes, _ = extract_class_nodes(
                    ctx,
                    ast.Module(body=[stmt], type_ignores=[]),
                    target=func_target,
                )
                nested.extend(nested_class_nodes)

    return nested


def extract_callable_nodes(
    ctx: ExtractionContext,
    target: ScopeTarget,
    nodes: Sequence[ast.AST],
) -> list[dict]:
    """Extract callable nodes from function/method definitions with nested scope support.

    Args:
        ctx: Shared per-file extraction context.
        target: Enclosing structural target metadata.
        nodes: Sequence of AST nodes to process.

    Returns:
        List of callable and nested declaration node dictionaries.
    """
    extracted: list[dict] = []
    for node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        callable_dict = _build_callable_node(ctx, target, node)
        extracted.append(callable_dict)
        func_target = ScopeTarget(
            parent_id=callable_dict["id"],
            parent_qualified_name=callable_dict["qualified_name"],
            scope="function",
        )
        extracted.extend(_extract_nested_declarations(ctx, func_target, node))
    return extracted


def _build_param_dict(
    arg: ast.arg,
    kind: str,
    default_node: ast.expr | None = None,
) -> dict:
    """Build one parameter metadata dictionary omitting null default clutter."""
    annotation = safe_unparse(arg.annotation) if arg.annotation is not None else None
    param: dict = {
        "name": arg.arg,
        "kind": kind,
        "annotation": annotation,
    }
    if default_node is not None:
        param["default_value_hint"] = safe_unparse(default_node)
    return param


def extract_parameters(args: ast.arguments) -> list[dict]:
    """Extract ordered parameter metadata including annotations and defaults.

    Args:
        args: AST arguments node.

    Returns:
        List of parameter dictionaries.
    """
    parameters: list[dict] = []

    posonlyargs = list(getattr(args, "posonlyargs", []))
    regular_args = list(args.args)
    positional = posonlyargs + regular_args
    defaults = list(args.defaults)
    first_default_idx = len(positional) - len(defaults)

    for idx, arg in enumerate(positional):
        kind = "positional_only" if idx < len(posonlyargs) else "positional_or_keyword"
        default_node = defaults[idx - first_default_idx] if idx >= first_default_idx else None
        parameters.append(_build_param_dict(arg, kind, default_node))

    if args.vararg is not None:
        parameters.append(_build_param_dict(args.vararg, "var_positional"))

    kw_defaults = list(getattr(args, "kw_defaults", []))
    for idx, arg in enumerate(args.kwonlyargs):
        default_node = kw_defaults[idx] if idx < len(kw_defaults) else None
        parameters.append(_build_param_dict(arg, "keyword_only", default_node))

    if args.kwarg is not None:
        parameters.append(_build_param_dict(args.kwarg, "var_keyword"))

    return parameters


def _is_generator(node: ast.AST) -> bool:
    """Return True if the function body itself yields (without descending into nested scopes)."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(child, _NESTED_SCOPE_BOUNDARY_NODES):
            continue
        stack.extend(ast.iter_child_nodes(child))
    return False


def _compute_callable_semantic_hash(
    qualified_name: str,
    kind: str,
    parameters: list[dict],
    return_annotation: str | None,
    decorators: list[str],
    is_async: bool,
) -> str:
    """Compute semantic hash preserving parameter order, annotations, and defaults."""
    hash_obj = hashlib.sha256()
    hash_obj.update(f"kind:{kind}".encode())
    hash_obj.update(f"qualified_name:{qualified_name}".encode())
    hash_obj.update(f"is_async:{is_async}".encode())

    param_data = [
        (p["name"], p["kind"], p.get("annotation"), p.get("default_value_hint"))
        for p in parameters
    ]
    hash_obj.update(f"parameters:{param_data}".encode())
    hash_obj.update(f"return:{return_annotation}".encode())
    hash_obj.update(f"decorators:{decorators}".encode())

    return f"sha256:{hash_obj.hexdigest()}"
