"""1-to-1 fixture-backed AST symbol extraction tests for cbi.ast."""

import ast
import dataclasses
import hashlib
import tokenize
from collections.abc import Collection
from pathlib import Path
from typing import Any, cast, get_args

import pytest

import cbi.ast as cbi_ast
from cbi.ast import (
    SymbolKind,
    SymbolNode,
    discover_internal_packages,
    extract_symbols,
    module_name_from_path,
    parse_file,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
FIXTURES_ROOT = TESTS_ROOT / "fixtures"
MOCK_PATH = FIXTURES_ROOT / "mock.py"
AST_FIXTURES = FIXTURES_ROOT / "ast"
SAMPLE_PKG = AST_FIXTURES / "sample_pkg"

_CALLABLE_OR_CLASS_KINDS: frozenset[SymbolKind] = frozenset(
    {"class", "function", "async_function", "method", "async_method", "property"}
)
_HEX_CHARS: frozenset[str] = frozenset("0123456789abcdef")


def _loc(path: Path, line: int, col: int) -> str:
    return f"{path}:{line}:{col}"


def _expected_hash(stmt_source: str) -> str:
    body = ast.parse(stmt_source).body
    canonical = "\n".join(ast.dump(s, include_attributes=False) for s in body)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _assert_token_at_location(
    sym: SymbolNode, raw_lines: list[str], line: int, col: int
) -> None:
    if not raw_lines:
        assert line == 1
        assert col == 1
        return
    tail = raw_lines[line - 1].encode("utf-8")[col - 1 :]
    match sym.kind:
        case "module":
            assert line == 1
            assert col == 1
        case "class":
            assert tail.startswith(b"class ")
        case "function" | "method":
            assert tail.startswith(b"def ")
        case "async_function" | "async_method":
            assert tail.startswith(b"async ")
        case "property":
            assert tail.startswith((b"def ", b"async "))
        case "constant" | "variable" | "type_alias":
            assert tail.startswith(sym.name.encode("utf-8"))
        case "field":
            assert tail.startswith((sym.name.encode("utf-8"), b"self"))
        case "external":
            assert tail.startswith((sym.name.encode("utf-8"), sym.qualname.encode("utf-8")))


def _assert_invariants(
    symbols: list[SymbolNode],
    source: str,
    filepath: str,
    expected_mod_qualname: str,
) -> None:
    assert symbols
    raw_lines = source.splitlines()
    total_lines = max(len(raw_lines), 1)

    mod = symbols[0]
    assert mod.kind == "module"
    assert mod.qualname == expected_mod_qualname
    assert mod.location == f"{filepath}:1:1"
    assert mod.start_line == 1
    assert mod.end_line == total_lines

    qualnames = [sym.qualname for sym in symbols]
    assert len(qualnames) == len(set(qualnames))

    for sym in symbols:
        assert sym.name != "_"
        loc_path, line_str, col_str = sym.location.rsplit(":", 2)
        line = int(line_str)
        col = int(col_str)
        assert loc_path == filepath
        assert col >= 1
        assert 1 <= sym.start_line <= line <= sym.end_line <= total_lines
        _assert_token_at_location(sym, raw_lines, line, col)

        if sym.kind not in {"module", "external"}:
            assert sym.qualname.startswith(f"{expected_mod_qualname}.")
            assert sym.qualname.endswith(f".{sym.name}")

        if sym.kind in _CALLABLE_OR_CLASS_KINDS:
            if sym.ast_hash is not None:
                assert len(sym.ast_hash) == 16
                assert set(sym.ast_hash) <= _HEX_CHARS
        else:
            assert sym.ast_hash is None
            assert sym.decorators == ()


def _parse_fixture(
    filename: str,
    *,
    module: str = "",
    internal_packages: Collection[str] | None = None,
) -> tuple[Path, str, list[SymbolNode]]:
    path = (AST_FIXTURES / filename).resolve()
    with tokenize.open(path) as handle:
        source = handle.read()
    mod_name = module or module_name_from_path(path, AST_FIXTURES)
    if module:
        symbols = extract_symbols(
            source,
            filename=path,
            module=module,
            internal_packages=internal_packages or (),
        )
    else:
        symbols = parse_file(
            path,
            src_root=AST_FIXTURES,
            internal_packages=internal_packages,
        )
    _assert_invariants(symbols, source, str(path), mod_name)
    return path, source, symbols


# ---------------------------------------------------------------------------
# f00: Full Realistic Integration Fixture (tests/fixtures/mock.py)
# ---------------------------------------------------------------------------


def test_f00_mock_fixture() -> None:
    """Fixture: tests/fixtures/mock.py."""
    path = MOCK_PATH.resolve()
    source = path.read_text(encoding="utf-8")
    symbols = extract_symbols(source, filename=path, module="cbi.ast.mock")
    _assert_invariants(symbols, source, str(path), "cbi.ast.mock")

    by_q = {sym.qualname: sym for sym in symbols}
    assert by_q["cbi.ast.mock.QuotaExceededError"].ast_hash is None
    assert by_q["cbi.ast.mock.TaskRecord"].decorators == ("dataclass(slots=True)",)
    run_batch = by_q["cbi.ast.mock.run_batch"]
    assert run_batch.decorators == ("route('/tasks/batch')",)
    assert run_batch.start_line == int(run_batch.location.rsplit(":", 2)[1]) - 1

    leaked_locals = {
        "cleaned_title",
        "clean_tags",
        "tag",
        "summary",
        "resolved",
        "worker",
        "results",
        "rec",
        "exc",
    }
    assert leaked_locals.isdisjoint({sym.name for sym in symbols})
    assert "cbi.ast.mock.TaskWorker.process.tag_count" not in by_q
    assert "cbi.ast.mock.tag_count" not in by_q

    assert [(sym.qualname, sym.kind) for sym in symbols] == [
        ("cbi.ast.mock", "module"),
        ("asyncio", "external"),
        ("pathlib", "external"),
        ("collections.abc.Callable", "external"),
        ("dataclasses.dataclass", "external"),
        ("dataclasses.field", "external"),
        ("typing.Any", "external"),
        ("typing.Self", "external"),
        ("typing.TypeAlias", "external"),
        ("pydantic.BaseModel", "external"),
        ("pydantic.Field", "external"),
        ("cbi.ast.mock.__all__", "variable"),
        ("cbi.ast.mock.Payload", "type_alias"),
        ("cbi.ast.mock.DEFAULT_RETRIES", "constant"),
        ("cbi.ast.mock.SERVICE_NAME", "constant"),
        ("cbi.ast.mock.ROUTES", "constant"),
        ("cbi.ast.mock.route", "function"),
        ("cbi.ast.mock.route.register", "function"),
        ("cbi.ast.mock.QuotaExceededError", "class"),
        ("cbi.ast.mock.TaskRecord", "class"),
        ("cbi.ast.mock.TaskRecord.task_id", "field"),
        ("cbi.ast.mock.TaskRecord.title", "field"),
        ("cbi.ast.mock.TaskRecord.tags", "field"),
        ("cbi.ast.mock.TaskSummary", "class"),
        ("cbi.ast.mock.TaskSummary.task_id", "field"),
        ("cbi.ast.mock.TaskSummary.title", "field"),
        ("cbi.ast.mock.TaskSummary.tag_count", "field"),
        ("cbi.ast.mock.BaseWorker", "class"),
        ("cbi.ast.mock.BaseWorker.kind", "field"),
        ("cbi.ast.mock.BaseWorker.describe", "method"),
        ("cbi.ast.mock.TaskWorker", "class"),
        ("cbi.ast.mock.TaskWorker.kind", "field"),
        ("cbi.ast.mock.TaskWorker.__init__", "method"),
        ("cbi.ast.mock.TaskWorker.root", "field"),
        ("cbi.ast.mock.TaskWorker.max_retries", "field"),
        ("cbi.ast.mock.TaskWorker._processed", "field"),
        ("cbi.ast.mock.TaskWorker.from_cwd", "method"),
        ("cbi.ast.mock.TaskWorker.normalize_title", "method"),
        ("cbi.ast.mock.TaskWorker.is_ready", "property"),
        ("cbi.ast.mock.TaskWorker.process", "method"),
        ("cbi.ast.mock.TaskWorker.process_async", "async_method"),
        ("cbi.ast.mock.build_worker", "function"),
        ("uuid", "external"),
        ("cbi.ast.mock.run_batch", "async_function"),
        ("cbi.ast.mock.run_batch.format_error", "function"),
        ("decimal.Decimal", "external"),
    ]


# ---------------------------------------------------------------------------
# f01 - f06: Contract, Classes, Callables, Properties, Decorators, Overloads
# ---------------------------------------------------------------------------


def test_f01_all_symbol_kinds() -> None:
    """Fixture: tests/fixtures/ast/f01_all_symbol_kinds.py."""
    path, _, symbols = _parse_fixture("f01_all_symbol_kinds.py")
    mod = "f01_all_symbol_kinds"

    expected_box_body = (
        "tag: str\n"
        "@property\n"
        "def size(self) -> int:\n"
        "    return 1\n"
        "async def fetch(self) -> int:\n"
        "    return 2\n"
        "def ping(self) -> None: ...\n"
    )
    helper_body = 'return int(pathlib.PurePosixPath("a").name == "a") + LIMIT + count'

    assert symbols == [
        SymbolNode(mod, mod, "module", _loc(path, 1, 1), 1, 29),
        SymbolNode("pathlib", "pathlib", "external", _loc(path, 1, 8), 1, 1),
        SymbolNode("dataclass", "dataclasses.dataclass", "external", _loc(path, 2, 25), 2, 2),
        SymbolNode("TypeAlias", "typing.TypeAlias", "external", _loc(path, 3, 20), 3, 3),
        SymbolNode("Alias", f"{mod}.Alias", "type_alias", _loc(path, 5, 1), 5, 5),
        SymbolNode("LIMIT", f"{mod}.LIMIT", "constant", _loc(path, 6, 1), 6, 6),
        SymbolNode("count", f"{mod}.count", "variable", _loc(path, 7, 1), 7, 7),
        SymbolNode(
            "Box",
            f"{mod}.Box",
            "class",
            _loc(path, 11, 1),
            10,
            21,
            ast_hash=_expected_hash(expected_box_body),
            decorators=("dataclass",),
        ),
        SymbolNode("tag", f"{mod}.Box.tag", "field", _loc(path, 12, 5), 12, 12),
        SymbolNode(
            "size",
            f"{mod}.Box.size",
            "property",
            _loc(path, 15, 5),
            14,
            16,
            ast_hash=_expected_hash("return 1"),
            decorators=("property",),
        ),
        SymbolNode(
            "fetch",
            f"{mod}.Box.fetch",
            "async_method",
            _loc(path, 18, 5),
            18,
            19,
            ast_hash=_expected_hash("return 2"),
        ),
        SymbolNode(
            "ping",
            f"{mod}.Box.ping",
            "method",
            _loc(path, 21, 5),
            21,
            21,
            ast_hash=None,
        ),
        SymbolNode(
            "helper",
            f"{mod}.helper",
            "function",
            _loc(path, 24, 1),
            24,
            25,
            ast_hash=_expected_hash(helper_body),
        ),
        SymbolNode(
            "run",
            f"{mod}.run",
            "async_function",
            _loc(path, 28, 1),
            28,
            29,
            ast_hash=_expected_hash("return 4"),
        ),
    ]
    assert {sym.kind for sym in symbols} == set(get_args(SymbolKind))


def test_f02_stacked_and_method_decorators() -> None:
    """Fixture: tests/fixtures/ast/f02_stacked_and_method_decorators.py."""
    path, _, symbols = _parse_fixture("f02_stacked_and_method_decorators.py")
    mod = "f02_stacked_and_method_decorators"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("collections.abc.Callable", "external"),
        ("typing.TypeVar", "external"),
        (f"{mod}.T", "constant"),
        (f"{mod}.first", "function"),
        (f"{mod}.second", "function"),
        (f"{mod}.second.wrap", "function"),
        (f"{mod}.Service", "class"),
        (f"{mod}.Service.make", "method"),
        (f"{mod}.Service.check", "async_method"),
    ]
    by_q = {s.qualname: s for s in symbols}
    svc = by_q[f"{mod}.Service"]
    assert svc.decorators == ("first", "second(flag=True)")
    assert (svc.start_line, svc.location, svc.end_line) == (19, _loc(path, 21, 1), 28)
    assert by_q[f"{mod}.Service.make"].decorators == ("classmethod",)
    assert by_q[f"{mod}.Service.check"].decorators == ("staticmethod",)


