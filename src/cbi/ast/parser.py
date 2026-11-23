"""Extract symbol definitions and their kinds from Python source using stdlib ast."""

import ast
import hashlib
from collections.abc import Collection
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
_PROPERTY_DECORATORS: frozenset[str] = frozenset({"property", "cached_property", "computed_field"})
_OVERLOAD_DECORATORS: frozenset[str] = frozenset({"overload"})
_DISCOVER_SKIP_DIRS: frozenset[str] = frozenset(
    {"__pycache__", "venv", ".venv", "node_modules", "build", "dist"}
)


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
    internal_pkgs: frozenset[str] = frozenset()
    symbols: list[SymbolNode] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)

    def add(self, node: SymbolNode) -> None:
        if node.name != "_" and node.qualname not in self.seen:
            self.seen.add(node.qualname)
            self.symbols.append(node)

    def add_symbol(
        self,
        name: str,
        kind: SymbolKind,
        stmt: ast.stmt,
        scope: tuple[str, ...],
        *,
        pos: ast.stmt | ast.expr | None = None,
        ast_hash: str | None = None,
        decorators: list[ast.expr] | None = None,
    ) -> None:
        anchor = pos if pos is not None else stmt
        first_line = decorators[0].lineno if decorators else stmt.lineno
        dec_names = tuple(ast.unparse(d) for d in decorators) if decorators else ()
        self.add(
            SymbolNode(
                name=name,
                qualname=".".join([*scope, name]),
                kind=kind,
                location=f"{self.filepath}:{anchor.lineno}:{anchor.col_offset + 1}",
                start_line=first_line,
                end_line=stmt.end_lineno or stmt.lineno,
                ast_hash=ast_hash,
                decorators=dec_names,
            )
        )

    def add_external(self, name: str, qualname: str, alias: ast.alias) -> None:
        self.add(
            SymbolNode(
                name=name,
                qualname=qualname,
                kind="external",
                location=f"{self.filepath}:{alias.lineno}:{alias.col_offset + 1}",
                start_line=alias.lineno,
                end_line=alias.end_lineno or alias.lineno,
            )
        )


def _has_decorator(decorators: list[ast.expr], names: frozenset[str]) -> bool:
    for raw in decorators:
        dec = raw.func if isinstance(raw, ast.Call) else raw
        if isinstance(dec, ast.Name) and dec.id in names:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr in names:
            return True
    return False


def _is_type_alias(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "TypeAlias"
    if isinstance(annotation, ast.Attribute):
        return annotation.attr == "TypeAlias"
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value == "TypeAlias" or annotation.value.endswith(".TypeAlias")
    return False


def _is_main_guard(test: ast.expr) -> bool:
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
    ):
        return False
    left, right = test.left, test.comparators[0]
    return any(
        isinstance(a, ast.Name)
        and a.id == "__name__"
        and isinstance(b, ast.Constant)
        and b.value == "__main__"
        for a, b in ((left, right), (right, left))
    )


def _target_nodes(target: ast.expr) -> list[ast.Name]:
    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, ast.Starred):
        return _target_nodes(target.value)
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
    if isinstance(target, ast.Starred):
        return _self_attr_nodes(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [attr for elt in target.elts for attr in _self_attr_nodes(elt)]
    return []


def _is_trivial_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    ):
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return (
            (isinstance(exc, ast.Name) and exc.id == "NotImplementedError")
            or (isinstance(exc, ast.Attribute) and exc.attr == "NotImplementedError")
        )
    return False


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


def _walk_callable(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_async: bool,
    scope: tuple[str, ...],
    context: _ScopeContext,
    state: _WalkState,
) -> None:
    if _has_decorator(stmt.decorator_list, _OVERLOAD_DECORATORS):
        return

    in_class = context == "class"
    if in_class and _has_decorator(stmt.decorator_list, _PROPERTY_DECORATORS):
        kind: SymbolKind = "property"
    elif in_class:
        kind = "async_method" if is_async else "method"
    else:
        kind = "async_function" if is_async else "function"

    state.add_symbol(
        stmt.name,
        kind,
        stmt,
        scope,
        ast_hash=_body_hash(stmt.body),
        decorators=stmt.decorator_list,
    )
    body_ctx: _ScopeContext = (
        "init" if in_class and stmt.name in {"__init__", "__post_init__"} else "function"
    )
    _walk_body(stmt.body, (*scope, stmt.name), body_ctx, state)


def _walk_assign(
    stmt: ast.Assign | ast.AnnAssign,
    scope: tuple[str, ...],
    context: _ScopeContext,
    state: _WalkState,
) -> None:
    targets = [stmt.target] if isinstance(stmt, ast.AnnAssign) else stmt.targets

    if context == "init":
        for target in targets:
            for attr_node in _self_attr_nodes(target):
                state.add_symbol(attr_node.attr, "field", stmt, scope[:-1], pos=attr_node)
        return

    if context not in {"module", "class"}:
        return

    if (
        isinstance(stmt, ast.AnnAssign)
        and stmt.value is not None
        and _is_type_alias(stmt.annotation)
    ):
        if isinstance(stmt.target, ast.Name):
            state.add_symbol(stmt.target.id, "type_alias", stmt, scope, pos=stmt.target)
        return

    for target in targets:
        for name_node in _target_nodes(target):
            name = name_node.id
            kind: SymbolKind = (
                "field"
                if context == "class"
                else ("constant" if name.isupper() else "variable")
            )
            state.add_symbol(name, kind, stmt, scope, pos=name_node)


