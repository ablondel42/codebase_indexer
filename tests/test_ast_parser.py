"""Comprehensive adversarial tests for AST symbol extraction in cbi.ast."""

import ast
import dataclasses
import hashlib
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
MOCK_PATH = TESTS_ROOT / "fixtures" / "mock.py"

_CALLABLE_OR_CLASS_KINDS: frozenset[SymbolKind] = frozenset(
    {"class", "function", "async_function", "method", "async_method", "property"}
)
_HEX_CHARS: frozenset[str] = frozenset("0123456789abcdef")


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


# ---------------------------------------------------------------------------
# 1. Exact SymbolNode Contract (All 12 SymbolKinds) & Mock Fixture
# ---------------------------------------------------------------------------


def test_exact_symbol_node_contract_across_all_12_kinds() -> None:
    source = (
        "import pathlib\n"
        "Alias: TypeAlias = int\n"
        "LIMIT = 5\n"
        "count = 0\n"
        "@dec\n"
        "class Box:\n"
        "    tag: str\n"
        "    @property\n"
        "    def size(self) -> int:\n"
        "        return 1\n"
        "    async def fetch(self) -> int:\n"
        "        return 2\n"
        "    def ping(self) -> None: ...\n"
        "def helper() -> int:\n"
        "    return 3\n"
        "async def run() -> int:\n"
        "    return 4\n"
    )
    symbols = extract_symbols(source, module="pkg.core")
    _assert_invariants(symbols, source, "<memory>", "pkg.core")

    expected_box_body = (
        "tag: str\n"
        "@property\n"
        "def size(self) -> int:\n"
        "    return 1\n"
        "async def fetch(self) -> int:\n"
        "    return 2\n"
        "def ping(self) -> None: ...\n"
    )
    expected = [
        SymbolNode("core", "pkg.core", "module", "<memory>:1:1", 1, 17),
        SymbolNode("pathlib", "pathlib", "external", "<memory>:1:8", 1, 1),
        SymbolNode("Alias", "pkg.core.Alias", "type_alias", "<memory>:2:1", 2, 2),
        SymbolNode("LIMIT", "pkg.core.LIMIT", "constant", "<memory>:3:1", 3, 3),
        SymbolNode("count", "pkg.core.count", "variable", "<memory>:4:1", 4, 4),
        SymbolNode(
            "Box",
            "pkg.core.Box",
            "class",
            "<memory>:6:1",
            5,
            13,
            ast_hash=_expected_hash(expected_box_body),
            decorators=("dec",),
        ),
        SymbolNode("tag", "pkg.core.Box.tag", "field", "<memory>:7:5", 7, 7),
        SymbolNode(
            "size",
            "pkg.core.Box.size",
            "property",
            "<memory>:9:5",
            8,
            10,
            ast_hash=_expected_hash("return 1"),
            decorators=("property",),
        ),
        SymbolNode(
            "fetch",
            "pkg.core.Box.fetch",
            "async_method",
            "<memory>:11:5",
            11,
            12,
            ast_hash=_expected_hash("return 2"),
        ),
        SymbolNode(
            "ping",
            "pkg.core.Box.ping",
            "method",
            "<memory>:13:5",
            13,
            13,
            ast_hash=None,
        ),
        SymbolNode(
            "helper",
            "pkg.core.helper",
            "function",
            "<memory>:14:1",
            14,
            15,
            ast_hash=_expected_hash("return 3"),
        ),
        SymbolNode(
            "run",
            "pkg.core.run",
            "async_function",
            "<memory>:16:1",
            16,
            17,
            ast_hash=_expected_hash("return 4"),
        ),
    ]
    assert symbols == expected
    assert {sym.kind for sym in symbols} == set(get_args(SymbolKind))


def test_parse_mock_fixture_extracts_all_kinds_and_skips_locals() -> None:
    source = MOCK_PATH.read_text(encoding="utf-8")
    symbols = extract_symbols(source, filename=MOCK_PATH, module="cbi.ast.mock")
    _assert_invariants(symbols, source, str(MOCK_PATH.resolve()), "cbi.ast.mock")

    by_qualname = {sym.qualname: sym for sym in symbols}

    # Top-level and non-top-level external imports extracted cleanly.
    assert by_qualname["pydantic.BaseModel"].kind == "external"
    assert by_qualname["pydantic.Field"].kind == "external"
    assert by_qualname["pathlib"].kind == "external"
    assert by_qualname["uuid"].kind == "external"
    assert by_qualname["decimal.Decimal"].kind == "external"

    # Module-level declarations.
    assert by_qualname["cbi.ast.mock.Payload"].kind == "type_alias"
    assert by_qualname["cbi.ast.mock.DEFAULT_RETRIES"].kind == "constant"
    assert by_qualname["cbi.ast.mock.__all__"].kind == "variable"

    # Classes, dataclass/Pydantic fields, decorators, and __init__ self.<attr> fields.
    assert by_qualname["cbi.ast.mock.QuotaExceededError"].ast_hash is None
    assert by_qualname["cbi.ast.mock.TaskRecord"].decorators == ("dataclass(slots=True)",)
    assert by_qualname["cbi.ast.mock.TaskSummary"].kind == "class"
    assert by_qualname["cbi.ast.mock.TaskSummary.tag_count"].kind == "field"
    assert by_qualname["cbi.ast.mock.TaskWorker.root"].kind == "field"
    assert by_qualname["cbi.ast.mock.TaskWorker.max_retries"].kind == "field"
    assert by_qualname["cbi.ast.mock.TaskWorker._processed"].kind == "field"

    # Callables, properties, async methods/functions, and nested functions.
    assert by_qualname["cbi.ast.mock.TaskWorker.is_ready"].kind == "property"
    assert by_qualname["cbi.ast.mock.TaskWorker.process"].kind == "method"
    assert by_qualname["cbi.ast.mock.TaskWorker.process_async"].kind == "async_method"
    assert by_qualname["cbi.ast.mock.route.register"].kind == "function"
    assert by_qualname["cbi.ast.mock.run_batch.format_error"].kind == "function"

    run_batch = by_qualname["cbi.ast.mock.run_batch"]
    assert run_batch.kind == "async_function"
    assert run_batch.decorators == ("route('/tasks/batch')",)
    assert run_batch.start_line == int(run_batch.location.rsplit(":", 2)[1]) - 1

    # Local variables inside functions/methods must never leak.
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
    assert "cbi.ast.mock.TaskWorker.process.tag_count" not in by_qualname
    assert "cbi.ast.mock.tag_count" not in by_qualname

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
# 2. Classes, Methods, Properties, Decorators & @overload
# ---------------------------------------------------------------------------