def test_f03_pep614_and_multiline_decorators() -> None:
    """Fixture: tests/fixtures/ast/f03_pep614_and_multiline_decorators.py."""
    path, _, symbols = _parse_fixture("f03_pep614_and_multiline_decorators.py")
    mod = "f03_pep614_and_multiline_decorators"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("collections.abc.Callable", "external"),
        ("typing.Any", "external"),
        (f"{mod}.dec_a", "function"),
        (f"{mod}.dec_b", "function"),
        (f"{mod}._Guard", "class"),
        (f"{mod}._Guard.guard", "method"),
        (f"{mod}.app_post", "function"),
        (f"{mod}.decorators", "variable"),
        (f"{mod}.FLAG", "constant"),
        (f"{mod}.registry", "variable"),
        (f"{mod}.Endpoint", "class"),
        (f"{mod}.create_item", "async_function"),
    ]
    by_q = {s.qualname: s for s in symbols}
    endpoint = by_q[f"{mod}.Endpoint"]
    assert (endpoint.start_line, endpoint.location, endpoint.end_line) == (
        29,
        _loc(path, 34, 1),
        35,
    )
    assert endpoint.decorators == (
        "decorators[0]",
        "dec_a if FLAG else dec_b",
        "registry['api'].guard(level=2)",
    )
    assert all("\n" not in dec for dec in endpoint.decorators)

    create_item = by_q[f"{mod}.create_item"]
    assert (create_item.start_line, create_item.location, create_item.end_line) == (
        38,
        _loc(path, 43, 1),
        44,
    )
    assert create_item.decorators == (
        "lambda fn: fn",
        "app_post('/v1/items', status_code=201)",
    )
    assert all("\n" not in dec for dec in create_item.decorators)


