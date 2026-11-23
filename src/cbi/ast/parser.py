"""Extract symbol definitions and their kinds from Python source using stdlib ast."""

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

SymbolKind: TypeAlias = Literal[
    "class",
    "function",
    "async_function",
    "method",
    "async_method",
    "property",
    "field",
    "constant",
    "variable",
    "type_alias",
]

_ScopeContext: TypeAlias = Literal["module", "class", "function"]


@dataclass(frozen=True, slots=True)
class SymbolNode:
    name: str
    qualname: str
    kind: SymbolKind
    location: str
    start_line: int
    end_line: int
    ast_hash: str | None = None
    decorators: tuple[str, ...] = ()


def _is_property(decorators: list[ast.expr]) -> bool:
    for dec in decorators:
        if isinstance(dec, ast.Name) and dec.id in {"property", "cached_property"}:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr in {"property", "cached_property"}:
            return True
    return False


def _is_type_alias(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "TypeAlias"
    if isinstance(annotation, ast.Attribute):
        return annotation.attr == "TypeAlias"
    return False


def _target_nodes(target: ast.expr) -> list[ast.Name]:
    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, ast.Tuple | ast.List):
        return [name_node for elt in target.elts for name_node in _target_nodes(elt)]
    return []


def _is_trivial_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    )


def _body_hash(body: list[ast.stmt]) -> str | None:
    stmts = body
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]

    if not stmts or all(_is_trivial_stmt(s) for s in stmts):
        return None

    canonical = "\n".join(ast.dump(s, include_attributes=False) for s in stmts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _make_symbol(
    name: str,
    kind: SymbolKind,
    stmt: ast.stmt,
    *,
    filepath: str,
    scope: tuple[str, ...],
    ast_hash: str | None = None,
    decorators: list[ast.expr] | None = None,
    line: int | None = None,
    col_offset: int | None = None,
) -> SymbolNode:
    loc_line = line if line is not None else stmt.lineno
    loc_col = (col_offset if col_offset is not None else stmt.col_offset) + 1
    first_line = decorators[0].lineno if decorators else stmt.lineno
    dec_names = tuple(ast.unparse(d) for d in decorators) if decorators else ()
    return SymbolNode(
        name=name,
        qualname=".".join([*scope, name]),
        kind=kind,
        location=f"{filepath}:{loc_line}:{loc_col}",
        start_line=first_line,
        end_line=stmt.end_lineno or stmt.lineno,
        ast_hash=ast_hash,
        decorators=dec_names,
    )


def _walk_callable(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_async: bool,
    filepath: str,
    scope: tuple[str, ...],
    context: _ScopeContext,
    out: list[SymbolNode],
) -> None:
    in_class = context == "class"
    if in_class and _is_property(stmt.decorator_list):
        kind: SymbolKind = "property"
    elif in_class:
        kind = "async_method" if is_async else "method"
    else:
        kind = "async_function" if is_async else "function"

    out.append(
        _make_symbol(
            stmt.name,
            kind,
            stmt,
            filepath=filepath,
            scope=scope,
            ast_hash=_body_hash(stmt.body),
            decorators=stmt.decorator_list,
        )
    )
    _walk_body(stmt.body, filepath, (*scope, stmt.name), "function", out)


def _walk_stmt(
    stmt: ast.stmt,
    filepath: str,
    scope: tuple[str, ...],
    context: _ScopeContext,
    out: list[SymbolNode],
) -> None:
    match stmt:
        case ast.ClassDef():
            out.append(
                _make_symbol(
                    stmt.name,
                    "class",
                    stmt,
                    filepath=filepath,
                    scope=scope,
                    ast_hash=_body_hash(stmt.body),
                    decorators=stmt.decorator_list,
                )
            )
            _walk_body(stmt.body, filepath, (*scope, stmt.name), "class", out)

        case ast.FunctionDef():
            _walk_callable(
                stmt, is_async=False, filepath=filepath, scope=scope, context=context, out=out
            )

        case ast.AsyncFunctionDef():
            _walk_callable(
                stmt, is_async=True, filepath=filepath, scope=scope, context=context, out=out
            )

        case ast.TypeAlias() if context != "function":
            out.append(
                _make_symbol(
                    stmt.name.id,
                    "type_alias",
                    stmt,
                    filepath=filepath,
                    scope=scope,
                    line=stmt.name.lineno,
                    col_offset=stmt.name.col_offset,
                )
            )

        case ast.AnnAssign(target=ast.Name(id=name)) if context != "function":
            if _is_type_alias(stmt.annotation):
                kind: SymbolKind = "type_alias"
            elif context == "class":
                kind = "field"
            else:
                kind = "constant" if name.isupper() else "variable"
            out.append(_make_symbol(name, kind, stmt, filepath=filepath, scope=scope))

        case ast.Assign() if context != "function":
            for target in stmt.targets:
                for name_node in _target_nodes(target):
                    name = name_node.id
                    assign_kind: SymbolKind = (
                        "field"
                        if context == "class"
                        else ("constant" if name.isupper() else "variable")
                    )
                    out.append(
                        _make_symbol(
                            name,
                            assign_kind,
                            stmt,
                            filepath=filepath,
                            scope=scope,
                            line=name_node.lineno,
                            col_offset=name_node.col_offset,
                        )
                    )

        case _:
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    _walk_stmt(child, filepath, scope, context, out)
                elif isinstance(child, ast.ExceptHandler | ast.match_case):
                    _walk_body(child.body, filepath, scope, context, out)


def _walk_body(
    stmts: list[ast.stmt],
    filepath: str,
    scope: tuple[str, ...],
    context: _ScopeContext,
    out: list[SymbolNode],
) -> None:
    for stmt in stmts:
        _walk_stmt(stmt, filepath, scope, context, out)


def extract_symbols(
    source: str, *, filename: str | Path = "<memory>", module: str = ""
) -> list[SymbolNode]:
    """Parse Python source and return all module/class/callable symbol definitions."""
    raw_name = str(filename)
    full_path = raw_name if raw_name == "<memory>" else str(Path(filename).resolve())
    tree = ast.parse(source, filename=full_path)
    initial_scope = (module,) if module else ()
    symbols: list[SymbolNode] = []
    _walk_body(tree.body, full_path, initial_scope, "module", symbols)
    return symbols


if __name__ == "__main__":
    mock_path = Path(__file__).with_name("mock.py")
    extracted = extract_symbols(
        mock_path.read_text(encoding="utf-8"),
        filename=mock_path,
        module="cbi.ast.mock",
    )
    for sym in extracted:
        digest = sym.ast_hash or "-"
        print(f"{sym.kind:<15} {sym.name:<19} {digest:<18} {sym.qualname:<38} {sym.location}")