def test_stacked_decorators_and_classmethod_staticmethod_sync_async() -> None:
    source = (
        "@first\n"
        "@second(flag=True)\n"
        "class Service:\n"
        "    @classmethod\n"
        "    def make(cls) -> int:\n"
        "        return 1\n"
        "    @staticmethod\n"
        "    async def check(x: int) -> int:\n"
        "        return x\n"
    )
    symbols = extract_symbols(source, module="pkg.svc")
    _assert_invariants(symbols, source, "<memory>", "pkg.svc")
    by_q = {s.qualname: s for s in symbols}

    svc = by_q["pkg.svc.Service"]
    assert svc.kind == "class"
    assert svc.decorators == ("first", "second(flag=True)")
    assert svc.start_line == 1
    assert svc.location == "<memory>:3:1"
    assert svc.end_line == 9

    make = by_q["pkg.svc.Service.make"]
    assert make.kind == "method"
    assert make.decorators == ("classmethod",)

    check = by_q["pkg.svc.Service.check"]
    assert check.kind == "async_method"
    assert check.decorators == ("staticmethod",)


@pytest.mark.parametrize(
    "decorator_line",
    [
        "@property",
        "@cached_property",
        "@functools.cached_property",
        "@computed_field",
        "@computed_field()",
        "@pydantic.computed_field(return_type=int)",
    ],
)
def test_property_decorator_variants_and_module_scope_boundary(decorator_line: str) -> None:
    source = (
        "@property\n"
        "def top_fn() -> int:\n"
        "    return 0\n\n"
        "class Item:\n"
        f"    {decorator_line}\n"
        "    @trace\n"
        "    def value(self) -> int:\n"
        "        return 1\n"
    )
    symbols = extract_symbols(source, module="pkg.props")
    _assert_invariants(symbols, source, "<memory>", "pkg.props")
    by_q = {s.qualname: s for s in symbols}

    # @property at module scope is still a regular function.
    assert by_q["pkg.props.top_fn"].kind == "function"

    prop = by_q["pkg.props.Item.value"]
    assert prop.kind == "property"
    assert prop.start_line == 6
    assert prop.location == "<memory>:8:5"
    assert len(prop.decorators) == 2


def test_property_setter_deleter_deduplicates_while_keeping_nested_helpers() -> None:
    source = (
        "class Account:\n"
        "    @property\n"
        "    def balance(self) -> int:\n"
        "        return 0\n"
        "    @balance.setter\n"
        "    def balance(self, v: int) -> None:\n"
        "        def validate(raw: int) -> int:\n"
        "            return raw\n"
        "        _ = validate(v)\n"
        "    @balance.deleter\n"
        "    def balance(self) -> None:\n"
        "        pass\n"
    )
    symbols = extract_symbols(source, module="pkg.acct")
    _assert_invariants(symbols, source, "<memory>", "pkg.acct")
    by_q = {s.qualname: s for s in symbols}

    assert [s.qualname for s in symbols].count("pkg.acct.Account.balance") == 1
    assert by_q["pkg.acct.Account.balance"].kind == "property"
    assert by_q["pkg.acct.Account.balance"].location == "<memory>:3:5"
    assert by_q["pkg.acct.Account.balance.validate"].kind == "function"


def test_overload_stubs_skipped_across_sync_async_and_methods() -> None:
    source = (
        "import typing\n"
        "from typing import overload\n\n"
        "@overload\n"
        "def parse(x: str) -> int: ...\n"
        "@typing.overload\n"
        "def parse(x: bytes) -> int: ...\n"
        "def parse(x: str | bytes) -> int:\n"
        "    return len(x)\n\n"
        "@overload\n"
        "async def aparse(x: str) -> int: ...\n"
        "async def aparse(x: str) -> int:\n"
        "    return len(x)\n\n"
        "class Codec:\n"
        "    @classmethod\n"
        "    @overload\n"
        "    def decode(cls, x: str) -> int: ...\n"
        "    @classmethod\n"
        "    def decode(cls, x: str) -> int:\n"
        "        return len(x)\n"
    )
    symbols = extract_symbols(source, module="pkg.codec")
    _assert_invariants(symbols, source, "<memory>", "pkg.codec")
    by_q = {s.qualname: s for s in symbols}

    parse = by_q["pkg.codec.parse"]
    assert parse.kind == "function"
    assert parse.location == "<memory>:8:1"
    assert parse.decorators == ()
    assert parse.ast_hash is not None

    aparse = by_q["pkg.codec.aparse"]
    assert aparse.kind == "async_function"
    assert aparse.location == "<memory>:13:1"
    assert aparse.ast_hash is not None

    decode = by_q["pkg.codec.Codec.decode"]
    assert decode.kind == "method"
    assert decode.location == "<memory>:21:5"
    assert decode.decorators == ("classmethod",)
    assert decode.ast_hash is not None


# ---------------------------------------------------------------------------
# 3. Fields, Assignments, Unpacking & Type Aliases
# ---------------------------------------------------------------------------