def test_f04_property_decorators() -> None:
    """Fixture: tests/fixtures/ast/f04_property_decorators.py."""
    _, _, symbols = _parse_fixture("f04_property_decorators.py")
    mod = "f04_property_decorators"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("functools", "external"),
        ("collections.abc.Callable", "external"),
        ("functools.cached_property", "external"),
        ("typing.Any", "external"),
        ("pydantic", "external"),
        ("pydantic.BaseModel", "external"),
        ("pydantic.computed_field", "external"),
        (f"{mod}.trace", "function"),
        (f"{mod}.top_fn", "function"),
        (f"{mod}.PlainItem", "class"),
        (f"{mod}.PlainItem.p_plain", "property"),
        (f"{mod}.PlainItem.p_cached", "property"),
        (f"{mod}.PlainItem.p_qual_cached", "property"),
        (f"{mod}.PlainItem.p_async", "property"),
        (f"{mod}.ModelItem", "class"),
        (f"{mod}.ModelItem.p_comp", "property"),
        (f"{mod}.ModelItem.p_comp_call", "property"),
        (f"{mod}.ModelItem.p_qual_comp", "property"),
    ]
    by_q = {s.qualname: s for s in symbols}
    assert by_q[f"{mod}.PlainItem.p_plain"].decorators == ("property", "trace")


def test_f05_property_setter_deleter() -> None:
    """Fixture: tests/fixtures/ast/f05_property_setter_deleter.py."""
    path, _, symbols = _parse_fixture("f05_property_setter_deleter.py")
    mod = "f05_property_setter_deleter"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        (f"{mod}.Account", "class"),
        (f"{mod}.Account._balance", "field"),
        (f"{mod}.Account.balance", "property"),
        (f"{mod}.Account.balance.validate", "function"),
    ]
    balance = {s.qualname: s for s in symbols}[f"{mod}.Account.balance"]
    assert balance.location == _loc(path, 5, 5)
    assert balance.decorators == ("property",)


def test_f06_overload_matrix() -> None:
    """Fixture: tests/fixtures/ast/f06_overload_matrix.py."""
    path, _, symbols = _parse_fixture("f06_overload_matrix.py")
    mod = "f06_overload_matrix"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("typing", "external"),
        ("typing.overload", "external"),
        (f"{mod}.parse", "function"),
        (f"{mod}.aparse", "async_function"),
        (f"{mod}.Codec", "class"),
        (f"{mod}.Codec.encode", "method"),
        (f"{mod}.Codec.decode", "method"),
        (f"{mod}.Codec.prop", "property"),
    ]
    by_q = {s.qualname: s for s in symbols}
    assert by_q[f"{mod}.parse"].location == _loc(path, 13, 1)
    assert by_q[f"{mod}.aparse"].location == _loc(path, 21, 1)
    assert by_q[f"{mod}.Codec.encode"].location == _loc(path, 29, 5)
    assert by_q[f"{mod}.Codec.decode"].location == _loc(path, 37, 5)
    assert by_q[f"{mod}.Codec.decode"].decorators == ("classmethod",)
    assert by_q[f"{mod}.Codec.prop"].location == _loc(path, 45, 5)
    assert by_q[f"{mod}.Codec.prop"].decorators == ("property",)


# ---------------------------------------------------------------------------
# f07 - f12: Fields, Assignments, Unpacking, Constants, Vars & Type Aliases
# ---------------------------------------------------------------------------


def test_f07_class_and_init_fields() -> None:
    """Fixture: tests/fixtures/ast/f07_class_and_init_fields.py."""
    path, _, symbols = _parse_fixture("f07_class_and_init_fields.py")
    mod = "f07_class_and_init_fields"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("types.SimpleNamespace", "external"),
        ("typing.Any", "external"),
        (f"{mod}.Record", "class"),
        (f"{mod}.Record.MAX_ITEMS", "field"),
        (f"{mod}.Record.shared", "field"),
        (f"{mod}.Record.nested", "field"),
        (f"{mod}.Record.tag_a", "field"),
        (f"{mod}.Record.tag_b", "field"),
        (f"{mod}.Record.__setitem__", "method"),
        (f"{mod}.Record.__init__", "method"),
        (f"{mod}.Record.bare_typed", "field"),
        (f"{mod}.Record.first", "field"),
        (f"{mod}.Record.mid", "field"),
        (f"{mod}.Record.rest", "field"),
        (f"{mod}.Record.alpha", "field"),
        (f"{mod}.Record.beta", "field"),
        (f"{mod}.Record.p", "field"),
        (f"{mod}.Record.q", "field"),
        (f"{mod}.Record.__post_init__", "method"),
        (f"{mod}.Record.ready", "field"),
        (f"{mod}.Record.helper", "method"),
    ]
    by_q = {s.qualname: s for s in symbols}
    assert by_q[f"{mod}.Record.shared"].location == _loc(path, 7, 5)
    assert by_q[f"{mod}.Record.alpha"].location == _loc(path, 19, 10)
    assert by_q[f"{mod}.Record.beta"].location == _loc(path, 19, 23)


