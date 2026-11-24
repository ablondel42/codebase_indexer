"""Extract symbol definitions and their kinds from Python source using stdlib ast."""

import ast
import hashlib
import sys
import tokenize
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
    "parameter",
    "constant",
    "variable",
    "type_alias",
    "external",
]

_ScopeContext: TypeAlias = Literal["module", "class", "init", "function"]
_ParamKind: TypeAlias = Literal["posonly", "arg", "vararg", "kwonly", "kwarg"]
_PROPERTY_DECORATORS: frozenset[str] = frozenset(
    {"property", "cached_property", "computed_field", "abstractproperty"}
)
_OVERLOAD_DECORATORS: frozenset[str] = frozenset({"overload"})
_STATICMETHOD_DECORATORS: frozenset[str] = frozenset({"staticmethod", "abstractstaticmethod"})
_RECEIVER_NAMES: frozenset[str] = frozenset({"self", "cls", "mcs", "metacls"})
_DISCOVER_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        "__pypackages__",
        "venv",
        ".venv",
        ".tox",
        ".nox",
        "site-packages",
        "node_modules",
        "build",
        "dist",
    }
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

    def add_param(
        self,
        arg: ast.arg,
        scope: tuple[str, ...],
        *,
        end_line: int,
        ast_hash: str | None,
    ) -> None:
        self.add(
            SymbolNode(
                name=arg.arg,
                qualname=".".join([*scope, arg.arg]),
                kind="parameter",
                location=f"{self.filepath}:{arg.lineno}:{arg.col_offset + 1}",
                start_line=arg.lineno,
                end_line=end_line,
                ast_hash=ast_hash,
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


def _is_final_annotation(annotation: ast.expr) -> bool:
    base = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    if isinstance(base, ast.Name):
        return base.id == "Final"
    if isinstance(base, ast.Attribute):
        return base.attr == "Final"
    if isinstance(base, ast.Constant) and isinstance(base.value, str):
        raw = base.value.split("[", 1)[0].strip()
        return raw == "Final" or raw.endswith(".Final")
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


def _param_hash(
    param_kind: _ParamKind,
    annotation: ast.expr | None,
    default: ast.expr | None,
) -> str | None:
    if param_kind == "arg" and annotation is None and default is None:
        return None
    ann_s = ast.dump(annotation, include_attributes=False) if annotation is not None else ""
    def_s = ast.dump(default, include_attributes=False) if default is not None else ""
    canonical = f"{param_kind}|{ann_s}|{def_s}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _walk_params(
    arguments: ast.arguments,
    scope: tuple[str, ...],
    *,
    skip_receiver: bool,
    state: _WalkState,
) -> None:
    pos_args: list[tuple[_ParamKind, ast.arg]] = [
        *(("posonly", a) for a in arguments.posonlyargs),
        *(("arg", a) for a in arguments.args),
    ]
    pad = len(pos_args) - len(arguments.defaults)
    pos_defaults: list[ast.expr | None] = [*([None] * pad), *arguments.defaults]
    entries: list[tuple[_ParamKind, ast.arg, ast.expr | None]] = [
        (kind_tag, a, d)
        for (kind_tag, a), d in zip(pos_args, pos_defaults, strict=True)
    ]
    if skip_receiver and entries and entries[0][1].arg in _RECEIVER_NAMES:
        entries = entries[1:]
    if arguments.vararg is not None:
        entries.append(("vararg", arguments.vararg, None))
    for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        entries.append(("kwonly", a, d))
    if arguments.kwarg is not None:
        entries.append(("kwarg", arguments.kwarg, None))

    for kind_tag, a, d in entries:
        end_line = max(
            a.lineno,
            a.end_lineno or a.lineno,
            d.end_lineno if d is not None and d.end_lineno is not None else a.lineno,
        )
        state.add_param(
            a,
            scope,
            end_line=end_line,
            ast_hash=_param_hash(kind_tag, a.annotation, d),
        )


def _record_callable(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_async: bool,
    scope: tuple[str, ...],
    context: _ScopeContext,
    state: _WalkState,
) -> _ScopeContext | None:
    if _has_decorator(stmt.decorator_list, _OVERLOAD_DECORATORS):
        return None

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
    skip_receiver = in_class and not _has_decorator(
        stmt.decorator_list, _STATICMETHOD_DECORATORS
    )
    _walk_params(
        stmt.args,
        (*scope, stmt.name),
        skip_receiver=skip_receiver,
        state=state,
    )
    return "init" if in_class and stmt.name in {"__init__", "__post_init__"} else "function"


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

    is_final = isinstance(stmt, ast.AnnAssign) and _is_final_annotation(stmt.annotation)
    for target in targets:
        for name_node in _target_nodes(target):
            name = name_node.id
            kind: SymbolKind = (
                "field"
                if context == "class"
                else ("constant" if (name.isupper() or is_final) else "variable")
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


def _walk_body(
    stmts: list[ast.stmt],
    scope: tuple[str, ...],
    context: _ScopeContext,
    state: _WalkState,
) -> None:
    stack: list[tuple[ast.stmt, tuple[str, ...], _ScopeContext]] = [
        (stmt, scope, context) for stmt in reversed(stmts)
    ]
    while stack:
        stmt, cur_scope, cur_ctx = stack.pop()
        match stmt:
            case ast.ClassDef():
                state.add_symbol(
                    stmt.name,
                    "class",
                    stmt,
                    cur_scope,
                    ast_hash=_body_hash(stmt.body),
                    decorators=stmt.decorator_list,
                )
                child_scope = (*cur_scope, stmt.name)
                stack.extend((child, child_scope, "class") for child in reversed(stmt.body))

            case ast.FunctionDef():
                body_ctx = _record_callable(
                    stmt, is_async=False, scope=cur_scope, context=cur_ctx, state=state
                )
                if body_ctx is not None:
                    child_scope = (*cur_scope, stmt.name)
                    stack.extend((child, child_scope, body_ctx) for child in reversed(stmt.body))

            case ast.AsyncFunctionDef():
                body_ctx = _record_callable(
                    stmt, is_async=True, scope=cur_scope, context=cur_ctx, state=state
                )
                if body_ctx is not None:
                    child_scope = (*cur_scope, stmt.name)
                    stack.extend((child, child_scope, body_ctx) for child in reversed(stmt.body))

            case ast.TypeAlias() if cur_ctx in {"module", "class"}:
                state.add_symbol(stmt.name.id, "type_alias", stmt, cur_scope, pos=stmt.name)

            case ast.Assign() | ast.AnnAssign():
                _walk_assign(stmt, cur_scope, cur_ctx, state)

            case ast.Import() | ast.ImportFrom():
                _walk_import(stmt, state)

            case ast.If() if cur_ctx == "module" and _is_main_guard(stmt.test):
                stack.extend((child, cur_scope, cur_ctx) for child in reversed(stmt.orelse))

            case _:
                children: list[ast.stmt] = []
                for child in ast.iter_child_nodes(stmt):
                    if isinstance(child, ast.stmt):
                        children.append(child)
                    elif isinstance(child, ast.ExceptHandler | ast.match_case):
                        children.extend(child.body)
                stack.extend((child, cur_scope, cur_ctx) for child in reversed(children))


def _is_scannable_subdir(entry: Path) -> bool:
    return (
        not entry.name.startswith(".")
        and entry.name not in _DISCOVER_SKIP_DIRS
        and entry.name.isidentifier()
        and entry.is_dir()
        and not (entry / "pyvenv.cfg").exists()
    )


def _contains_python_module(directory: Path) -> bool:
    visited: set[Path] = set()
    stack: list[Path] = [directory]
    while stack:
        current = stack.pop()
        resolved = current.resolve()
        if resolved in visited:
            continue
        visited.add(resolved)
        for child in current.iterdir():
            if child.name.startswith(".") or child.name in _DISCOVER_SKIP_DIRS:
                continue
            if (
                child.is_file()
                and child.suffix == ".py"
                and (child.stem == "__init__" or child.stem.isidentifier())
            ):
                return True
            if _is_scannable_subdir(child):
                stack.append(child)
    return False


def discover_internal_packages(*roots: str | Path) -> frozenset[str]:
    """Return top-level Python package and module names under the given root directories."""
    names: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root).resolve()
        if not root.is_dir() or (root / "pyvenv.cfg").exists():
            continue
        if (root / "__init__.py").exists() and root.name.isidentifier():
            names.add(root.name)
        for entry in root.iterdir():
            if entry.name.startswith(".") or entry.name in _DISCOVER_SKIP_DIRS:
                continue
            if _is_scannable_subdir(entry) and _contains_python_module(entry):
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
        if path.name == "__init__.py":
            return path.parent.name if path.parent.name.isidentifier() else "__init__"
        return path.stem

    root = Path(src_root).resolve()
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if parts:
        return ".".join(parts)
    return root.name if root.name.isidentifier() else "__init__"


def extract_symbols(
    source: str,
    *,
    filename: str | Path = "<memory>",
    module: str = "",
    internal_packages: Collection[str] = (),
) -> list[SymbolNode]:
    """Parse Python source and return module, class, field, parameter, and callable symbols."""
    raw_name = str(filename)
    full_path = raw_name if raw_name == "<memory>" else str(Path(filename).resolve())
    clean_source = source.removeprefix("\ufeff")
    tree = ast.parse(clean_source, filename=full_path)

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

    total_lines = max(len(clean_source.splitlines()), 1)
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


def _iter_python_files(root: Path) -> list[Path]:
    if not root.is_dir() or (root / "pyvenv.cfg").exists():
        return []
    files: list[Path] = []
    visited: set[Path] = set()
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        resolved = current.resolve()
        if resolved in visited:
            continue
        visited.add(resolved)
        subdirs: list[Path] = []
        for entry in sorted(current.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or entry.name in _DISCOVER_SKIP_DIRS:
                continue
            if (
                entry.is_file()
                and entry.suffix == ".py"
                and (entry.stem == "__init__" or entry.stem.isidentifier())
            ):
                files.append(entry)
            elif _is_scannable_subdir(entry):
                subdirs.append(entry)
        stack.extend(reversed(subdirs))
    return files


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
    with tokenize.open(path) as handle:
        source = handle.read()
    return extract_symbols(
        source,
        filename=path,
        module=mod,
        internal_packages=internal,
    )


def parse_directory(
    root: str | Path,
    *,
    internal_packages: Collection[str] | None = None,
) -> list[SymbolNode]:
    """Parse all Python modules under root (across all subdirectories) in sorted path order."""
    root_path = Path(root).resolve()
    internal: Collection[str] = (
        discover_internal_packages(root_path)
        if internal_packages is None
        else internal_packages
    )
    symbols: list[SymbolNode] = []
    for filepath in _iter_python_files(root_path):
        symbols.extend(parse_file(filepath, src_root=root_path, internal_packages=internal))
    return symbols


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1]).resolve()
        src_arg = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
        extracted = (
            parse_directory(target)
            if target.is_dir()
            else parse_file(target, src_root=src_arg)
        )
    else:
        mock_path = Path(__file__).with_name("mock.py").resolve()
        extracted = parse_file(mock_path, src_root=mock_path.parents[2])
    for sym in extracted:
        digest = sym.ast_hash or "-"
        print(f"{sym.kind:<15} {digest:<18} {sym.qualname:<40} {sym.location}")