def _walk_import(stmt: ast.Import | ast.ImportFrom, state: _WalkState) -> None:
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            if alias.name.split(".", 1)[0] in state.internal_pkgs:
                continue
            short_name = alias.name.rsplit(".", 1)[-1]
            state.add_external(short_name, alias.name, alias)
        return

    if stmt.level > 0 or not stmt.module:
        return
    if stmt.module.split(".", 1)[0] in state.internal_pkgs:
        return
    for alias in stmt.names:
        if alias.name != "*":
            state.add_external(alias.name, f"{stmt.module}.{alias.name}", alias)


def _walk_stmt(
    stmt: ast.stmt,
    scope: tuple[str, ...],
    context: _ScopeContext,
    state: _WalkState,
) -> None:
    match stmt:
        case ast.ClassDef():
            state.add_symbol(
                stmt.name,
                "class",
                stmt,
                scope,
                ast_hash=_body_hash(stmt.body),
                decorators=stmt.decorator_list,
            )
            _walk_body(stmt.body, (*scope, stmt.name), "class", state)

        case ast.FunctionDef():
            _walk_callable(stmt, is_async=False, scope=scope, context=context, state=state)

        case ast.AsyncFunctionDef():
            _walk_callable(stmt, is_async=True, scope=scope, context=context, state=state)

        case ast.TypeAlias() if context in {"module", "class"}:
            state.add_symbol(stmt.name.id, "type_alias", stmt, scope, pos=stmt.name)

        case ast.Assign() | ast.AnnAssign():
            _walk_assign(stmt, scope, context, state)

        case ast.Import() | ast.ImportFrom():
            _walk_import(stmt, state)

        case ast.If() if context == "module" and _is_main_guard(stmt.test):
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


def discover_internal_packages(*roots: str | Path) -> frozenset[str]:
    """Return top-level Python package and module names under the given root directories."""
    names: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root).resolve()
        if not root.is_dir():
            continue
        if (root / "__init__.py").exists() and root.name.isidentifier():
            names.add(root.name)
        for entry in root.iterdir():
            if entry.name.startswith(".") or entry.name in _DISCOVER_SKIP_DIRS:
                continue
            if (
                entry.is_dir()
                and entry.name.isidentifier()
                and ((entry / "__init__.py").exists() or any(entry.glob("*.py")))
            ):
                names.add(entry.name)
            elif (
                entry.is_file()
                and entry.suffix == ".py"
                and entry.stem != "__init__"
                and entry.stem.isidentifier()
            ):
                names.add(entry.stem)
    return frozenset(names)


def module_name_from_path(
    filepath: str | Path, src_root: str | Path | None = None
) -> str:
    """Convert a Python file path into its dotted module name (stripping __init__)."""
    path = Path(filepath).resolve()
    if src_root is None:
        return path.parent.name if path.name == "__init__.py" else path.stem

    root = Path(src_root).resolve()
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else root.name


def extract_symbols(
    source: str,
    *,
    filename: str | Path = "<memory>",
    module: str = "",
    internal_packages: Collection[str] = (),
) -> list[SymbolNode]:
    """Parse Python source and return module, class, field, and callable symbol nodes."""
    raw_name = str(filename)
    full_path = raw_name if raw_name == "<memory>" else str(Path(filename).resolve())
    tree = ast.parse(source, filename=full_path)

    if module:
        mod_name = module.rsplit(".", 1)[-1]
        mod_qualname = module
    elif raw_name == "<memory>":
        mod_name = "<module>"
        mod_qualname = "<module>"
    else:
        mod_name = module_name_from_path(full_path)
        mod_qualname = mod_name
    initial_scope: tuple[str, ...] = (mod_qualname,)

    internal = set(internal_packages)
    if mod_qualname != "<module>":
        internal.add(mod_qualname.split(".", 1)[0])

    total_lines = max(len(source.splitlines()), 1)
    state = _WalkState(filepath=full_path, internal_pkgs=frozenset(internal))
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


def parse_file(
    filepath: str | Path,
    *,
    src_root: str | Path | None = None,
    internal_packages: Collection[str] | None = None,
) -> list[SymbolNode]:
    """Read a Python file from disk and extract all symbol nodes."""
    path = Path(filepath).resolve()
    mod = module_name_from_path(path, src_root)
    if internal_packages is None:
        internal: Collection[str] = (
            discover_internal_packages(src_root) if src_root is not None else ()
        )
    else:
        internal = internal_packages
    return extract_symbols(
        path.read_text(encoding="utf-8"),
        filename=path,
        module=mod,
        internal_packages=internal,
    )