def test_f08_field_deduplication() -> None:
    """Fixture: tests/fixtures/ast/f08_field_deduplication.py."""
    path, _, symbols = _parse_fixture("f08_field_deduplication.py")
    mod = "f08_field_deduplication"

    assert [(s.qualname, s.kind, s.location) for s in symbols] == [
        (mod, "module", _loc(path, 1, 1)),
        (f"{mod}.Item", "class", _loc(path, 1, 1)),
        (f"{mod}.Item.x", "field", _loc(path, 2, 5)),
        (f"{mod}.Item.__init__", "method", _loc(path, 4, 5)),
        (f"{mod}.Item.y", "field", _loc(path, 7, 13)),
    ]


def test_f09_constants_and_variables() -> None:
    """Fixture: tests/fixtures/ast/f09_constants_and_variables.py."""
    _, _, symbols = _parse_fixture("f09_constants_and_variables.py")
    mod = "f09_constants_and_variables"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        (f"{mod}.UPPER_CASE", "constant"),
        (f"{mod}._PRIVATE_CONST", "constant"),
        (f"{mod}.HTTP_200_OK", "constant"),
        (f"{mod}.lower_case", "variable"),
        (f"{mod}.camelCase", "variable"),
        (f"{mod}.__all__", "variable"),
        (f"{mod}._private", "variable"),
        (f"{mod}._1", "variable"),
        (f"{mod}.__", "variable"),
    ]


def test_f10_unpacking_multiline_and_semicolons() -> None:
    """Fixture: tests/fixtures/ast/f10_unpacking_multiline_and_semicolons.py."""
    path, _, symbols = _parse_fixture("f10_unpacking_multiline_and_semicolons.py")
    mod = "f10_unpacking_multiline_and_semicolons"

    assert [(s.qualname, s.kind, s.location, s.start_line, s.end_line) for s in symbols] == [
        (mod, "module", _loc(path, 1, 1), 1, 23),
        ("types.SimpleNamespace", "external", _loc(path, 1, 19), 1, 1),
        (f"{mod}.HEAD", "constant", _loc(path, 3, 1), 3, 3),
        (f"{mod}.mid", "variable", _loc(path, 3, 8), 3, 3),
        (f"{mod}.TAIL", "constant", _loc(path, 3, 14), 3, 3),
        (f"{mod}.FIRST", "constant", _loc(path, 5, 5), 4, 10),
        (f"{mod}.second", "variable", _loc(path, 6, 5), 4, 10),
        (f"{mod}.ALPHA", "constant", _loc(path, 11, 1), 11, 11),
        (f"{mod}.beta", "variable", _loc(path, 11, 9), 11, 11),
        (f"{mod}.A", "constant", _loc(path, 12, 1), 12, 12),
        (f"{mod}.b", "variable", _loc(path, 12, 8), 12, 12),
        ("math", "external", _loc(path, 12, 22), 12, 12),
        (f"{mod}.Inline", "class", _loc(path, 15, 1), 15, 16),
        (f"{mod}.Inline.x", "field", _loc(path, 16, 5), 16, 16),
        (f"{mod}.Inline.y", "field", _loc(path, 16, 12), 16, 16),
        (f"{mod}.items", "variable", _loc(path, 19, 1), 19, 19),
        (f"{mod}.state", "variable", _loc(path, 21, 1), 21, 21),
    ]


def test_f11_type_aliases_pep613_and_quoted() -> None:
    """Fixture: tests/fixtures/ast/f11_type_aliases_pep613_and_quoted.py."""
    _, _, symbols = _parse_fixture("f11_type_aliases_pep613_and_quoted.py")
    mod = "f11_type_aliases_pep613_and_quoted"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("__future__.annotations", "external"),
        ("typing", "external"),
        ("typing.TypeAlias", "external"),
        (f"{mod}.Alpha", "type_alias"),
        (f"{mod}.Pair", "type_alias"),
        (f"{mod}.Beta", "type_alias"),
        (f"{mod}.Gamma", "type_alias"),
        (f"{mod}.Box", "class"),
        (f"{mod}.Box.not_alias", "field"),
        (f"{mod}.Box.bare_quoted", "field"),
        (f"{mod}.Box.Delta", "type_alias"),
        (f"{mod}.make_list", "function"),
    ]


def test_f12_pep695_generics_and_type_stmt() -> None:
    """Fixture: tests/fixtures/ast/f12_pep695_generics_and_type_stmt.py."""
    path, _, symbols = _parse_fixture("f12_pep695_generics_and_type_stmt.py")
    mod = "f12_pep695_generics_and_type_stmt"

    assert [(s.qualname, s.kind, s.location, s.start_line, s.end_line) for s in symbols] == [
        (mod, "module", _loc(path, 1, 1), 1, 16),
        (f"{mod}.Vector", "type_alias", _loc(path, 1, 6), 1, 1),
        (f"{mod}.Matrix", "type_alias", _loc(path, 2, 6), 2, 4),
        (f"{mod}.Holder", "class", _loc(path, 8, 1), 8, 10),
        (f"{mod}.Holder.value", "field", _loc(path, 9, 5), 9, 9),
        (f"{mod}.Holder.Nested", "type_alias", _loc(path, 10, 10), 10, 10),
        (f"{mod}.build", "function", _loc(path, 13, 1), 13, 16),
    ]


# ---------------------------------------------------------------------------
# f13 - f14: Imports, External Spans, Internal Prefixes & Scoped Imports
# ---------------------------------------------------------------------------


def test_f13_imports_spans_aliases_and_prefixes() -> None:
    """Fixture: tests/fixtures/ast/f13_imports_spans_aliases_and_prefixes.py."""
    path, _, symbols = _parse_fixture(
        "f13_imports_spans_aliases_and_prefixes.py",
        internal_packages={"cbi", "time"},
    )
    mod = "f13_imports_spans_aliases_and_prefixes"

    rows = [(s.name, s.qualname, s.kind, s.location, s.start_line, s.end_line) for s in symbols]
    assert rows == [
        (mod, mod, "module", _loc(path, 1, 1), 1, 16),
        ("os", "os", "external", _loc(path, 1, 8), 1, 1),
        ("sys", "sys", "external", _loc(path, 1, 12), 1, 1),
        ("parse", "urllib.parse", "external", _loc(path, 2, 8), 2, 2),
        ("BaseModel", "pydantic.BaseModel", "external", _loc(path, 5, 5), 5, 5),
        ("Field", "pydantic.Field", "external", _loc(path, 6, 5), 6, 6),
        ("Field", "dataclasses.Field", "external", _loc(path, 8, 25), 8, 8),
        ("timeit", "timeit", "external", _loc(path, 13, 8), 13, 13),
        ("BaseSettings", "pydantic_settings.BaseSettings", "external", _loc(path, 14, 31), 14, 14),
    ]