def test_class_fields_init_post_init_unpacking_and_rejections() -> None:
    source = (
        "class Record:\n"
        "    MAX_ITEMS = 10\n"
        "    shared: str\n"
        "    tag_a, tag_b = 'a', 'b'\n"
        "    def __init__(self, other: object) -> None:\n"
        "        self.shared = 'override'\n"
        "        self.bare_typed: int\n"
        "        self.first, (self.mid, *self.rest), self._ = [1, [2, 3], 4]\n"
        "        self.p = self.q = 0\n"
        "        other.not_ours = 1\n"
        "        self.nested.not_ours = 2\n"
        "        self.bare_typed += 1\n"
        "        self[0] = 3\n"
        "    def __post_init__(self) -> None:\n"
        "        self.ready = True\n"
        "    def helper(self) -> None:\n"
        "        self.late_attr = 99\n"
    )
    symbols = extract_symbols(source, module="pkg.rec")
    _assert_invariants(symbols, source, "<memory>", "pkg.rec")

    fields = {s.name: s for s in symbols if s.kind == "field"}
    assert set(fields) == {
        "MAX_ITEMS",
        "shared",
        "tag_a",
        "tag_b",
        "bare_typed",
        "first",
        "mid",
        "rest",
        "p",
        "q",
        "ready",
    }
    # Class-level UPPER_CASE stays 'field' (not 'constant'), and class line wins over __init__.
    assert fields["MAX_ITEMS"].kind == "field"
    assert fields["shared"].location == "<memory>:3:5"
    assert "not_ours" not in fields
    assert "late_attr" not in fields


def test_deduplicates_fields_declared_in_class_and_conditional_init() -> None:
    source = (
        "class Item:\n"
        "    x: int\n"
        "    def __init__(self, flag: bool) -> None:\n"
        "        self.x = 1\n"
        "        if flag:\n"
        "            self.y = 2\n"
        "        else:\n"
        "            self.y = 3\n"
    )
    symbols = extract_symbols(source, module="pkg.item")
    _assert_invariants(symbols, source, "<memory>", "pkg.item")
    assert [(sym.kind, sym.qualname) for sym in symbols] == [
        ("module", "pkg.item"),
        ("class", "pkg.item.Item"),
        ("field", "pkg.item.Item.x"),
        ("method", "pkg.item.Item.__init__"),
        ("field", "pkg.item.Item.y"),
    ]


@pytest.mark.parametrize(
    ("name", "expected_kind"),
    [
        ("UPPER_CASE", "constant"),
        ("_PRIVATE_CONST", "constant"),
        ("HTTP_200_OK", "constant"),
        ("lower_case", "variable"),
        ("camelCase", "variable"),
        ("__all__", "variable"),
        ("_private", "variable"),
        ("_1", "variable"),
        ("__", "variable"),
    ],
)
def test_module_constant_vs_variable_classification(
    name: str, expected_kind: SymbolKind
) -> None:
    source = f"{name} = 1\n"
    symbols = extract_symbols(source, module="pkg.vars")
    _assert_invariants(symbols, source, "<memory>", "pkg.vars")
    assert symbols[1].name == name
    assert symbols[1].kind == expected_kind


def test_module_unpacking_chained_and_ignores_non_name_targets() -> None:
    source = (
        "HEAD, (mid, *TAIL), _ = [1, [2, 3, 4], 5]\n"
        "ALPHA = beta = 42\n"
        "items[0] = 1\n"
        "state.flag = True\n"
    )
    symbols = extract_symbols(source, module="pkg.unpack")
    _assert_invariants(symbols, source, "<memory>", "pkg.unpack")

    extracted = [(s.name, s.kind, s.location) for s in symbols[1:]]
    assert extracted == [
        ("HEAD", "constant", "<memory>:1:1"),
        ("mid", "variable", "<memory>:1:8"),
        ("TAIL", "constant", "<memory>:1:14"),
        ("ALPHA", "constant", "<memory>:2:1"),
        ("beta", "variable", "<memory>:2:9"),
    ]


def test_type_aliases_pep613_pep695_and_bare_annotation() -> None:
    source = (
        "from typing import TypeAlias\n"
        "import typing\n"
        "Payload: TypeAlias = dict[str, int]\n"
        "Pair: typing.TypeAlias = tuple[int, int]\n"
        "type Vector[T] = list[T]\n"
        "class Holder[T]:\n"
        "    kind: TypeAlias\n"
        "    Entry: TypeAlias = tuple[str, T]\n"
        "    type Nested = set[T]\n"
        "def build[T](val: T) -> list[T]:\n"
        "    type Local = list[T]\n"
        "    LocalTwo: TypeAlias = int\n"
        "    return [val]\n"
    )
    symbols = extract_symbols(source, module="pkg.types")
    _assert_invariants(symbols, source, "<memory>", "pkg.types")
    by_q = {s.qualname: s for s in symbols}

    assert by_q["pkg.types.Payload"].kind == "type_alias"
    assert by_q["pkg.types.Pair"].kind == "type_alias"
    assert by_q["pkg.types.Vector"].kind == "type_alias"
    assert by_q["pkg.types.Holder"].kind == "class"
    # Bare `kind: TypeAlias` without `= ...` is a field, not a TypeAlias.
    assert by_q["pkg.types.Holder.kind"].kind == "field"
    assert by_q["pkg.types.Holder.Entry"].kind == "type_alias"
    assert by_q["pkg.types.Holder.Nested"].kind == "type_alias"
    assert by_q["pkg.types.build"].kind == "function"
    # Function-local type aliases must not leak.
    assert "Local" not in {s.name for s in symbols}
    assert "LocalTwo" not in {s.name for s in symbols}


# ---------------------------------------------------------------------------
# 4. Imports & External Symbol Filtering
# ---------------------------------------------------------------------------


