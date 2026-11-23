"""Tests for AST symbol extraction in cbi.ast."""

from pathlib import Path

from cbi.ast import (
    discover_internal_packages,
    extract_symbols,
    module_name_from_path,
    parse_file,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
MOCK_PATH = TESTS_ROOT / "fixtures" / "mock.py"


def test_parse_mock_fixture_extracts_all_kinds_and_skips_locals() -> None:
    symbols = extract_symbols(
        MOCK_PATH.read_text(encoding="utf-8"),
        filename=MOCK_PATH,
        module="cbi.ast.mock",
    )
    by_qualname = {sym.qualname: sym for sym in symbols}

    assert symbols[0].kind == "module"
    assert symbols[0].qualname == "cbi.ast.mock"

    # External venv & stdlib imports kept; internal relative `..storage` skipped.
    assert by_qualname["pydantic.BaseModel"].kind == "external"
    assert by_qualname["pydantic.Field"].kind == "external"
    assert by_qualname["pathlib"].kind == "external"
    assert not any("storage" in q or q.endswith(".db") for q in by_qualname)

    # Module-level declarations.
    assert by_qualname["cbi.ast.mock.Payload"].kind == "type_alias"
    assert by_qualname["cbi.ast.mock.DEFAULT_RETRIES"].kind == "constant"
    assert by_qualname["cbi.ast.mock.__all__"].kind == "variable"

    # Classes, dataclass/Pydantic fields, and __init__ self.<attr> fields.
    assert by_qualname["cbi.ast.mock.QuotaExceededError"].ast_hash is None
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

    # Local variables inside functions/methods must never leak into module/class symbols.
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


def test_multi_root_internal_packages_not_marked_external() -> None:
    internal = discover_internal_packages(SRC_ROOT, TESTS_ROOT)
    assert "cbi" in internal
    assert "ui" not in internal

    this_file = Path(__file__).resolve()
    symbols = parse_file(this_file, src_root=REPO_ROOT, internal_packages=internal)
    externals = {sym.qualname for sym in symbols if sym.kind == "external"}

    assert "pathlib.Path" in externals
    assert not any(q.startswith("cbi.") for q in externals)


def test_overload_stubs_do_not_shadow_implementation() -> None:
    source = (
        "import typing\n"
        "from typing import overload\n\n"
        "@overload\n"
        "def load(raw: str) -> int: ...\n\n"
        "@typing.overload\n"
        "def load(raw: bytes) -> int: ...\n\n"
        "def load(raw: str | bytes) -> int:\n"
        "    return len(raw)\n"
    )
    symbols = extract_symbols(source, module="pkg.loader")
    funcs = [sym for sym in symbols if sym.name == "load"]

    assert len(funcs) == 1
    assert funcs[0].qualname == "pkg.loader.load"
    assert funcs[0].start_line == 10
    assert funcs[0].ast_hash is not None
    assert funcs[0].decorators == ()


def test_call_property_decorators_and_reversed_main_guard() -> None:
    source = (
        "from pydantic import BaseModel, computed_field\n\n"
        "class Report(BaseModel):\n"
        "    @computed_field()\n"
        "    def total(self) -> int:\n"
        "        return 1\n\n"
        'if "__main__" == __name__:\n'
        "    leaked = 99\n"
    )
    symbols = extract_symbols(source, module="pkg.report")
    by_qualname = {sym.qualname: sym for sym in symbols}

    assert by_qualname["pkg.report.Report.total"].kind == "property"
    assert "leaked" not in {sym.name for sym in symbols}


def test_ignores_throwaway_underscore_and_main_guard() -> None:
    source = (
        "_ = 123\n"
        "_, KEEP = 1, 2\n\n"
        "class Box:\n"
        "    def __init__(self) -> None:\n"
        "        self._ = 0\n"
        "        self.value = 1\n\n"
        'if __name__ == "__main__":\n'
        "    scratch = 99\n"
    )
    symbols = extract_symbols(source, module="pkg.sample")
    names = {sym.name for sym in symbols}

    assert "_" not in names
    assert "scratch" not in names
    assert {"sample", "KEEP", "Box", "__init__", "value"} == names


def test_ast_hash_matches_clones_and_skips_stubs() -> None:
    src_a = 'def alpha(x: str) -> str:\n    """Doc A."""\n    return x.strip().lower()\n'
    src_b = "def beta(x: str) -> str:\n    # comment\n    return x.strip().lower()\n"
    stub = "def empty() -> None:\n    pass\n"

    sym_a = extract_symbols(src_a)[1]
    sym_b = extract_symbols(src_b)[1]
    sym_stub = extract_symbols(stub)[1]

    assert sym_a.ast_hash is not None
    assert sym_a.ast_hash == sym_b.ast_hash
    assert sym_stub.ast_hash is None


def test_init_py_uses_package_qualname(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "cbi" / "sub"
    pkg_dir.mkdir(parents=True)
    init_file = pkg_dir / "__init__.py"
    init_file.write_text("class Exported:\n    value: int = 1\n", encoding="utf-8")

    assert module_name_from_path(init_file, tmp_path) == "cbi.sub"

    symbols = parse_file(init_file, src_root=tmp_path)
    assert [(sym.kind, sym.qualname) for sym in symbols] == [
        ("module", "cbi.sub"),
        ("class", "cbi.sub.Exported"),
        ("field", "cbi.sub.Exported.value"),
    ]


def test_deduplicates_fields_declared_in_class_and_init() -> None:
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
    assert [(sym.kind, sym.qualname) for sym in symbols] == [
        ("module", "pkg.item"),
        ("class", "pkg.item.Item"),
        ("field", "pkg.item.Item.x"),
        ("method", "pkg.item.Item.__init__"),
        ("field", "pkg.item.Item.y"),
    ]