def test_f14_scoped_and_conditional_imports() -> None:
    """Fixture: tests/fixtures/ast/f14_scoped_and_conditional_imports.py."""
    _, _, symbols = _parse_fixture("f14_scoped_and_conditional_imports.py")
    mod = "f14_scoped_and_conditional_imports"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("typing.TYPE_CHECKING", "external"),
        ("html", "external"),
        ("sqlite3", "external"),
        ("asyncio", "external"),
        (f"{mod}.Box", "class"),
        ("uuid.UUID", "external"),
        (f"{mod}.worker", "function"),
        ("decimal", "external"),
    ]


# ---------------------------------------------------------------------------
# f15 - f20: Control Flow, Nested Scopes, Noise & Shadowing
# ---------------------------------------------------------------------------


def test_f15_module_control_flow_and_main_guard() -> None:
    """Fixture: tests/fixtures/ast/f15_module_control_flow_and_main_guard.py."""
    _, _, symbols = _parse_fixture("f15_module_control_flow_and_main_guard.py")
    mod = "f15_module_control_flow_and_main_guard"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("contextlib.nullcontext", "external"),
        (f"{mod}.COND", "constant"),
        (f"{mod}.MODE", "constant"),
        (f"{mod}.from_if", "function"),
        (f"{mod}.from_else", "function"),
        (f"{mod}.A", "constant"),
        (f"{mod}.B", "constant"),
        (f"{mod}.D", "constant"),
        (f"{mod}.C", "constant"),
        (f"{mod}.WITH_VAL", "constant"),
        (f"{mod}.MATCH_VAL", "constant"),
        (f"{mod}.KEEP_ELSE", "constant"),
        (f"{mod}.NOT_MAIN_GUARD", "constant"),
    ]


def test_f16_nested_classes_and_conditional_bodies() -> None:
    """Fixture: tests/fixtures/ast/f16_nested_classes_and_conditional_bodies.py."""
    path, _, symbols = _parse_fixture("f16_nested_classes_and_conditional_bodies.py")
    mod = "f16_nested_classes_and_conditional_bodies"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        (f"{mod}.FAST", "constant"),
        (f"{mod}.Outer", "class"),
        (f"{mod}.Outer.Meta", "class"),
        (f"{mod}.Outer.Meta.db_table", "field"),
        (f"{mod}.Outer.Meta.Rules", "class"),
        (f"{mod}.Outer.Meta.Rules.strict", "field"),
        (f"{mod}.Outer.Meta.Rules.__init__", "method"),
        (f"{mod}.Outer.Meta.Rules.active", "field"),
        (f"{mod}.Outer.Meta.Rules.ok", "property"),
        (f"{mod}.Outer.__init__", "method"),
        (f"{mod}.Outer.fast", "field"),
        (f"{mod}.Outer.run", "method"),
        (f"{mod}.Outer.slow", "field"),
    ]
    by_q = {s.qualname: s for s in symbols}
    assert by_q[f"{mod}.Outer.__init__"].location == _loc(path, 19, 9)
    assert by_q[f"{mod}.Outer.run"].location == _loc(path, 22, 9)


def test_f17_init_control_flow_and_nested_scopes() -> None:
    """Fixture: tests/fixtures/ast/f17_init_control_flow_and_nested_scopes.py."""
    _, _, symbols = _parse_fixture("f17_init_control_flow_and_nested_scopes.py")
    mod = "f17_init_control_flow_and_nested_scopes"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("contextlib.nullcontext", "external"),
        ("typing.Any", "external"),
        (f"{mod}.Complex", "class"),
        (f"{mod}.Complex.__init__", "method"),
        (f"{mod}.Complex.empty", "field"),
        (f"{mod}.Complex.nonempty", "field"),
        (f"{mod}.Complex.failed", "field"),
        (f"{mod}.Complex.__init__.inner", "function"),
        (f"{mod}.factory", "function"),
        (f"{mod}.factory.Inner", "class"),
        (f"{mod}.factory.Inner.cls_field", "field"),
        (f"{mod}.factory.Inner.__init__", "method"),
        (f"{mod}.factory.Inner.inst_field", "field"),
        (f"{mod}.factory.Inner.ping", "method"),
    ]


def test_f18_overloaded_and_non_class_dunder_init() -> None:
    """Fixture: tests/fixtures/ast/f18_overloaded_and_non_class_dunder_init.py."""
    path, _, symbols = _parse_fixture("f18_overloaded_and_non_class_dunder_init.py")
    mod = "f18_overloaded_and_non_class_dunder_init"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("typing.Any", "external"),
        ("typing.overload", "external"),
        (f"{mod}.Client", "class"),
        (f"{mod}.Client.__init__", "method"),
        (f"{mod}.Client.host", "field"),
        (f"{mod}.Client.port", "field"),
        (f"{mod}.Client.__init__.LocalHelper", "class"),
        (f"{mod}.Client.__init__.LocalHelper.__init__", "method"),
        (f"{mod}.Client.__init__.LocalHelper.helper_only", "field"),
        (f"{mod}.__init__", "function"),
        (f"{mod}.wrapper", "function"),
        (f"{mod}.wrapper.__init__", "function"),
    ]
    assert {s.qualname: s for s in symbols}[f"{mod}.Client.__init__"].location == _loc(path, 8, 5)


def test_f19_local_noise_torture() -> None:
    """Fixture: tests/fixtures/ast/f19_local_noise_torture.py."""
    _, _, symbols = _parse_fixture("f19_local_noise_torture.py")
    mod = "f19_local_noise_torture"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("collections.abc.AsyncIterable", "external"),
        ("contextlib.AbstractAsyncContextManager", "external"),
        ("contextlib.nullcontext", "external"),
        ("typing.Any", "external"),
        (f"{mod}.KEEP", "constant"),
        (f"{mod}.GLOBAL_VAR", "constant"),
        (f"{mod}.torture", "function"),
        (f"{mod}.torture.touch", "function"),
        (f"{mod}.async_torture", "async_function"),
    ]


def test_f20_rebinding_and_shadowing() -> None:
    """Fixture: tests/fixtures/ast/f20_rebinding_and_shadowing.py."""
    path, _, symbols = _parse_fixture("f20_rebinding_and_shadowing.py")
    mod = "f20_rebinding_and_shadowing"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("math.sin", "external"),
        (f"{mod}.FLAG", "constant"),
        (f"{mod}.x", "variable"),
        (f"{mod}.sin", "function"),
        (f"{mod}.job", "function"),
        ("fractions", "external"),
        (f"{mod}.job.sub", "function"),
    ]
    by_q = {s.qualname: s for s in symbols}
    assert by_q[f"{mod}.x"].location == _loc(path, 4, 1)
    assert by_q[f"{mod}.job"].location == _loc(path, 13, 5)


