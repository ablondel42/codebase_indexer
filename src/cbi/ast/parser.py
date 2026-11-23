"""Extract symbol definitions and their kinds from Python source using stdlib ast."""

import ast
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


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self, filepath: str, module: str = "") -> None:
        self.symbols: list[SymbolNode] = []
        self._filepath: str = filepath
        self._scope: list[str] = [module] if module else []
        self._context: list[_ScopeContext] = ["module"]

    def _qualname(self, name: str) -> str:
        return ".".join([*self._scope, name])

    def _add(
        self,
        name: str,
        kind: SymbolKind,
        node: ast.stmt,
        *,
        decorators: list[ast.expr] | None = None,
        line: int | None = None,
        col_offset: int | None = None,
    ) -> None:
        loc_line = line if line is not None else node.lineno
        loc_col = (col_offset if col_offset is not None else node.col_offset) + 1
        dec_names = tuple(ast.unparse(d) for d in decorators) if decorators else ()
        self.symbols.append(
            SymbolNode(
                name=name,
                qualname=self._qualname(name),
                kind=kind,
                location=f"{self._filepath}:{loc_line}:{loc_col}",
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                decorators=dec_names,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node.name, "class", node, decorators=node.decorator_list)
        self._scope.append(node.name)
        self._context.append("class")
        self.generic_visit(node)
        self._context.pop()
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_callable(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_callable(node, is_async=True)

    def _visit_callable(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool
    ) -> None:
        in_class = self._context[-1] == "class"
        if in_class and _is_property(node.decorator_list):
            kind: SymbolKind = "property"
        elif in_class:
            kind = "async_method" if is_async else "method"
        else:
            kind = "async_function" if is_async else "function"

        self._add(node.name, kind, node, decorators=node.decorator_list)
        self._scope.append(node.name)
        self._context.append("function")
        self.generic_visit(node)
        self._context.pop()
        self._scope.pop()

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        if self._context[-1] != "function":
            self._add(node.name.id, "type_alias", node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        ctx = self._context[-1]
        if ctx == "function" or not isinstance(node.target, ast.Name):
            return

        name = node.target.id
        if _is_type_alias(node.annotation):
            kind: SymbolKind = "type_alias"
        elif ctx == "class":
            kind = "field"
        else:
            kind = "constant" if name.isupper() else "variable"
        self._add(name, kind, node)

    def visit_Assign(self, node: ast.Assign) -> None:
        ctx = self._context[-1]
        if ctx == "function":
            return

        for target in node.targets:
            for name_node in _target_nodes(target):
                name = name_node.id
                kind: SymbolKind = (
                    "field" if ctx == "class" else ("constant" if name.isupper() else "variable")
                )
                self._add(
                    name,
                    kind,
                    node,
                    line=name_node.lineno,
                    col_offset=name_node.col_offset,
                )


def extract_symbols(
    source: str, *, filename: str | Path = "<memory>", module: str = ""
) -> list[SymbolNode]:
    """Parse Python source and return all module/class/callable symbol definitions."""
    raw_name = str(filename)
    full_path = raw_name if raw_name == "<memory>" else str(Path(filename).resolve())
    tree = ast.parse(source, filename=full_path)
    visitor = _SymbolVisitor(filepath=full_path, module=module)
    visitor.visit(tree)
    return visitor.symbols


if __name__ == "__main__":
    mock_path = Path(__file__).with_name("mock.py")
    extracted = extract_symbols(
        mock_path.read_text(encoding="utf-8"),
        filename=mock_path,
        module="cbi.ast.mock",
    )
    for sym in extracted:
        print(f"{sym.kind:<15} {sym.qualname:<42} {sym.location}")
