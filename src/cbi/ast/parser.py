"""Extract symbol definitions and their kinds from Python source using stdlib ast."""

import ast
import hashlib
import sys
import tokenize
import warnings
from collections.abc import Collection
from dataclasses import dataclass, field, replace
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

_ScopeContext: TypeAlias = Literal["module", "class", "method", "function"]
_ParamKind: TypeAlias = Literal["posonly", "arg", "vararg", "kwonly", "kwarg"]
_PROPERTY_DECORATORS: frozenset[str] = frozenset(
    {"property", "cached_property", "computed_field", "abstractproperty"}
)
_OVERLOAD_DECORATORS: frozenset[str] = frozenset({"overload"})
_STATICMETHOD_DECORATORS: frozenset[str] = frozenset({"staticmethod", "abstractstaticmethod"})
_CLASSMETHOD_DECORATORS: frozenset[str] = frozenset({"classmethod", "abstractclassmethod"})
_MAX_FILE_BYTES: int = 2 * 1024 * 1024
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
    line: int
    col_offset: int
    start_line: int
    end_line: int
    ast_hash: str | None = None
    decorators: tuple[str, ...] = ()


@dataclass(slots=True)
class _WalkState:
    filepath: str
    lines_utf8: tuple[bytes, ...]
    internal_pkgs: frozenset[str] = frozenset()
    symbols: list[SymbolNode] = field(default_factory=list)
    index_by_qualname: dict[str, int] = field(default_factory=dict)

    def def_pos(
        self, stmt: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> tuple[int, int]:
        """Return (line, col_offset) pointing at the class or function identifier."""
        if isinstance(stmt, ast.ClassDef):
            prefix = b"class "
        elif isinstance(stmt, ast.AsyncFunctionDef):
            prefix = b"async def "
        else:
            prefix = b"def "
        expected = prefix + stmt.name.encode("utf-8")
        row = self.lines_utf8[stmt.lineno - 1]
        after = stmt.col_offset + len(expected)
        if row[stmt.col_offset : after] == expected and (
            after == len(row) or row[after] in b"([: \t\\#"
        ):
            return stmt.lineno, stmt.col_offset + len(prefix)
        return stmt.lineno, stmt.col_offset

    def attr_pos(self, node: ast.Attribute) -> tuple[int, int]:
        """Return (line, col_offset) pointing at the attribute name on a dot access."""
        end_ln = node.end_lineno or node.lineno
        end_col = node.end_col_offset or 0
        dot_attr = b"." + node.attr.encode("utf-8")
        row = self.lines_utf8[end_ln - 1]
        if end_col >= len(dot_attr) and row[end_col - len(dot_attr) : end_col] == dot_attr:
            return end_ln, end_col - len(dot_attr) + 1
        return node.lineno, node.col_offset

    def import_pos(self, alias: ast.alias, short_name: str) -> tuple[int, int]:
        """Return (line, col_offset) pointing at the short imported symbol name."""
        if "." not in alias.name:
            return alias.lineno, alias.col_offset
        row = self.lines_utf8[alias.lineno - 1]
        full_b = alias.name.encode("utf-8")
        short_b = short_name.encode("utf-8")
        after = alias.col_offset + len(full_b)
        if row[alias.col_offset : after] == full_b and (
            after == len(row) or row[after] in b" \t,;\\#)"
        ):
            return alias.lineno, after - len(short_b)
        return alias.lineno, alias.col_offset

    def add(self, node: SymbolNode) -> None:
        """Record a symbol node once unless it is '_' or its qualname already exists."""
        if node.name != "_" and node.qualname not in self.index_by_qualname:
            self.index_by_qualname[node.qualname] = len(self.symbols)
            self.symbols.append(node)

    def merge_property_accessor(
        self,
        qualname: str,
        accessor_kind: Literal["setter", "deleter"],
        end_line: int,
        acc_hash: str | None,
    ) -> bool:
        """Fold a @prop.setter or @prop.deleter span and hash into its property node."""
        idx = self.index_by_qualname.get(qualname)
        if idx is None or self.symbols[idx].kind != "property":
            return False
        prev = self.symbols[idx]
        if prev.ast_hash is None and acc_hash is None:
            merged_hash = None
        else:
            raw = f"{prev.ast_hash or ''}|{accessor_kind}:{acc_hash or ''}".encode()
            merged_hash = hashlib.sha256(raw).hexdigest()[:16]
        self.symbols[idx] = replace(
            prev,
            end_line=max(prev.end_line, end_line),
            ast_hash=merged_hash,
        )
        return True

    def add_symbol(
        self,
        name: str,
        kind: SymbolKind,
        stmt: ast.stmt,
        scope: tuple[str, ...],
        *,
        line: int,
        col_offset: int,
        ast_hash: str | None = None,
        decorators: list[ast.expr] | None = None,
    ) -> None:
        """Build and register an internal SymbolNode for the given AST statement."""
        first_line = decorators[0].lineno if decorators else stmt.lineno
        dec_names = tuple(ast.unparse(d) for d in decorators) if decorators else ()
        self.add(
            SymbolNode(
                name=name,
                qualname=".".join([*scope, name]),
                kind=kind,
                location=f"{self.filepath}:{line}:{col_offset + 1}",
                line=line,
                col_offset=col_offset,
                start_line=first_line,
                end_line=stmt.end_lineno or stmt.lineno,
                ast_hash=ast_hash,
                decorators=dec_names,
            )
        )

    def add_external(self, name: str, qualname: str, alias: ast.alias) -> None:
        """Build and register an external SymbolNode for an import alias."""
        line, col_offset = self.import_pos(alias, name)
        self.add(
            SymbolNode(
                name=name,
                qualname=qualname,
                kind="external",
                location=f"{self.filepath}:{line}:{col_offset + 1}",
                line=line,
                col_offset=col_offset,
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
        """Build and register a parameter SymbolNode for a callable argument."""
        self.add(
            SymbolNode(
                name=arg.arg,
                qualname=".".join([*scope, arg.arg]),
                kind="parameter",
                location=f"{self.filepath}:{arg.lineno}:{arg.col_offset + 1}",
                line=arg.lineno,
                col_offset=arg.col_offset,
                start_line=arg.lineno,
                end_line=end_line,
                ast_hash=ast_hash,
            )
        )


def _has_decorator(decorators: list[ast.expr], names: frozenset[str]) -> bool:
    """Return True if any decorator's simple or attribute name is in names."""
    for raw in decorators:
        dec = raw.func if isinstance(raw, ast.Call) else raw
        if isinstance(dec, ast.Name) and dec.id in names:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr in names:
            return True
    return False


def _property_accessor_kind(
    decorators: list[ast.expr], name: str
) -> Literal["setter", "deleter"] | None:
    """Return 'setter' or 'deleter' when decorators match @<name>.setter/.deleter."""
    for raw in decorators:
        if (
            isinstance(raw, ast.Attribute)
            and isinstance(raw.value, ast.Name)
            and raw.value.id == name
        ):
            if raw.attr == "setter":
                return "setter"
            if raw.attr == "deleter":
                return "deleter"
    return None


def _is_type_alias(annotation: ast.expr) -> bool:
    """Return True if annotation denotes typing.TypeAlias (including string forms)."""
    if isinstance(annotation, ast.Name):
        return annotation.id == "TypeAlias"
    if isinstance(annotation, ast.Attribute):
        return annotation.attr == "TypeAlias"
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value == "TypeAlias" or annotation.value.endswith(".TypeAlias")
    return False


def _is_final_annotation(annotation: ast.expr) -> bool:
    """Return True if annotation denotes typing.Final or Final[...]."""
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
    """Return True if test matches __name__ == '__main__' in either operand order."""
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
    """Collect all ast.Name assignment targets, recursing into unpacking."""
    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, ast.Starred):
        return _target_nodes(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [name_node for elt in target.elts for name_node in _target_nodes(elt)]
    return []


def _receiver_attr_nodes(target: ast.expr, receiver: str) -> list[ast.Attribute]:
    """Collect all <receiver>.<attr> assignment targets, recursing into unpacking."""
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == receiver
    ):
        return [target]
    if isinstance(target, ast.Starred):
        return _receiver_attr_nodes(target.value, receiver)
    if isinstance(target, ast.Tuple | ast.List):
        return [attr for elt in target.elts for attr in _receiver_attr_nodes(elt, receiver)]
    return []


def _is_trivial_stmt(stmt: ast.stmt) -> bool:
    """Return True if stmt is pass, Ellipsis (...), or raise NotImplementedError."""
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
    """Compute a 16-char SHA-256 structural hash of non-trivial body statements."""
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

    hasher = hashlib.sha256()
    for idx, s in enumerate(stmts):
        if idx:
            hasher.update(b"\n")
        hasher.update(ast.dump(s, include_attributes=False).encode("utf-8"))
    return hasher.hexdigest()[:16]


def _param_hash(
    param_kind: _ParamKind,
    annotation: ast.expr | None,
    default: ast.expr | None,
) -> str | None:
    """Compute a 16-char SHA-256 hash of a parameter's kind, annotation, and default."""
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
    """Extract parameter SymbolNodes in signature order, skipping bound receivers."""
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
    if skip_receiver and entries and entries[0][0] in {"posonly", "arg"}:
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
) -> tuple[_ScopeContext, str | None] | None:
    """Record a function, method, or property symbol and return its body walk context."""
    if _has_decorator(stmt.decorator_list, _OVERLOAD_DECORATORS):
        return None

    in_class = context == "class"
    qualname = ".".join([*scope, stmt.name])
    acc_kind = _property_accessor_kind(stmt.decorator_list, stmt.name) if in_class else None
    body_digest = _body_hash(stmt.body)
    end_line = stmt.end_lineno or stmt.lineno

    if acc_kind is None or not state.merge_property_accessor(
        qualname, acc_kind, end_line, body_digest
    ):
        if in_class and (
            _has_decorator(stmt.decorator_list, _PROPERTY_DECORATORS) or acc_kind is not None
        ):
            kind: SymbolKind = "property"
        elif in_class:
            kind = "async_method" if is_async else "method"
        else:
            kind = "async_function" if is_async else "function"

        line, col_offset = state.def_pos(stmt)
        state.add_symbol(
            stmt.name,
            kind,
            stmt,
            scope,
            line=line,
            col_offset=col_offset,
            ast_hash=body_digest,
            decorators=stmt.decorator_list,
        )

    is_static = _has_decorator(stmt.decorator_list, _STATICMETHOD_DECORATORS)
    _walk_params(
        stmt.args,
        (*scope, stmt.name),
        skip_receiver=in_class and not is_static,
        state=state,
    )
    pos_params = [*stmt.args.posonlyargs, *stmt.args.args]
    has_inst_receiver = (
        in_class
        and not is_static
        and not _has_decorator(stmt.decorator_list, _CLASSMETHOD_DECORATORS)
        and bool(pos_params)
    )
    if has_inst_receiver:
        return ("method", pos_params[0].arg)
    return ("function", None)


def _walk_assign(
    stmt: ast.Assign | ast.AnnAssign,
    scope: tuple[str, ...],
    context: _ScopeContext,
    receiver: str | None,
    state: _WalkState,
) -> None:
    """Extract variable, constant, type_alias, or field symbols from an assignment."""
    targets = [stmt.target] if isinstance(stmt, ast.AnnAssign) else stmt.targets

    if context == "method" and receiver is not None:
        for target in targets:
            for attr_node in _receiver_attr_nodes(target, receiver):
                line, col_offset = state.attr_pos(attr_node)
                state.add_symbol(
                    attr_node.attr,
                    "field",
                    stmt,
                    scope[:-1],
                    line=line,
                    col_offset=col_offset,
                )
        return

    if context not in {"module", "class"}:
        return

    if (
        isinstance(stmt, ast.AnnAssign)
        and stmt.value is not None
        and _is_type_alias(stmt.annotation)
    ):
        if isinstance(stmt.target, ast.Name):
            state.add_symbol(
                stmt.target.id,
                "type_alias",
                stmt,
                scope,
                line=stmt.target.lineno,
                col_offset=stmt.target.col_offset,
            )
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
            state.add_symbol(
                name,
                kind,
                stmt,
                scope,
                line=name_node.lineno,
                col_offset=name_node.col_offset,
            )


def _walk_import(stmt: ast.Import | ast.ImportFrom, state: _WalkState) -> None:
    """Extract external SymbolNodes from non-internal absolute imports."""
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
    """Iteratively walk statement blocks to extract symbols across nested scopes."""
    stack: list[tuple[ast.stmt, tuple[str, ...], _ScopeContext, str | None]] = [
        (stmt, scope, context, None) for stmt in reversed(stmts)
    ]
    while stack:
        stmt, cur_scope, cur_ctx, cur_recv = stack.pop()
        match stmt:
            case ast.ClassDef():
                line, col_offset = state.def_pos(stmt)
                state.add_symbol(
                    stmt.name,
                    "class",
                    stmt,
                    cur_scope,
                    line=line,
                    col_offset=col_offset,
                    ast_hash=_body_hash(stmt.body),
                    decorators=stmt.decorator_list,
                )
                child_scope = (*cur_scope, stmt.name)
                stack.extend((child, child_scope, "class", None) for child in reversed(stmt.body))

            case ast.FunctionDef():
                body_ctx = _record_callable(
                    stmt, is_async=False, scope=cur_scope, context=cur_ctx, state=state
                )
                if body_ctx is not None:
                    next_ctx, next_recv = body_ctx
                    child_scope = (*cur_scope, stmt.name)
                    stack.extend(
                        (child, child_scope, next_ctx, next_recv) for child in reversed(stmt.body)
                    )

            case ast.AsyncFunctionDef():
                body_ctx = _record_callable(
                    stmt, is_async=True, scope=cur_scope, context=cur_ctx, state=state
                )
                if body_ctx is not None:
                    next_ctx, next_recv = body_ctx
                    child_scope = (*cur_scope, stmt.name)
                    stack.extend(
                        (child, child_scope, next_ctx, next_recv) for child in reversed(stmt.body)
                    )

            case ast.TypeAlias() if cur_ctx in {"module", "class"}:
                state.add_symbol(
                    stmt.name.id,
                    "type_alias",
                    stmt,
                    cur_scope,
                    line=stmt.name.lineno,
                    col_offset=stmt.name.col_offset,
                )

            case ast.Assign() | ast.AnnAssign():
                _walk_assign(stmt, cur_scope, cur_ctx, cur_recv, state)

            case ast.Import() | ast.ImportFrom():
                _walk_import(stmt, state)

            case ast.If() if cur_ctx == "module" and _is_main_guard(stmt.test):
                stack.extend(
                    (child, cur_scope, cur_ctx, cur_recv) for child in reversed(stmt.orelse)
                )

            case _:
                children: list[ast.stmt] = []
                for child in ast.iter_child_nodes(stmt):
                    if isinstance(child, ast.stmt):
                        children.append(child)
                    elif isinstance(child, ast.ExceptHandler | ast.match_case):
                        children.extend(child.body)
                stack.extend((child, cur_scope, cur_ctx, cur_recv) for child in reversed(children))


def _is_module_stem(stem: str) -> bool:
    """Return True if stem contains only identifier characters (leading digits allowed)."""
    return bool(stem) and f"_{stem}".isidentifier()


def _is_scannable_subdir(entry: Path) -> bool:
    """Return True if entry is a non-hidden, non-venv identifier directory worth scanning."""
    try:
        return (
            not entry.name.startswith(".")
            and entry.name not in _DISCOVER_SKIP_DIRS
            and entry.name.isidentifier()
            and entry.is_dir()
            and not (entry / "pyvenv.cfg").exists()
        )
    except OSError:
        return False


def _contains_python_module(directory: Path) -> bool:
    """Return True if directory or any scannable subdirectory contains a Python module."""
    visited: set[Path] = set()
    stack: list[Path] = [directory]
    while stack:
        current = stack.pop()
        try:
            resolved = current.resolve()
        except OSError:
            continue
        if resolved in visited:
            continue
        visited.add(resolved)
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for child in entries:
            if child.name.startswith(".") or child.name in _DISCOVER_SKIP_DIRS:
                continue
            try:
                is_py = (
                    child.is_file()
                    and child.suffix == ".py"
                    and _is_module_stem(child.stem)
                )
            except OSError:
                continue
            if is_py:
                return True
            if _is_scannable_subdir(child):
                stack.append(child)
    return False


def discover_internal_packages(*roots: str | Path) -> frozenset[str]:
    """Return top-level Python package and module names under the given root directories."""
    names: set[str] = set()
    for raw_root in roots:
        try:
            root = Path(raw_root).resolve()
            if not root.is_dir() or (root / "pyvenv.cfg").exists():
                continue
            if (root / "__init__.py").exists() and root.name.isidentifier():
                names.add(root.name)
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _DISCOVER_SKIP_DIRS:
                continue
            if _is_scannable_subdir(entry) and _contains_python_module(entry):
                names.add(entry.name)
            else:
                try:
                    is_ident_py = (
                        entry.is_file()
                        and entry.suffix == ".py"
                        and entry.stem != "__init__"
                        and entry.stem.isidentifier()
                    )
                except OSError:
                    continue
                if is_ident_py:
                    names.add(entry.stem)
    return frozenset(names)


def module_name_from_path(
    filepath: str | Path, src_root: str | Path | None = None
) -> str:
    """Convert a Python file path into its dotted module name (stripping __init__)."""
    raw_path = Path(filepath)
    if src_root is None:
        path = raw_path.resolve()
        if path.name == "__init__.py":
            return path.parent.name if path.parent.name.isidentifier() else "__init__"
        return path.stem

    raw_root = Path(src_root)
    try:
        rel = raw_path.absolute().relative_to(raw_root.absolute())
    except ValueError:
        rel = raw_path.resolve().relative_to(raw_root.resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if parts:
        return ".".join(parts)
    root_name = raw_root.resolve().name
    return root_name if root_name.isidentifier() else "__init__"


def extract_symbols(
    source: str,
    *,
    filename: str | Path = "<memory>",
    module: str = "",
    internal_packages: Collection[str] = (),
) -> list[SymbolNode]:
    """Parse Python source and return module, class, field, parameter, and callable symbols."""
    raw_name = str(filename)
    is_pseudo = raw_name.startswith("<") and raw_name.endswith(">")
    full_path = raw_name if is_pseudo else str(Path(filename).resolve())
    clean_source = source.removeprefix("\ufeff")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(clean_source, filename=full_path)

    if module:
        mod_name = module.rsplit(".", 1)[-1]
        mod_qualname = module
    elif is_pseudo:
        mod_name = raw_name if raw_name != "<memory>" else "<module>"
        mod_qualname = mod_name
    else:
        mod_name = module_name_from_path(full_path)
        mod_qualname = mod_name
    initial_scope: tuple[str, ...] = (mod_qualname,)

    internal = set(internal_packages)
    if mod_qualname != "<module>" and not is_pseudo:
        internal.add(mod_qualname.split(".", 1)[0])

    norm_source = clean_source.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = norm_source.removesuffix("\n").split("\n") if norm_source else []
    total_lines = max(len(raw_lines), 1)
    state = _WalkState(
        filepath=full_path,
        lines_utf8=tuple(ln.encode("utf-8", errors="replace") for ln in raw_lines),
        internal_pkgs=frozenset(internal),
    )
    state.add(
        SymbolNode(
            name=mod_name,
            qualname=mod_qualname,
            kind="module",
            location=f"{full_path}:1:1",
            line=1,
            col_offset=0,
            start_line=1,
            end_line=total_lines,
        )
    )
    _walk_body(tree.body, initial_scope, "module", state)
    return state.symbols


def _iter_python_files(root: Path) -> list[Path]:
    """Return all indexable Python files under root in deterministic sorted path order."""
    try:
        if not root.is_dir() or (root / "pyvenv.cfg").exists():
            return []
    except OSError:
        return []
    files: list[Path] = []
    visited: set[Path] = set()
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            resolved = current.resolve()
        except OSError:
            continue
        if resolved in visited:
            continue
        visited.add(resolved)
        subdirs: list[Path] = []
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _DISCOVER_SKIP_DIRS:
                continue
            try:
                is_py = (
                    entry.is_file()
                    and entry.suffix == ".py"
                    and _is_module_stem(entry.stem)
                )
            except OSError:
                continue
            if is_py:
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
    raw_path = Path(filepath)
    mod = module_name_from_path(raw_path, src_root)
    path = raw_path.resolve()
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
    max_file_bytes: int = _MAX_FILE_BYTES,
) -> list[SymbolNode]:
    """Parse all Python modules under root in sorted order, skipping broken/oversized files."""
    root_path = Path(root).resolve()
    internal: Collection[str] = (
        discover_internal_packages(root_path)
        if internal_packages is None
        else internal_packages
    )
    symbols: list[SymbolNode] = []
    for filepath in _iter_python_files(root_path):
        try:
            if filepath.stat().st_size > max_file_bytes:
                continue
            symbols.extend(parse_file(filepath, src_root=root_path, internal_packages=internal))
        except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError, OSError):
            continue
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