# ---------------------------------------------------------------------------
# f21 - f23: ast_hash Clones, Stubs & Raise Edge Cases
# ---------------------------------------------------------------------------


def test_f21_function_hash_clones_and_variants() -> None:
    """Fixture: tests/fixtures/ast/f21_function_hash_clones_and_variants.py."""
    _, _, symbols = _parse_fixture("f21_function_hash_clones_and_variants.py")
    mod = "f21_function_hash_clones_and_variants"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("collections.abc.Callable", "external"),
        ("typing.Any", "external"),
        (f"{mod}.dec_a", "function"),
        (f"{mod}.dec_b", "function"),
        (f"{mod}.alpha", "function"),
        (f"{mod}.beta", "function"),
        (f"{mod}.gamma", "async_function"),
        (f"{mod}.delta", "function"),
        (f"{mod}.eps", "function"),
        (f"{mod}.zeta", "function"),
        (f"{mod}.eta", "function"),
        (f"{mod}.theta", "function"),
        (f"{mod}.iota", "function"),
    ]
    by_q = {s.qualname: s for s in symbols}
    h_alpha = by_q[f"{mod}.alpha"].ast_hash
    assert h_alpha == _expected_hash("return x + 1")
    assert by_q[f"{mod}.beta"].ast_hash == h_alpha
    assert by_q[f"{mod}.gamma"].ast_hash == h_alpha

    for variant in ("delta", "eps", "zeta", "eta", "theta", "iota"):
        h_var = by_q[f"{mod}.{variant}"].ast_hash
        assert h_var is not None
        assert h_var != h_alpha


def test_f22_class_hash_clones_and_raises() -> None:
    """Fixture: tests/fixtures/ast/f22_class_hash_clones_and_raises.py."""
    _, _, symbols = _parse_fixture("f22_class_hash_clones_and_raises.py")
    mod = "f22_class_hash_clones_and_raises"

    assert [(s.qualname, s.kind) for s in symbols] == [
        (mod, "module"),
        ("collections.abc.Callable", "external"),
        ("dataclasses.dataclass", "external"),
        (f"{mod}.BaseA", "class"),
        (f"{mod}.BaseB", "class"),
        (f"{mod}.UserDTO", "class"),
        (f"{mod}.UserDTO.id", "field"),
        (f"{mod}.UserDTO.key", "method"),
        (f"{mod}.AccountDTO", "class"),
        (f"{mod}.AccountDTO.id", "field"),
        (f"{mod}.AccountDTO.key", "method"),
        (f"{mod}.DiffTypeDTO", "class"),
        (f"{mod}.DiffTypeDTO.id", "field"),
        (f"{mod}.DiffTypeDTO.key", "method"),
        (f"{mod}.DiffNameDTO", "class"),
        (f"{mod}.DiffNameDTO.uid", "field"),
        (f"{mod}.DiffNameDTO.key", "method"),
        (f"{mod}.make_err", "function"),
        (f"{mod}.fail_value_error", "function"),
        (f"{mod}.guarded_not_implemented", "function"),
        (f"{mod}.bare_reraise", "function"),
        (f"{mod}.dynamic_raise", "function"),
    ]
    by_q = {s.qualname: s for s in symbols}
    assert by_q[f"{mod}.BaseA"].ast_hash is None
    assert by_q[f"{mod}.BaseB"].ast_hash is None

    h_user = by_q[f"{mod}.UserDTO"].ast_hash
    assert h_user is not None
    assert by_q[f"{mod}.AccountDTO"].ast_hash == h_user
    assert by_q[f"{mod}.DiffTypeDTO"].ast_hash != h_user
    assert by_q[f"{mod}.DiffNameDTO"].ast_hash != h_user

    for fn in ("fail_value_error", "guarded_not_implemented", "bare_reraise", "dynamic_raise"):
        assert by_q[f"{mod}.{fn}"].ast_hash is not None


def test_f23_stub_bodies_and_empty_classes() -> None:
    """Fixture: tests/fixtures/ast/f23_stub_bodies_and_empty_classes.py."""
    _, _, symbols = _parse_fixture("f23_stub_bodies_and_empty_classes.py")
    mod = "f23_stub_bodies_and_empty_classes"

    assert [(s.qualname, s.kind, s.ast_hash) for s in symbols] == [
        (mod, "module", None),
        ("builtins", "external", None),
        (f"{mod}.stub_pass", "function", None),
        (f"{mod}.stub_ellipsis", "function", None),
        (f"{mod}.stub_doc_only", "function", None),
        (f"{mod}.stub_doc_pass", "function", None),
        (f"{mod}.stub_doc_ellipsis", "function", None),
        (f"{mod}.stub_pass_ellipsis", "function", None),
        (f"{mod}.stub_not_impl", "function", None),
        (f"{mod}.stub_not_impl_call", "function", None),
        (f"{mod}.stub_not_impl_msg", "function", None),
        (f"{mod}.stub_builtins_not_impl", "function", None),
        (f"{mod}.stub_doc_not_impl", "function", None),
        (f"{mod}.EmptyPass", "class", None),
        (f"{mod}.DocOnlyClass", "class", None),
        (f"{mod}.EllipsisClass", "class", None),
    ]


# ---------------------------------------------------------------------------
# f24 - f31: Boundary Inputs, Encoding/BOM, Scale & Error Propagation
# ---------------------------------------------------------------------------


def test_f24_empty_file() -> None:
    """Fixture: tests/fixtures/ast/f24_empty_file.py."""
    path, _, symbols = _parse_fixture("f24_empty_file.py")
    mod = "f24_empty_file"
    assert symbols == [SymbolNode(mod, mod, "module", _loc(path, 1, 1), 1, 1)]


def test_f25_comments_and_docstring_only() -> None:
    """Fixture: tests/fixtures/ast/f25_comments_and_docstring_only.py."""
    path, source, symbols = _parse_fixture("f25_comments_and_docstring_only.py")
    mod = "f25_comments_and_docstring_only"
    assert symbols == [SymbolNode(mod, mod, "module", _loc(path, 1, 1), 1, 4)]

    # Leading/trailing newlines without trailing code line preserve exact splitlines() count.
    assert extract_symbols(source.strip("\n"))[0].end_line == 3
    assert extract_symbols("a = 1")[0].end_line == 1
    assert extract_symbols("a = 1\n")[0].end_line == 1
    assert extract_symbols("a = 1\n\n")[0].end_line == 2