def test_imports_external_spans_aliases_dedup_and_prefix_safety() -> None:
    source = (
        "import os, sys\n"
        "import urllib.parse as urlparse\n"
        "from urllib import parse\n"
        "from pydantic import (\n"
        "    BaseModel,\n"
        "    Field as PydanticField,\n"
        ")\n"
        "from dataclasses import Field\n"
        "from . import local_mod\n"
        "from ..parent import secret\n"
        "from math import *\n"
        "import cbi\n"
        "from cbi.ast import parse_file\n"
        "import cbi_extra\n"
        "from cbi_tools import runner\n"
    )
    symbols = extract_symbols(
        source, module="cbi.mod", internal_packages={"cbi"}
    )
    _assert_invariants(symbols, source, "<memory>", "cbi.mod")

    externals = [s for s in symbols if s.kind == "external"]
    assert [(s.name, s.qualname, s.location, s.start_line, s.end_line) for s in externals] == [
        ("os", "os", "<memory>:1:8", 1, 1),
        ("sys", "sys", "<memory>:1:12", 1, 1),
        ("parse", "urllib.parse", "<memory>:2:8", 2, 2),
        ("BaseModel", "pydantic.BaseModel", "<memory>:5:5", 5, 5),
        ("Field", "pydantic.Field", "<memory>:6:5", 6, 6),
        ("Field", "dataclasses.Field", "<memory>:8:25", 8, 8),
        ("cbi_extra", "cbi_extra", "<memory>:14:8", 14, 14),
        ("runner", "cbi_tools.runner", "<memory>:15:23", 15, 15),
    ]


def test_non_top_level_and_conditional_imports_vs_main_guard() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import rich\n"
        "try:\n"
        "    import uvloop\n"
        "except ImportError:\n"
        "    import asyncio\n"
        "class Box:\n"
        "    from uuid import UUID\n"
        "def worker() -> None:\n"
        "    import httpx\n"
        'if __name__ == "__main__":\n'
        "    import argparse\n"
    )
    symbols = extract_symbols(source, module="pkg.imp")
    _assert_invariants(symbols, source, "<memory>", "pkg.imp")
    externals = {s.qualname for s in symbols if s.kind == "external"}

    assert externals == {
        "typing.TYPE_CHECKING",
        "rich",
        "uvloop",
        "asyncio",
        "uuid.UUID",
        "httpx",
    }
    assert "argparse" not in externals


def test_multi_root_internal_packages_not_marked_external() -> None:
    internal = discover_internal_packages(SRC_ROOT, TESTS_ROOT)
    assert "cbi" in internal
    assert "ui" not in internal

    this_file = Path(__file__).resolve()
    source = this_file.read_text(encoding="utf-8")
    symbols = parse_file(this_file, src_root=REPO_ROOT, internal_packages=internal)
    _assert_invariants(symbols, source, str(this_file), "tests.test_ast_parser")

    externals = {sym.qualname for sym in symbols if sym.kind == "external"}
    assert "pathlib.Path" in externals
    assert not any(q.startswith("cbi.") for q in externals)


# ---------------------------------------------------------------------------
# 5. Scope Boundaries, Control Flow & Local-Noise Torture
# ---------------------------------------------------------------------------


def test_module_control_flow_and_main_guard_branches() -> None:
    source = (
        "if COND:\n"
        "    def from_if() -> None: pass\n"
        "else:\n"
        "    def from_else() -> None: pass\n"
        "try:\n"
        "    A = 1\n"
        "except* ValueError as err:\n"
        "    B = 2\n"
        "finally:\n"
        "    C = 3\n"
        "with ctx() as handle:\n"
        "    WITH_VAL = 4\n"
        "match MODE:\n"
        "    case captured:\n"
        "        MATCH_VAL = 5\n"
        'if "__main__" == __name__:\n'
        "    skipped_main = 1\n"
        "    def skipped_fn() -> None: pass\n"
        "else:\n"
        "    KEEP_ELSE = 6\n"
        'if __name__ != "__main__":\n'
        "    NOT_MAIN_GUARD = 7\n"
    )
    symbols = extract_symbols(source, module="pkg.cfg")
    _assert_invariants(symbols, source, "<memory>", "pkg.cfg")
    names = {s.name for s in symbols}

    assert {
        "from_if",
        "from_else",
        "A",
        "B",
        "C",
        "WITH_VAL",
        "MATCH_VAL",
        "KEEP_ELSE",
        "NOT_MAIN_GUARD",
    } <= names
    assert {"err", "handle", "captured", "skipped_main", "skipped_fn"}.isdisjoint(names)


def test_init_control_flow_fields_nested_helpers_and_nested_class_in_function() -> None:
    source = (
        "class Complex:\n"
        "    def __init__(self, items: list[int]) -> None:\n"
        "        try:\n"
        "            with ctx():\n"
        "                match len(items):\n"
        "                    case 0:\n"
        "                        self.empty = True\n"
        "                    case _:\n"
        "                        for _i in items:\n"
        "                            self.nonempty = True\n"
        "        except RuntimeError:\n"
        "            self.failed = True\n"
        "        def inner(self: object) -> None:\n"
        "            self.fake_nested = 1\n"
        "def factory() -> object:\n"
        "    class Inner:\n"
        "        cls_field = 1\n"
        "        def __init__(self) -> None:\n"
        "            self.inst_field = 2\n"
        "        def ping(self) -> int:\n"
        "            return self.inst_field\n"
        "    return Inner()\n"
    )
    symbols = extract_symbols(source, module="pkg.nested")
    _assert_invariants(symbols, source, "<memory>", "pkg.nested")
    by_q = {s.qualname: s for s in symbols}

    assert by_q["pkg.nested.Complex.empty"].kind == "field"
    assert by_q["pkg.nested.Complex.nonempty"].kind == "field"
    assert by_q["pkg.nested.Complex.failed"].kind == "field"
    assert by_q["pkg.nested.Complex.__init__.inner"].kind == "function"
    assert "fake_nested" not in {s.name for s in symbols}

    assert by_q["pkg.nested.factory.Inner"].kind == "class"
    assert by_q["pkg.nested.factory.Inner.cls_field"].kind == "field"
    assert by_q["pkg.nested.factory.Inner.__init__"].kind == "method"
    assert by_q["pkg.nested.factory.Inner.inst_field"].kind == "field"
    assert by_q["pkg.nested.factory.Inner.ping"].kind == "method"


