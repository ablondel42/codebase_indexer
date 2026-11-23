"""Extract symbol definitions and their kinds from Python source using stdlib ast."""

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

SymbolKind: TypeAlias = Literal[
    "module",
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
    "external",
]

_ScopeContext: TypeAlias = Literal["module", "class", "init", "function"]


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


@dataclass(slots=True)
class _WalkState:
    filepath: str
    root_pkg: str = ""
    symbols: list[SymbolNode] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)

    def add(self, node: SymbolNode) -> None:
        if node.qualname not in self.seen:
            self.seen.add(node.qualname)
            self.symbols.append(node)


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


def _self_attr_nodes(target: ast.expr) -> list[ast.Attribute]:
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ):
        return [target]
    if isinstance(target, ast.Tuple | ast.List):
        return [attr for elt in target.elts for attr in _self_attr_nodes(elt)]
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
    scope: tuple[str, ...],
    context: _ScopeContext,
    state: _WalkState,
) -> None:
    in_class = context == "class"
    if in_class and _is_property(stmt.decorator_list):
        kind: SymbolKind = "property"
    elif in_class:
        kind = "async_method" if is_async else "method"
    else:
        kind = "async_function" if is_async else "function"

    state.add(
        _make_symbol(
            stmt.name,
            kind,
            stmt,
            filepath=state.filepath,
            scope=scope,
            ast_hash=_body_hash(stmt.body),
            decorators=stmt.decorator_list,
        )
    )
    body_ctx: _ScopeContext = (
        "init" if in_class and stmt.name in {"__init__", "__post_init__"} else "function"
    )
    _walk_body(stmt.body, (*scope, stmt.name), body_ctx, state)


def _walk_import(stmt: ast.Import | ast.ImportFrom, state: _WalkState) -> None:
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            top_pkg = alias.name.split(".", 1)[0]
            if state.root_pkg and top_pkg == state.root_pkg:
                continue
            short_name = alias.name.rsplit(".", 1)[-1]
            state.add(
                SymbolNode(
                    name=short_name,
                    qualname=alias.name,
                    kind="external",
                    location=f"{state.filepath}:{alias.lineno}:{alias.col_offset + 1}",
                    start_line=stmt.lineno,
                    end_line=stmt.end_lineno or stmt.lineno,
                )
            )
        return

    if stmt.level > 0 or not stmt.module:
        return
    top_pkg = stmt.module.split(".", 1)[0]
    if state.root_pkg and top_pkg == state.root_pkg:
        return
    for alias in stmt.names:
        if alias.name == "*":
            continue
        state.add(
            SymbolNode(
                name=alias.name,
                qualname=f"{stmt.module}.{alias.name}",
                kind="external",
                location=f"{state.filepath}:{alias.lineno}:{alias.col_offset + 1}",
                start_line=stmt.lineno,
                end_line=stmt.end_lineno or stmt.lineno,
            )
        )