def test_f26_unicode_identifiers() -> None:
    """Fixture: tests/fixtures/ast/f26_unicode_identifiers.py."""
    path, source, symbols = _parse_fixture("f26_unicode_identifiers.py")
    mod = "f26_unicode_identifiers"

    assert [(s.name, s.qualname, s.kind, s.location) for s in symbols] == [
        (mod, mod, "module", _loc(path, 1, 1)),
        ("π", f"{mod}.π", "variable", _loc(path, 1, 1)),
        ("Données", f"{mod}.Données", "class", _loc(path, 4, 1)),
        ("valeur", f"{mod}.Données.valeur", "field", _loc(path, 5, 5)),
        ("café", f"{mod}.café", "function", _loc(path, 8, 1)),
    ]

    crlf_source = source.replace("\n", "\r\n")
    crlf_symbols = extract_symbols(crlf_source, module="pkg.uni")
    _assert_invariants(crlf_symbols, crlf_source, "<memory>", "pkg.uni")
    assert [(s.name, s.qualname, s.kind) for s in crlf_symbols] == [
        ("uni", "pkg.uni", "module"),
        ("π", "pkg.uni.π", "variable"),
        ("Données", "pkg.uni.Données", "class"),
        ("valeur", "pkg.uni.Données.valeur", "field"),
        ("café", "pkg.uni.café", "function"),
    ]


def test_f27_memory_and_self_import_collision() -> None:
    """Fixture: tests/fixtures/ast/f27_memory_and_self_import_collision.py."""
    path, source, symbols = _parse_fixture("f27_memory_and_self_import_collision.py")
    mod = "f27_memory_and_self_import_collision"

    assert [(s.name, s.qualname, s.kind, s.location) for s in symbols] == [
        (mod, mod, "module", _loc(path, 1, 1)),
        ("json", "json", "external", _loc(path, 1, 8)),
        ("json", f"{mod}.json", "function", _loc(path, 6, 1)),
    ]

    mem_symbols = extract_symbols(source)
    _assert_invariants(mem_symbols, source, "<memory>", "<module>")
    assert [(s.name, s.qualname, s.kind) for s in mem_symbols] == [
        ("<module>", "<module>", "module"),
        ("json", "json", "external"),
        ("json", "<module>.json", "function"),
    ]


def test_f28_syntax_error(tmp_path: Path) -> None:
    """Fixture: tests/fixtures/ast/f28_syntax_error.py."""
    broken = (AST_FIXTURES / "f28_syntax_error.py").resolve()
    with pytest.raises(SyntaxError) as exc_info:
        parse_file(broken, src_root=AST_FIXTURES)
    assert exc_info.value.filename == str(broken)
    assert exc_info.value.lineno == 3

    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / "does_not_exist.py", src_root=tmp_path)


def test_f29_utf8_bom() -> None:
    """Fixture: tests/fixtures/ast/f29_utf8_bom.py."""
    path, source, symbols = _parse_fixture("f29_utf8_bom.py")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    mod = "f29_utf8_bom"

    expected = [
        (mod, mod, "module"),
        ("json", "json", "external"),
        ("BOM_TAG", f"{mod}.BOM_TAG", "constant"),
        ("encode_tag", f"{mod}.encode_tag", "function"),
    ]
    assert [(s.name, s.qualname, s.kind) for s in symbols] == expected

    raw_bom_source = path.read_text(encoding="utf-8")
    assert raw_bom_source.startswith("\ufeff")
    mem_symbols = extract_symbols(raw_bom_source, filename=path, module=mod)
    _assert_invariants(mem_symbols, source, str(path), mod)
    assert mem_symbols == symbols

    bom_only = extract_symbols("\ufeff")
    _assert_invariants(bom_only, "", "<memory>", "<module>")
    assert bom_only == [SymbolNode("<module>", "<module>", "module", "<memory>:1:1", 1, 1)]


def test_f30_pep263_latin1_cookie() -> None:
    """Fixture: tests/fixtures/ast/f30_pep263_latin1_cookie.py."""
    raw_path = (AST_FIXTURES / "f30_pep263_latin1_cookie.py").resolve()
    with pytest.raises(UnicodeDecodeError):
        raw_path.read_text(encoding="utf-8")

    path, _, symbols = _parse_fixture("f30_pep263_latin1_cookie.py")
    mod = "f30_pep263_latin1_cookie"

    assert [(s.name, s.qualname, s.kind, s.location) for s in symbols] == [
        (mod, mod, "module", _loc(path, 1, 1)),
        ("decimal", "decimal", "external", _loc(path, 2, 8)),
        ("CAFE_PRICE", f"{mod}.CAFE_PRICE", "constant", _loc(path, 4, 1)),
        ("résumé", f"{mod}.résumé", "function", _loc(path, 7, 1)),
    ]
    fn = {s.qualname: s for s in symbols}[f"{mod}.résumé"]
    assert fn.ast_hash is not None
    assert len(fn.ast_hash) == 16


def test_f31_deep_elif_and_nesting() -> None:
    """Fixture: tests/fixtures/ast/f31_deep_elif_and_nesting.py."""
    path, _, symbols = _parse_fixture("f31_deep_elif_and_nesting.py")
    mod = "f31_deep_elif_and_nesting"

    assert [(s.name, s.qualname, s.kind) for s in symbols] == [
        (mod, mod, "module"),
        ("dispatch_code", f"{mod}.dispatch_code", "function"),
        ("fractions", "fractions", "external"),
        ("AFTER_CHAIN", f"{mod}.AFTER_CHAIN", "constant"),
    ]
    by_q = {s.qualname: s for s in symbols}
    dispatch = by_q[f"{mod}.dispatch_code"]
    assert dispatch.ast_hash is not None
    assert len(dispatch.ast_hash) == 16
    assert set(dispatch.ast_hash) <= _HEX_CHARS
    assert by_q["fractions"].location == _loc(path, 33, 16)
    assert by_q[f"{mod}.AFTER_CHAIN"].location == _loc(path, 38, 1)


# ---------------------------------------------------------------------------
# f32 - f36: Package Layout, Discovery, API Contract & Dogfooding
# ---------------------------------------------------------------------------


def test_f32_package_init_and_self_import() -> None:
    """Fixture: tests/fixtures/ast/sample_pkg/mypkg/__init__.py."""
    init_py = (SAMPLE_PKG / "mypkg" / "__init__.py").resolve()
    source = init_py.read_text(encoding="utf-8")

    # Even with module="" and internal_packages=(), extract_symbols marks `mypkg` internal.
    syms_direct = extract_symbols(source, filename=init_py)
    _assert_invariants(syms_direct, source, str(init_py), "mypkg")
    assert [(s.qualname, s.kind) for s in syms_direct] == [
        ("mypkg", "module"),
        ("typing.TYPE_CHECKING", "external"),
        ("mypkg.Exported", "class"),
        ("mypkg.Exported.value", "field"),
    ]

    syms_root = parse_file(init_py, src_root=SAMPLE_PKG)
    _assert_invariants(syms_root, source, str(init_py), "mypkg")
    assert syms_root == syms_direct

    assert module_name_from_path(init_py) == "mypkg"
    assert module_name_from_path(init_py, SAMPLE_PKG) == "mypkg"
    assert module_name_from_path(SAMPLE_PKG / "__init__.py", SAMPLE_PKG) == "sample_pkg"