def test_function_local_noise_torture_and_underscores() -> None:
    source = (
        "_ = 1\n"
        "_, KEEP = 1, 2\n"
        "def torture(items: list[int], other: object) -> int:\n"
        "    global GLOBAL_VAR\n"
        "    nonlocal NONLOCAL_VAR\n"
        "    local_a = 1\n"
        "    local_b: int = 2\n"
        "    local_a += 1\n"
        "    head, *tail = items\n"
        "    other.attr = 3\n"
        "    squares = [sq * 2 for sq in items if (walrus := sq) > 0]\n"
        "    unique = {u for u in squares}\n"
        "    mapped = {k: v for k, v in enumerate(unique)}\n"
        "    gen = sum(g for g in mapped)\n"
        "    for loop_i in items:\n"
        "        pass\n"
        "    with ctx() as ctx_res:\n"
        "        pass\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc_var:\n"
        "        pass\n"
        "    match items:\n"
        "        case [m_first, *m_rest]:\n"
        "            pass\n"
        "    fn = lambda lam_arg: lam_arg + gen\n"
        "    return fn(walrus)\n"
        "async def async_torture(aiter: object) -> None:\n"
        "    async for a_item in aiter:\n"
        "        async with aiter as a_ctx:\n"
        "            pass\n"
    )
    symbols = extract_symbols(source, module="pkg.noise")
    _assert_invariants(symbols, source, "<memory>", "pkg.noise")
    assert [(s.name, s.kind) for s in symbols] == [
        ("noise", "module"),
        ("KEEP", "constant"),
        ("torture", "function"),
        ("async_torture", "async_function"),
    ]


# ---------------------------------------------------------------------------
# 6. ast_hash Clone & Stub Semantics
# ---------------------------------------------------------------------------


def test_ast_hash_clone_equivalences_and_sensitivities() -> None:
    baseline = (
        '@dec_a\n'
        'def alpha(x: int) -> int:\n'
        '    """Doc A."""\n'
        '    # comment\n'
        '    return x + 1\n'
    )
    renamed_and_reformatted = (
        "@dec_b(flag=True)\n"
        "def beta(\n"
        "    x: float,\n"
        ") -> float:\n"
        "    return (\n"
        "        x\n"
        "        + 1\n"
        "    )\n"
    )
    async_same_body = "async def gamma(x: int) -> int:\n    return x + 1\n"

    renamed_local = "def delta(y: int) -> int:\n    return y + 1\n"
    changed_op = "def eps(x: int) -> int:\n    return x - 1\n"
    return_string = 'def zeta() -> str:\n    return "doc"\n'
    leading_fstring = 'def eta(x: int) -> int:\n    f"{x}"\n    return x + 1\n'
    leading_bytes = 'def theta(x: int) -> int:\n    b"raw"\n    return x + 1\n'
    two_strings = 'def iota(x: int) -> int:\n    """Doc."""\n    "second"\n    return x + 1\n'

    h_base = extract_symbols(baseline)[1].ast_hash
    h_reformatted = extract_symbols(renamed_and_reformatted)[1].ast_hash
    h_async = extract_symbols(async_same_body)[1].ast_hash

    assert h_base is not None
    assert h_base == h_reformatted
    assert h_base == h_async

    for variant in (
        renamed_local,
        changed_op,
        return_string,
        leading_fstring,
        leading_bytes,
        two_strings,
    ):
        h_variant = extract_symbols(variant)[1].ast_hash
        assert h_variant is not None
        assert h_variant != h_base


@pytest.mark.parametrize(
    "source",
    [
        "def f() -> None:\n    pass\n",
        "def f() -> None:\n    ...\n",
        'def f() -> None:\n    """Only a docstring."""\n',
        'def f() -> None:\n    """Doc."""\n    pass\n',
        'def f() -> None:\n    """Doc."""\n    ...\n',
        "def f() -> None:\n    pass\n    ...\n",
        "def f() -> None:\n    raise NotImplementedError\n",
        "def f() -> None:\n    raise NotImplementedError()\n",
        'def f() -> None:\n    raise NotImplementedError("override me")\n',
        'def f() -> None:\n    raise builtins.NotImplementedError("override")\n',
        'def f() -> None:\n    """Doc."""\n    raise NotImplementedError\n',
        "class Empty:\n    pass\n",
        'class DocOnly:\n    """Class doc."""\n',
        "class EllipsisClass:\n    ...\n",
    ],
)
def test_stub_bodies_and_empty_classes_produce_none_ast_hash(source: str) -> None:
    symbols = extract_symbols(source, module="pkg.stubs")
    _assert_invariants(symbols, source, "<memory>", "pkg.stubs")
    assert symbols[1].ast_hash is None


def test_non_trivial_raises_and_conditional_not_implemented_are_hashed() -> None:
    other_raise = 'def fail() -> None:\n    raise ValueError("bad")\n'
    guarded_stub = (
        "def maybe(flag: bool) -> int:\n"
        "    if flag:\n"
        "        raise NotImplementedError\n"
        "    return 1\n"
    )
    assert extract_symbols(other_raise)[1].ast_hash is not None
    assert extract_symbols(guarded_stub)[1].ast_hash is not None


# ---------------------------------------------------------------------------
# 7. Filesystem, Module Naming, Boundary Inputs & Error Propagation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "",
        "   \n\n\t\n",
        "# just a comment\n# second line\n",
        '"""Module docstring only."""\n',
    ],
)
def test_empty_and_comment_only_sources_emit_only_module_node(source: str) -> None:
    symbols = extract_symbols(source, module="pkg.blank")
    _assert_invariants(symbols, source, "<memory>", "pkg.blank")
    assert len(symbols) == 1
    assert symbols[0].kind == "module"


def test_crlf_line_endings_and_unicode_identifiers() -> None:
    source = 'π = 3.14\r\ndef café() -> float:\r\n    return π\r\n'
    symbols = extract_symbols(source, module="pkg.uni")
    _assert_invariants(symbols, source, "<memory>", "pkg.uni")
    assert [(s.name, s.qualname, s.kind) for s in symbols] == [
        ("uni", "pkg.uni", "module"),
        ("π", "pkg.uni.π", "variable"),
        ("café", "pkg.uni.café", "function"),
    ]


