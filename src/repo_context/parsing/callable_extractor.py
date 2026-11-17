"""Callable node extraction from Python AST."""

import ast
import hashlib
import json
import typing
from collections.abc import Sequence

import repo_context.parsing.class_extractor
from repo_context.models.file import FileRecord
from repo_context.parsing.docstrings import get_doc_summary
from repo_context.parsing.hashing import compute_symbol_content_hash
from repo_context.parsing.naming import (
    DuplicateTracker,
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
from repo_context.parsing.scope_tracker import ScopeTracker

_NESTED_SCOPE_BOUNDARY_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)


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


def extract_callable_nodes(
    repo_id: str,
    file_record: FileRecord,
    parent_id: str,
    parent_qualified_name: str,
    nodes: "typing.Sequence[ast.AST]",
    scope_tracker: ScopeTracker,
    duplicate_tracker: DuplicateTracker,
    module_path: str,
    file_text: str = "",
    *,
    lines: Sequence[str] | None = None,
) -> list[dict]:
    """Extract callable nodes from function/method definitions with nested scope support.

    Args:
        repo_id: Repository ID.
        file_record: File record for the source module.
        parent_id: Enclosing structural parent node ID.
        parent_qualified_name: Parent qualified name fallback when tracker is empty.
        nodes: Sequence of AST nodes to process.
        scope_tracker: Scope tracker for lexical nesting.
        duplicate_tracker: Duplicate tracker for symbol ID disambiguation.
        module_path: Module dotted path.
        file_text: Full file text (used when ``lines`` is not provided).
        lines: Optional pre-split source lines.

    Returns:
        List of callable and nested declaration node dictionaries.
    """
    file_lines = lines if lines is not None else (file_text.splitlines() if file_text else None)
    extracted: list[dict] = []

    for node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        callable_name = node.name
        is_async = isinstance(node, ast.AsyncFunctionDef)
        current_scope = scope_tracker.get_current_scope()
        enclosing_id = scope_tracker.get_lexical_parent_id()

        if current_scope == "module":
            kind = "async_function" if is_async else "function"
            actual_parent_id = parent_id
            lexical_parent_id: str | None = None
        elif current_scope == "class":
            kind = "async_method" if is_async else "method"
            actual_parent_id = enclosing_id if enclosing_id is not None else parent_id
            lexical_parent_id = None
        else:
            kind = "local_async_function" if is_async else "local_function"
            actual_parent_id = enclosing_id if enclosing_id is not None else parent_id
            lexical_parent_id = enclosing_id

        if scope_tracker.is_empty():
            qualified_name = build_callable_qualified_name(parent_qualified_name, callable_name)
        else:
            qualified_name = build_nested_qualified_name(
                module_path, scope_tracker.get_lexical_chain(), callable_name
            )

        node_id = duplicate_tracker.get_symbol_id(repo_id, kind, qualified_name)

        decorators = [safe_unparse(decorator) for decorator in node.decorator_list]
        parameters = extract_parameters(node.args)
        return_annotation = safe_unparse(node.returns) if node.returns is not None else None
        is_generator = _is_generator(node)
        visibility_hint = compute_visibility_hint(callable_name, current_scope)
        start_line, end_line = make_declaration_lines(node)
        range_json = make_range(node, lines=file_lines)
        selection_range_json = make_name_selection_range(node, lines=file_lines)
        signature = _build_callable_signature(
            callable_name, parameters, return_annotation, is_async
        )
        content_hash = compute_symbol_content_hash(node, file_lines, start_line, end_line)
        semantic_hash = _compute_callable_semantic_hash(
            qualified_name, kind, parameters, return_annotation, decorators, is_async
        )

        extracted.append(
            {
                "id": node_id,
                "repo_id": repo_id,
                "file_id": file_record.id,
                "language": "python",
                "kind": kind,
                "name": callable_name,
                "qualified_name": qualified_name,
                "uri": file_record.uri,
                "range_json": range_json,
                "selection_range_json": selection_range_json,
                "start_line": start_line,
                "end_line": end_line,
                "signature": signature,
                "parent_id": actual_parent_id,
                "visibility_hint": visibility_hint,
                "doc_summary": get_doc_summary(node),
                "content_hash": content_hash,
                "semantic_hash": semantic_hash,
                "source": "python-ast",
                "confidence": 1.0,
                "scope": current_scope,
                "lexical_parent_id": lexical_parent_id,
                "payload_json": json.dumps(
                    {
                        "parameters": parameters,
                        "return_annotation": return_annotation,
                        "decorators": decorators,
                        "is_async": is_async,
                        "is_method": current_scope == "class",
                        "is_generator": is_generator,
                    },
                    sort_keys=True,
                ),
                "last_indexed_at": file_record.last_indexed_at,
            }
        )

        nested_nodes = _extract_nested_declarations(
            repo_id=repo_id,
            file_record=file_record,
            func_node_id=node_id,
            func_qualified_name=qualified_name,
            func_node=node,
            scope_tracker=scope_tracker,
            duplicate_tracker=duplicate_tracker,
            module_path=module_path,
            file_text=file_text,
            lines=file_lines,
        )
        extracted.extend(nested_nodes)

    return extracted


def _extract_nested_declarations(
    repo_id: str,
    file_record: FileRecord,
    func_node_id: str,
    func_qualified_name: str,
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope_tracker: ScopeTracker,
    duplicate_tracker: DuplicateTracker,
    module_path: str,
    file_text: str,
    lines: Sequence[str] | None,
) -> list[dict]:
    """Extract nested functions and local classes directly within a function body."""
    extract_class_nodes = repo_context.parsing.class_extractor.extract_class_nodes

    nested: list[dict] = []
    func_name = getattr(func_node, "name", "<unknown>")

    with scope_tracker.scope_context(func_node_id, func_name, "function"):
        for stmt in func_node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested.extend(
                    extract_callable_nodes(
                        repo_id=repo_id,
                        file_record=file_record,
                        parent_id=func_node_id,
                        parent_qualified_name=func_qualified_name,
                        nodes=[stmt],
                        scope_tracker=scope_tracker,
                        duplicate_tracker=duplicate_tracker,
                        module_path=module_path,
                        file_text=file_text,
                        lines=lines,
                    )
                )
            elif isinstance(stmt, ast.ClassDef):
                nested_class_nodes, _ = extract_class_nodes(
                    repo_id=repo_id,
                    file_record=file_record,
                    module_node_id=func_node_id,
                    module_path=module_path,
                    tree=ast.Module(body=[stmt], type_ignores=[]),
                    scope_tracker=scope_tracker,
                    duplicate_tracker=duplicate_tracker,
                    parent_id=func_node_id,
                    parent_qualified_name=func_qualified_name,
                    file_text=file_text,
                    lines=lines,
                )
                nested.extend(nested_class_nodes)

    return nested


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
    hash_obj.update(f"kind:{kind}".encode("utf-8"))
    hash_obj.update(f"qualified_name:{qualified_name}".encode("utf-8"))
    hash_obj.update(f"is_async:{is_async}".encode("utf-8"))

    param_data = [
        (p["name"], p["kind"], p.get("annotation"), p.get("default_value_hint"))
        for p in parameters
    ]
    hash_obj.update(f"parameters:{param_data}".encode("utf-8"))
    hash_obj.update(f"return:{return_annotation}".encode("utf-8"))
    hash_obj.update(f"decorators:{decorators}".encode("utf-8"))

    return f"sha256:{hash_obj.hexdigest()}"