def test_f33_package_dunder_main_and_symlinks(tmp_path: Path) -> None:
    """Fixture: tests/fixtures/ast/sample_pkg/mypkg/__main__.py."""
    main_py = (SAMPLE_PKG / "mypkg" / "__main__.py").resolve()
    source = main_py.read_text(encoding="utf-8")

    assert module_name_from_path(str(main_py), str(SAMPLE_PKG)) == "mypkg.__main__"
    syms = parse_file(str(main_py), src_root=str(SAMPLE_PKG))
    _assert_invariants(syms, source, str(main_py), "mypkg.__main__")
    assert [(s.qualname, s.kind) for s in syms] == [
        ("mypkg.__main__", "module"),
        ("mypkg.__main__.ENTRY_CODE", "constant"),
        ("mypkg.__main__.main", "function"),
    ]

    linked_pkg = tmp_path / "linked_sample_pkg"
    linked_pkg.symlink_to(SAMPLE_PKG)
    linked_syms = parse_file(linked_pkg / "mypkg" / "__main__.py", src_root=linked_pkg)
    _assert_invariants(linked_syms, source, str(main_py), "mypkg.__main__")
    assert linked_syms == syms

    with pytest.raises(ValueError):
        module_name_from_path(tmp_path / "outside.py", SAMPLE_PKG)


def test_f34_package_worker_and_discovery(tmp_path: Path) -> None:
    """Fixture: tests/fixtures/ast/sample_pkg/mypkg/worker.py."""
    worker_py = (SAMPLE_PKG / "mypkg" / "worker.py").resolve()
    source = worker_py.read_text(encoding="utf-8")

    discovered = discover_internal_packages(str(SAMPLE_PKG), SAMPLE_PKG / "does_not_exist")
    assert discovered == frozenset({"sample_pkg", "standalone", "mypkg", "ns_pkg"})
    assert "custom_venv" not in discovered
    assert discover_internal_packages(SAMPLE_PKG / "custom_venv") == frozenset()
    assert "venv" not in discover_internal_packages(REPO_ROOT)

    for skip_dir in (".tox", ".nox", "__pypackages__", "site-packages"):
        d = tmp_path / skip_dir
        d.mkdir()
        (d / "__init__.py").write_text("", encoding="utf-8")
    env311 = tmp_path / "env311"
    env311.mkdir()
    (env311 / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (env311 / "pkg.py").write_text("X = 1\n", encoding="utf-8")
    real_pkg = tmp_path / "real_pkg"
    real_pkg.mkdir()
    (real_pkg / "__init__.py").write_text("", encoding="utf-8")
    assert discover_internal_packages(tmp_path) == frozenset({"real_pkg"})

    syms = parse_file(worker_py, src_root=SAMPLE_PKG, internal_packages=discovered)
    _assert_invariants(syms, source, str(worker_py), "mypkg.worker")
    assert [(s.qualname, s.kind) for s in syms] == [
        ("mypkg.worker", "module"),
        ("pathlib", "external"),
        ("mypkg.worker.run_worker", "function"),
    ]


def test_f35_hyphenated_root_dunder_init() -> None:
    """Fixture: tests/fixtures/ast/sample_pkg/my-cool-saas/__init__.py."""
    hyphen_root = (SAMPLE_PKG / "my-cool-saas").resolve()
    hyphen_init = (hyphen_root / "__init__.py").resolve()
    source = hyphen_init.read_text(encoding="utf-8")

    assert module_name_from_path(hyphen_init, hyphen_root) == "__init__"
    assert module_name_from_path(hyphen_init) == "__init__"

    syms_root = parse_file(hyphen_init, src_root=hyphen_root)
    _assert_invariants(syms_root, source, str(hyphen_init), "__init__")
    expected = [
        ("__init__", "module"),
        ("__init__.APP_VERSION", "constant"),
        ("__init__.create_app", "function"),
    ]
    assert [(s.qualname, s.kind) for s in syms_root] == expected

    syms_direct = extract_symbols(source, filename=hyphen_init)
    _assert_invariants(syms_direct, source, str(hyphen_init), "__init__")
    assert syms_direct == syms_root


def test_f36_dogfood_ast_parser_and_api_contract() -> None:
    """Fixture: src/cbi/ast/parser.py (plus __init__.py & test_ast_parser.py)."""
    expected_exports = {
        "SymbolKind",
        "SymbolNode",
        "discover_internal_packages",
        "extract_symbols",
        "module_name_from_path",
        "parse_file",
    }
    assert set(cbi_ast.__all__) == expected_exports
    for name in expected_exports:
        assert hasattr(cbi_ast, name)

    node = SymbolNode("fn", "pkg.fn", "function", "<memory>:1:1", 1, 2)
    assert node.ast_hash is None
    assert node.decorators == ()
    assert not hasattr(node, "__dict__")
    assert len({node, SymbolNode("fn", "pkg.fn", "function", "<memory>:1:1", 1, 2)}) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, node).name = "changed"

    # Guard against orphan fixture files: f01..f31 on disk must match the 31 fixtures tested above.
    disk_fixtures = sorted(p.name for p in AST_FIXTURES.glob("f*.py"))
    assert len(disk_fixtures) == 31
    assert [name[:3] for name in disk_fixtures] == [f"f{i:02d}" for i in range(1, 32)]

    internal = discover_internal_packages(SRC_ROOT, TESTS_ROOT)
    assert "cbi" in internal
    assert "ui" not in internal
    for filepath, src_root in (
        (SRC_ROOT / "cbi" / "ast" / "parser.py", SRC_ROOT),
        (SRC_ROOT / "cbi" / "ast" / "__init__.py", SRC_ROOT),
        (REPO_ROOT / "tests" / "test_ast_parser.py", REPO_ROOT),
    ):
        source = filepath.read_text(encoding="utf-8")
        expected_mod = module_name_from_path(filepath, src_root)
        symbols = parse_file(filepath, src_root=src_root, internal_packages=internal)
        _assert_invariants(symbols, source, str(filepath.resolve()), expected_mod)