def test_memory_scope_prevents_external_vs_function_collision() -> None:
    source = "import json\ndef json() -> int:\n    return 1\n"
    symbols = extract_symbols(source)
    _assert_invariants(symbols, source, "<memory>", "<module>")
    assert [(s.name, s.qualname, s.kind) for s in symbols] == [
        ("<module>", "<module>", "module"),
        ("json", "json", "external"),
        ("json", "<module>.json", "function"),
    ]


def test_module_name_from_path_variants_and_outside_root_error(tmp_path: Path) -> None:
    src = tmp_path / "src"
    pkg = src / "cbi" / "sub"
    pkg.mkdir(parents=True)
    mod_file = pkg / "worker.py"
    init_file = pkg / "__init__.py"
    root_init = src / "__init__.py"
    outside = tmp_path / "other" / "stray.py"
    outside.parent.mkdir()

    for path in (mod_file, init_file, root_init, outside):
        path.write_text("X = 1\n", encoding="utf-8")

    assert module_name_from_path(mod_file) == "worker"
    assert module_name_from_path(init_file) == "sub"
    assert module_name_from_path(mod_file, src) == "cbi.sub.worker"
    assert module_name_from_path(init_file, src) == "cbi.sub"
    assert module_name_from_path(root_init, src) == "src"

    with pytest.raises(ValueError):
        module_name_from_path(outside, src)


def test_parse_file_init_py_and_errors(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "cbi" / "sub"
    pkg_dir.mkdir(parents=True)
    init_file = pkg_dir / "__init__.py"
    init_content = "class Exported:\n    value: int = 1\n"
    init_file.write_text(init_content, encoding="utf-8")

    symbols = parse_file(init_file, src_root=tmp_path)
    _assert_invariants(symbols, init_content, str(init_file.resolve()), "cbi.sub")
    assert [(sym.kind, sym.qualname) for sym in symbols] == [
        ("module", "cbi.sub"),
        ("class", "cbi.sub.Exported"),
        ("field", "cbi.sub.Exported.value"),
    ]

    broken = tmp_path / "broken.py"
    broken.write_text("x = 1\ndef oops(:\n", encoding="utf-8")
    with pytest.raises(SyntaxError) as exc_info:
        parse_file(broken, src_root=tmp_path)
    assert exc_info.value.filename == str(broken.resolve())
    assert exc_info.value.lineno == 2

    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / "missing.py", src_root=tmp_path)


def test_discover_internal_packages_filters_non_packages_and_non_identifiers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "standalone.py").write_text("", encoding="utf-8")
    (root / "bad.name.py").write_text("", encoding="utf-8")

    (root / "regular_pkg").mkdir()
    (root / "regular_pkg" / "__init__.py").write_text("", encoding="utf-8")

    (root / "ns_pkg").mkdir()
    (root / "ns_pkg" / "mod.py").write_text("", encoding="utf-8")

    for ignored_dir in (
        ".hidden",
        "__pycache__",
        "venv",
        ".venv",
        "node_modules",
        "build",
        "dist",
        "my-tools",
        "ui",
    ):
        d = root / ignored_dir
        d.mkdir()
        if ignored_dir == "ui":
            (d / "App.tsx").write_text("", encoding="utf-8")
        else:
            (d / "mod.py").write_text("", encoding="utf-8")

    discovered = discover_internal_packages(root, tmp_path / "does_not_exist")
    assert discovered == frozenset({"workspace", "standalone", "regular_pkg", "ns_pkg"})


# ---------------------------------------------------------------------------
# 8. Dogfooding Real Repository Files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filepath", "src_root"),
    [
        (SRC_ROOT / "cbi" / "ast" / "parser.py", SRC_ROOT),
        (SRC_ROOT / "cbi" / "ast" / "__init__.py", SRC_ROOT),
        (REPO_ROOT / "tests" / "test_ast_parser.py", REPO_ROOT),
    ],
)
def test_dogfood_real_repository_files(filepath: Path, src_root: Path) -> None:
    source = filepath.read_text(encoding="utf-8")
    internal = discover_internal_packages(SRC_ROOT, TESTS_ROOT)
    expected_mod = module_name_from_path(filepath, src_root)
    symbols = parse_file(filepath, src_root=src_root, internal_packages=internal)
    _assert_invariants(symbols, source, str(filepath.resolve()), expected_mod)


# ---------------------------------------------------------------------------
# 9. Second-Pass Adversarial Edge Cases
# ---------------------------------------------------------------------------


def test_public_api_exports_and_symbol_node_dataclass_contract() -> None:
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


def test_pep614_arbitrary_decorator_expressions_and_multiline_decorators() -> None:
    source = (
        "@decorators[0]\n"
        "@(dec_a if FLAG else dec_b)\n"
        '@registry["api"].guard(\n'
        "    level=2,\n"
        ")\n"
        "class Endpoint:\n"
        "    pass\n"
        "@(lambda fn: fn)\n"
        "@app.post(\n"
        '    "/v1/items",\n'
        "    status_code=201,\n"
        ")\n"
        "async def create_item() -> int:\n"
        "    return 1\n"
    )
    symbols = extract_symbols(source, module="pkg.pep614")
    _assert_invariants(symbols, source, "<memory>", "pkg.pep614")
    by_q = {s.qualname: s for s in symbols}

    endpoint = by_q["pkg.pep614.Endpoint"]
    assert endpoint.start_line == 1
    assert endpoint.location == "<memory>:6:1"
    assert endpoint.end_line == 7
    assert endpoint.decorators == (
        "decorators[0]",
        "dec_a if FLAG else dec_b",
        "registry['api'].guard(level=2)",
    )
    assert all("\n" not in dec for dec in endpoint.decorators)

    create_item = by_q["pkg.pep614.create_item"]
    assert create_item.kind == "async_function"
    assert create_item.start_line == 8
    assert create_item.location == "<memory>:13:1"
    assert create_item.end_line == 14
    assert create_item.decorators == ("lambda fn: fn", "app.post('/v1/items', status_code=201)")
    assert all("\n" not in dec for dec in create_item.decorators)