def _walk_stmt(
    stmt: ast.stmt,
    scope: tuple[str, ...],
    context: _ScopeContext,
    state: _WalkState,
) -> None:
    match stmt:
        case ast.ClassDef():
            state.add(
                _make_symbol(
                    stmt.name,
                    "class",
                    stmt,
                    filepath=state.filepath,
                    scope=scope,
                    ast_hash=_body_hash(stmt.body),
                    decorators=stmt.decorator_list,
                )
            )
            _walk_body(stmt.body, (*scope, stmt.name), "class", state)

        case ast.FunctionDef():
            _walk_callable(stmt, is_async=False, scope=scope, context=context, state=state)

        case ast.AsyncFunctionDef():
            _walk_callable(stmt, is_async=True, scope=scope, context=context, state=state)

        case ast.TypeAlias() if context in {"module", "class"}:
            state.add(
                _make_symbol(
                    stmt.name.id,
                    "type_alias",
                    stmt,
                    filepath=state.filepath,
                    scope=scope,
                    line=stmt.name.lineno,
                    col_offset=stmt.name.col_offset,
                )
            )

        case ast.AnnAssign(target=ast.Name(id=name)) if context in {"module", "class"}:
            if _is_type_alias(stmt.annotation):
                kind: SymbolKind = "type_alias"
            elif context == "class":
                kind = "field"
            else:
                kind = "constant" if name.isupper() else "variable"
            state.add(_make_symbol(name, kind, stmt, filepath=state.filepath, scope=scope))

        case ast.Assign() if context in {"module", "class"}:
            for target in stmt.targets:
                for name_node in _target_nodes(target):
                    name = name_node.id
                    assign_kind: SymbolKind = (
                        "field"
                        if context == "class"
                        else ("constant" if name.isupper() else "variable")
                    )
                    state.add(
                        _make_symbol(
                            name,
                            assign_kind,
                            stmt,
                            filepath=state.filepath,
                            scope=scope,
                            line=name_node.lineno,
                            col_offset=name_node.col_offset,
                        )
                    )

        case ast.AnnAssign(
            target=ast.Attribute(value=ast.Name(id="self"), attr=attr_name)
        ) if context == "init":
            state.add(
                _make_symbol(
                    attr_name,
                    "field",
                    stmt,
                    filepath=state.filepath,
                    scope=scope[:-1],
                    line=stmt.target.lineno,
                    col_offset=stmt.target.col_offset,
                )
            )

        case ast.Assign() if context == "init":
            for target in stmt.targets:
                for attr_node in _self_attr_nodes(target):
                    state.add(
                        _make_symbol(
                            attr_node.attr,
                            "field",
                            stmt,
                            filepath=state.filepath,
                            scope=scope[:-1],
                            line=attr_node.lineno,
                            col_offset=attr_node.col_offset,
                        )
                    )

        case ast.Import() | ast.ImportFrom() if context == "module":
            _walk_import(stmt, state)

        case ast.If() if context == "module" and ast.unparse(stmt.test) == "__name__ == '__main__'":
            _walk_body(stmt.orelse, scope, context, state)

        case _:
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    _walk_stmt(child, scope, context, state)
                elif isinstance(child, ast.ExceptHandler | ast.match_case):
                    _walk_body(child.body, scope, context, state)


def _walk_body(
    stmts: list[ast.stmt],
    scope: tuple[str, ...],
    context: _ScopeContext,
    state: _WalkState,
) -> None:
    for stmt in stmts:
        _walk_stmt(stmt, scope, context, state)


def extract_symbols(
    source: str, *, filename: str | Path = "<memory>", module: str = ""
) -> list[SymbolNode]:
    """Parse Python source and return module, class, field, and callable symbol nodes."""
    raw_name = str(filename)
    full_path = raw_name if raw_name == "<memory>" else str(Path(filename).resolve())
    tree = ast.parse(source, filename=full_path)

    if module:
        mod_name = module.rsplit(".", 1)[-1]
        mod_qualname = module
        initial_scope: tuple[str, ...] = (module,)
    elif raw_name == "<memory>":
        mod_name = "<module>"
        mod_qualname = "<module>"
        initial_scope = ()
    else:
        mod_name = Path(full_path).stem
        mod_qualname = mod_name
        initial_scope = (mod_name,)

    total_lines = max(len(source.splitlines()), 1)
    root_pkg = module.split(".", 1)[0] if module else ""
    state = _WalkState(filepath=full_path, root_pkg=root_pkg)
    state.add(
        SymbolNode(
            name=mod_name,
            qualname=mod_qualname,
            kind="module",
            location=f"{full_path}:1:1",
            start_line=1,
            end_line=total_lines,
        )
    )
    _walk_body(tree.body, initial_scope, "module", state)
    return state.symbols


if __name__ == "__main__":
    mock_path = Path(__file__).with_name("mock.py")
    extracted = extract_symbols(
        mock_path.read_text(encoding="utf-8"),
        filename=mock_path,
        module="cbi.ast.mock",
    )
    for sym in extracted:
        digest = sym.ast_hash or "-"
        print(f"{sym.kind:<15} {digest:<18} {sym.qualname:<38}")