def test_async_property_and_overloaded_property_edge_cases() -> None:
    source = (
        "from functools import cached_property\n"
        "from typing import overload\n\n"
        "class Stream:\n"
        "    @cached_property\n"
        "    async def data(self) -> int:\n"
        "        return 1\n"
        "    @overload\n"
        "    @property\n"
        "    def prop(self) -> int: ...\n"
        "    @property\n"
        "    def prop(self) -> int:\n"
        "        return 2\n"
    )
    symbols = extract_symbols(source, module="pkg.prop_edge")
    _assert_invariants(symbols, source, "<memory>", "pkg.prop_edge")
    by_q = {s.qualname: s for s in symbols}

    data = by_q["pkg.prop_edge.Stream.data"]
    assert data.kind == "property"
    assert data.location == "<memory>:6:5"

    prop = by_q["pkg.prop_edge.Stream.prop"]
    assert prop.kind == "property"
    assert prop.start_line == 11
    assert prop.location == "<memory>:12:5"
    assert prop.ast_hash is not None


def test_nested_classes_inside_classes_and_conditional_class_bodies() -> None:
    source = (
        "class Outer:\n"
        "    class Meta:\n"
        '        db_table = "t"\n'
        "        class Rules:\n"
        "            strict: bool = True\n"
        "            def __init__(self) -> None:\n"
        "                self.active = True\n"
        "            @property\n"
        "            def ok(self) -> bool:\n"
        "                return self.active\n"
        "    if FAST:\n"
        "        def __init__(self) -> None:\n"
        "            self.fast = True\n"
        "        def run(self) -> int:\n"
        "            return 1\n"
        "    else:\n"
        "        def __init__(self) -> None:\n"
        "            self.slow = True\n"
        "        def run(self) -> int:\n"
        "            return 2\n"
    )
    symbols = extract_symbols(source, module="pkg.cls_edge")
    _assert_invariants(symbols, source, "<memory>", "pkg.cls_edge")
    by_q = {s.qualname: s for s in symbols}

    assert by_q["pkg.cls_edge.Outer.Meta"].kind == "class"
    assert by_q["pkg.cls_edge.Outer.Meta.db_table"].kind == "field"
    assert by_q["pkg.cls_edge.Outer.Meta.Rules"].kind == "class"
    assert by_q["pkg.cls_edge.Outer.Meta.Rules.strict"].kind == "field"
    assert by_q["pkg.cls_edge.Outer.Meta.Rules.active"].kind == "field"
    assert by_q["pkg.cls_edge.Outer.Meta.Rules.ok"].kind == "property"

    # First branch __init__/run win location while fields from both branches are captured.
    assert by_q["pkg.cls_edge.Outer.__init__"].location == "<memory>:12:9"
    assert by_q["pkg.cls_edge.Outer.run"].location == "<memory>:14:9"
    assert by_q["pkg.cls_edge.Outer.fast"].kind == "field"
    assert by_q["pkg.cls_edge.Outer.slow"].kind == "field"


def test_overloaded_init_nested_class_in_init_and_non_class_init_functions() -> None:
    source = (
        "from typing import overload\n\n"
        "class Client:\n"
        "    @overload\n"
        "    def __init__(self, host: str) -> None: ...\n"
        "    def __init__(self, host: str, port: int = 80) -> None:\n"
        "        self.host = host\n"
        "        self.port = port\n"
        "        class LocalHelper:\n"
        "            def __init__(self) -> None:\n"
        "                self.helper_only = 1\n"
        "def __init__(self: object) -> None:\n"
        "    self.leaked_top = 1\n"
        "def wrapper() -> None:\n"
        "    def __init__(self: object) -> None:\n"
        "        self.leaked_nested = 1\n"
    )
    symbols = extract_symbols(source, module="pkg.init_edge")
    _assert_invariants(symbols, source, "<memory>", "pkg.init_edge")
    by_q = {s.qualname: s for s in symbols}

    assert by_q["pkg.init_edge.Client.__init__"].location == "<memory>:6:5"
    assert by_q["pkg.init_edge.Client.host"].kind == "field"
    assert by_q["pkg.init_edge.Client.port"].kind == "field"
    assert by_q["pkg.init_edge.Client.__init__.LocalHelper.helper_only"].kind == "field"
    assert "pkg.init_edge.Client.helper_only" not in by_q

    assert by_q["pkg.init_edge.__init__"].kind == "function"
    assert by_q["pkg.init_edge.wrapper.__init__"].kind == "function"
    assert "leaked_top" not in {s.name for s in symbols}
    assert "leaked_nested" not in {s.name for s in symbols}


def test_multiline_statements_semicolons_and_init_unpacked_locations() -> None:
    source = (
        "(\n"
        "    FIRST,\n"
        "    second,\n"
        ") = (\n"
        "    10,\n"
        "    20,\n"
        ")\n"
        "type Matrix[T] = (\n"
        "    list[list[T]]\n"
        ")\n"
        "A = 1; b = 2; import math\n"
        "class Inline:\n"
        "    x = 1; y = 2\n"
        "    def __init__(self) -> None:\n"
        "        [self.alpha, *self.beta] = [1, 2, 3]\n"
    )
    symbols = extract_symbols(source, module="pkg.layout")
    _assert_invariants(symbols, source, "<memory>", "pkg.layout")
    by_q = {s.qualname: s for s in symbols}

    first = by_q["pkg.layout.FIRST"]
    second = by_q["pkg.layout.second"]
    assert (first.kind, first.location, first.start_line, first.end_line) == (
        "constant",
        "<memory>:2:5",
        1,
        7,
    )
    assert (second.kind, second.location, second.start_line, second.end_line) == (
        "variable",
        "<memory>:3:5",
        1,
        7,
    )

    matrix = by_q["pkg.layout.Matrix"]
    assert (matrix.kind, matrix.location, matrix.start_line, matrix.end_line) == (
        "type_alias",
        "<memory>:8:6",
        8,
        10,
    )

    assert by_q["pkg.layout.A"].location == "<memory>:11:1"
    assert by_q["pkg.layout.b"].location == "<memory>:11:8"
    assert by_q["math"].location == "<memory>:11:22"
    assert by_q["pkg.layout.Inline.x"].location == "<memory>:13:5"
    assert by_q["pkg.layout.Inline.y"].location == "<memory>:13:12"
    assert by_q["pkg.layout.Inline.alpha"].location == "<memory>:15:10"
    assert by_q["pkg.layout.Inline.beta"].location == "<memory>:15:23"


def test_stringified_and_future_annotations_type_aliases() -> None:
    source = (
        "from __future__ import annotations\n"
        "import typing\n"
        "from typing import TypeAlias\n"
        "Alpha: TypeAlias = list[int]\n"
        'Beta: "TypeAlias" = dict[str, "Beta"]\n'
        'Gamma: "typing.TypeAlias" = tuple[int, ...]\n'
        "class Box:\n"
        '    not_alias: "TypeAlias"\n'
        '    Delta: "TypeAlias" = set[str]\n'
    )
    symbols = extract_symbols(source, module="pkg.str_alias")
    _assert_invariants(symbols, source, "<memory>", "pkg.str_alias")
    by_q = {s.qualname: s for s in symbols}

    assert by_q["pkg.str_alias.Alpha"].kind == "type_alias"
    assert by_q["pkg.str_alias.Beta"].kind == "type_alias"
    assert by_q["pkg.str_alias.Gamma"].kind == "type_alias"
    assert by_q["pkg.str_alias.Box.not_alias"].kind == "field"
    assert by_q["pkg.str_alias.Box.Delta"].kind == "type_alias"


def test_class_ast_hash_clones_and_raise_edge_cases() -> None:
    cls_a = (
        "@dataclass\n"
        "class UserDTO(BaseA):\n"
        '    """Doc 1."""\n'
        "    id: int\n"
        "    def key(self) -> int:\n"
        "        return self.id\n"
    )
    cls_b = (
        "class AccountDTO(BaseB):\n"
        "    # comment only\n"
        "    id: int\n"
        "    def key(self) -> int:\n"
        "        return self.id\n"
    )
    cls_diff_type = (
        "class OtherDTO:\n    id: str\n    def key(self) -> str:\n        return self.id\n"
    )
    cls_diff_name = (
        "class OtherDTO:\n    uid: int\n    def key(self) -> int:\n        return self.uid\n"
    )

    h_a = extract_symbols(cls_a)[1].ast_hash
    h_b = extract_symbols(cls_b)[1].ast_hash
    assert h_a is not None
    assert h_a == h_b
    assert extract_symbols(cls_diff_type)[1].ast_hash != h_a
    assert extract_symbols(cls_diff_name)[1].ast_hash != h_a

    reraise = "def reraise() -> None:\n    raise\n"
    dynamic_raise = "def dynamic_raise() -> None:\n    raise factory()()\n"
    assert extract_symbols(reraise)[1].ast_hash is not None
    assert extract_symbols(dynamic_raise)[1].ast_hash is not None


def test_rebinding_shadowing_and_duplicate_branch_nested_extraction() -> None:
    source = (
        "from math import sin\n"
        "x = 1\n"
        "x = 2\n"
        "def sin(v: float) -> float:\n"
        "    return v\n"
        "if FLAG:\n"
        "    def job() -> int:\n"
        "        return 1\n"
        "else:\n"
        "    def job() -> int:\n"
        "        import decimal\n"
        "        def sub() -> int:\n"
        "            return 2\n"
        "        return sub()\n"
    )
    symbols = extract_symbols(source, module="pkg.shadow")
    _assert_invariants(symbols, source, "<memory>", "pkg.shadow")
    by_q = {s.qualname: s for s in symbols}

    assert by_q["pkg.shadow.x"].location == "<memory>:2:1"
    assert by_q["math.sin"].kind == "external"
    assert by_q["pkg.shadow.sin"].kind == "function"
    assert by_q["pkg.shadow.job"].location == "<memory>:7:5"
    assert by_q["decimal"].kind == "external"
    assert by_q["pkg.shadow.job.sub"].kind == "function"


def test_filename_without_module_arg_treats_own_package_as_internal(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    init_py = pkg_dir / "__init__.py"
    source = "from mypkg import helper\ndef helper() -> int:\n    return 1\n"
    init_py.write_text(source, encoding="utf-8")

    symbols = extract_symbols(source, filename=init_py)
    _assert_invariants(symbols, source, str(init_py.resolve()), "mypkg")
    by_q = {s.qualname: s for s in symbols}

    assert by_q["mypkg.helper"].kind == "function"


def test_str_paths_symlinks_dunder_main_file_and_trailing_newlines(tmp_path: Path) -> None:
    src = tmp_path / "src"
    pkg = src / "pkg"
    pkg.mkdir(parents=True)
    main_py = pkg / "__main__.py"
    content = "VALUE = 1\n"
    main_py.write_text(content, encoding="utf-8")

    assert module_name_from_path(str(main_py), str(src)) == "pkg.__main__"
    assert discover_internal_packages(str(src)) == frozenset({"pkg"})

    direct_syms = parse_file(str(main_py), src_root=str(src))
    _assert_invariants(direct_syms, content, str(main_py.resolve()), "pkg.__main__")

    linked_src = tmp_path / "linked_src"
    linked_src.symlink_to(src)
    linked_syms = parse_file(linked_src / "pkg" / "__main__.py", src_root=linked_src)
    _assert_invariants(linked_syms, content, str(main_py.resolve()), "pkg.__main__")

    assert extract_symbols("a = 1")[0].end_line == 1
    assert extract_symbols("a = 1\n")[0].end_line == 1
    assert extract_symbols("a = 1\n\n")[0].end_line == 2
