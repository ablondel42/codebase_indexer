"""AST extraction, hierarchy, UTF-8 range, assignment, call, and SymbolLinker tests."""

import ast
import json
from pathlib import Path

import pytest

from core.models import (
    AST_DEPENDENCY_EDGE_KINDS,
    AppConfig,
    EdgeKind,
    LineSpan,
    ParseError,
    SymbolKind,
    SymbolNode,
    SymbolScope,
)
from core.utils.pathing import derive_repo_id
from parsing import AstExtractionPipeline, FileGraphExtraction, RepoScanner, SymbolLinker
from parsing.utils.ast_helpers import load_python_source, parse_python_ast
from parsing.utils.ranges import (
    make_declaration_lines,
    make_name_selection_range,
    make_range,
)
from storage import SqlUnitOfWorkFactory

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def uow_factory(tmp_path: Path) -> SqlUnitOfWorkFactory:
    """Create an initialized SQLite UnitOfWorkFactory for persistence assertions."""
    factory = SqlUnitOfWorkFactory(tmp_path / "ast.db")
    with factory.open(initialize=True):
        pass
    return factory


def _extract_single_file(repo_root: Path, rel_path: str) -> FileGraphExtraction:
    """Scan and extract a single Python file inside ``repo_root``."""
    scanner = RepoScanner()
    pipeline = AstExtractionPipeline()
    repo_id = derive_repo_id(repo_root)
    file_rec = scanner.scan_file(repo_id, repo_root, repo_root / rel_path)
    return pipeline.extract_file(repo_root, file_rec)


def _persist_single_file(
    uow_factory: SqlUnitOfWorkFactory,
    repo_root: Path,
    rel_path: str,
) -> FileGraphExtraction:
    """Index and persist a single file inside ``repo_root`` respecting v2 FKs."""
    scanner = RepoScanner()
    pipeline = AstExtractionPipeline()
    repo = scanner.build_repo_record(repo_root)
    file_rec = scanner.scan_file(repo.id, repo_root, repo_root / rel_path)
    extraction = pipeline.extract_file(repo_root, file_rec)
    with uow_factory.open() as uow:
        uow.repos.upsert(repo)
        uow.replace_file_graph(file_rec, extraction.nodes, extraction.edges)
    return extraction


def test_contains_hierarchy_and_no_module_to_method_edges() -> None:
    """Module contains only top-level symbols; classes contain their direct methods."""
    extraction = _extract_single_file(FIXTURES_DIR / "simple_package", "services.py")
    nodes = extraction.nodes
    edges = extraction.edges

    by_id = {n.id: n for n in nodes}
    module_node = next(n for n in nodes if n.kind == SymbolKind.MODULE)
    auth_cls = next(n for n in nodes if n.name == "AuthService")
    login_method = next(n for n in nodes if n.name == "login")
    create_fn = next(n for n in nodes if n.name == "create_service")

    assert auth_cls.parent_id == module_node.id
    assert auth_cls.lexical_parent_id is None
    assert login_method.parent_id == auth_cls.id
    assert login_method.lexical_parent_id is None
    assert login_method.scope == SymbolScope.CLASS
    assert create_fn.parent_id == module_node.id

    contains = [e for e in edges if e.kind == EdgeKind.CONTAINS]
    pairs = {(e.from_id, e.to_id) for e in contains}
    assert (module_node.id, auth_cls.id) in pairs
    assert (module_node.id, create_fn.id) in pairs
    assert (auth_cls.id, login_method.id) in pairs

    # Strictly zero module -> method contains edges
    for src, dst in pairs:
        if by_id[src].kind == SymbolKind.MODULE:
            assert by_id[dst].kind not in (SymbolKind.METHOD, SymbolKind.ASYNC_METHOD)


def test_selection_range_for_class_def_async_def_and_utf8_columns(tmp_path: Path) -> None:
    """selection_range pinpoints identifier names on class/def/async def and UTF-8 lines."""
    repo = tmp_path / "utf8_repo"
    repo.mkdir()
    src_file = repo / "mod.py"
    src_file.write_text(
        'PREFIX = "héllo😀"\n'
        "class Foo:\n"
        "    pass\n"
        "\n"
        "def f(x: int) -> int:\n"
        "    return x\n"
        "\n"
        "async def sync() -> None:\n"
        "    return None\n"
        "\n"
        'héllo = "😀"; café = 2\n',
        encoding="utf-8",
    )
    extraction = _extract_single_file(repo, "mod.py")
    lines = src_file.read_text(encoding="utf-8").splitlines()

    def _slice_sel(node: SymbolNode) -> str:
        assert node.selection_range is not None
        sel = node.selection_range
        line_txt = lines[sel.start.line]
        return line_txt[sel.start.character : sel.end.character]

    foo = next(n for n in extraction.nodes if n.name == "Foo")
    fn_f = next(n for n in extraction.nodes if n.name == "f")
    async_sync = next(n for n in extraction.nodes if n.name == "sync")
    cafe = next(n for n in extraction.nodes if n.name == "café")

    assert _slice_sel(foo) == "Foo"
    assert _slice_sel(fn_f) == "f"
    assert _slice_sel(async_sync) == "sync"
    assert _slice_sel(cafe) == "café"


def test_decorator_inclusive_lines() -> None:
    """Decorated functions/classes include decorator lines in start_line..end_line."""
    extraction = _extract_single_file(FIXTURES_DIR / "decorators_case", "decorated.py")
    nodes = extraction.nodes

    compute = next(n for n in nodes if n.name == "compute")
    cached_cls = next(n for n in nodes if n.name == "CachedClass")
    get_val = next(n for n in nodes if n.name == "get_value")

    assert compute.start_line == 34  # @logged line above def compute on line 36
    assert compute.end_line == 38
    assert cached_cls.start_line == 41
    assert get_val.start_line == 48


def test_nested_classes_and_local_classes_without_doubled_name(tmp_path: Path) -> None:
    """Nested Outer.Inner classes and function-local classes build exact hierarchy & names."""
    repo = tmp_path / "nested_cls_repo"
    repo.mkdir()
    (repo / "nested.py").write_text(
        "class Outer:\n"
        "    TAG = 'outer'\n"
        "    class Inner:\n"
        "        VALUE = 42\n"
        "        def ping(self) -> int:\n"
        "            return 1\n",
        encoding="utf-8",
    )
    extraction = _extract_single_file(repo, "nested.py")
    nodes = extraction.nodes

    outer = next(n for n in nodes if n.name == "Outer")
    inner = next(n for n in nodes if n.name == "Inner")
    inner_val = next(n for n in nodes if n.name == "VALUE")
    ping = next(n for n in nodes if n.name == "ping")

    assert inner.qualified_name == "nested.Outer.Inner"
    assert inner.parent_id == outer.id
    assert inner.lexical_parent_id is None
    assert inner.scope == SymbolScope.CLASS
    assert inner_val.kind == SymbolKind.ATTRIBUTE
    assert inner_val.parent_id == inner.id
    assert ping.parent_id == inner.id
    assert ping.qualified_name == "nested.Outer.Inner.ping"

    # Local class from fixture must not double the factory function name
    local_ext = _extract_single_file(FIXTURES_DIR / "nested_scope_case", "local_classes.py")
    factory = next(n for n in local_ext.nodes if n.name == "factory")
    local_cls = next(n for n in local_ext.nodes if n.name == "LocalClass")
    local_method = next(n for n in local_ext.nodes if n.name == "method")

    assert local_cls.qualified_name == "local_classes.factory.LocalClass"
    assert local_cls.scope == SymbolScope.FUNCTION
    assert local_cls.parent_id == factory.id
    assert local_cls.lexical_parent_id == factory.id
    assert local_method.parent_id == local_cls.id
    assert local_method.lexical_parent_id is None
    assert local_method.scope == SymbolScope.CLASS
    assert any(
        e.kind == EdgeKind.SCOPE_PARENT and e.from_id == local_cls.id and e.to_id == factory.id
        for e in local_ext.edges
    )


def test_assignments_constants_attributes_and_type_aliases(tmp_path: Path) -> None:
    """Extracts module constants, class attributes, and PEP 695/typing type_alias symbols."""
    repo = tmp_path / "assign_repo"
    repo.mkdir()
    (repo / "defs.py").write_text(
        "from typing import TypeAlias\n"
        "MAX_SIZE: int = 100\n"
        "UserId: TypeAlias = int\n"
        "type AliasList = list[str]\n"
        "A, B = 1, 2\n"
        "class Config:\n"
        "    timeout: float = 5.0\n"
        "    RETRIES = 3\n",
        encoding="utf-8",
    )
    extraction = _extract_single_file(repo, "defs.py")
    by_name = {n.name: n for n in extraction.nodes}

    assert by_name["MAX_SIZE"].kind == SymbolKind.CONSTANT
    assert by_name["A"].kind == SymbolKind.CONSTANT
    assert by_name["B"].kind == SymbolKind.CONSTANT
    assert by_name["UserId"].kind == SymbolKind.TYPE_ALIAS
    assert by_name["AliasList"].kind == SymbolKind.TYPE_ALIAS
    assert by_name["timeout"].kind == SymbolKind.ATTRIBUTE
    assert by_name["RETRIES"].kind == SymbolKind.ATTRIBUTE
    assert extraction.metrics.constant_count == 3
    assert extraction.metrics.attribute_count == 2
    assert extraction.metrics.type_alias_count == 2


def test_relative_imports_calls_linker_resolution_and_broken_target_reset(
    uow_factory: SqlUnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    """SymbolLinker resolves intra-repo edges and resets broken targets back to placeholders."""
    repo = tmp_path / "app_repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("from .core import BaseService\n", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "class BaseService:\n"
        "    def step(self) -> int:\n"
        "        return 1\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "worker.py").write_text(
        "from . import BaseService\n"
        "from .core import helper as run_helper\n"
        "\n"
        "class Worker(BaseService):\n"
        "    def run(self) -> int:\n"
        "        a = super().step()\n"
        "        b = self.compute()\n"
        "        return a + b + run_helper(1)\n"
        "\n"
        "    def compute(self) -> int:\n"
        "        return 2\n",
        encoding="utf-8",
    )

    cfg = AppConfig()
    scan_report = RepoScanner(cfg).scan_repository(repo)
    pipeline = AstExtractionPipeline(cfg)
    linker = SymbolLinker()

    with uow_factory.open() as uow:
        uow.repos.upsert(scan_report.repo)
        for file_rec in scan_report.files:
            ext = pipeline.extract_file(repo, file_rec)
            uow.replace_file_graph(file_rec, ext.nodes, ext.edges)

        nodes = uow.nodes.list_for_repo(scan_report.repo.id)
        candidate_edges = uow.edges.list_for_repo(
            scan_report.repo.id, kinds=AST_DEPENDENCY_EDGE_KINDS
        )
        link_report = linker.resolve(
            scan_report.repo.id, scan_report.repo.name, nodes, candidate_edges
        )
        uow.edges.update_resolved_targets(link_report.updates)

    assert link_report.imports_resolved >= 3
    assert link_report.inherits_resolved >= 1
    assert link_report.calls_resolved >= 3

    with uow_factory.open() as uow:
        by_name = {n.name: n.id for n in uow.nodes.list_for_repo(scan_report.repo.id)}
        inh_edges = uow.edges.list_outgoing(by_name["Worker"], EdgeKind.INHERITS)
        assert [e.to_id for e in inh_edges] == [by_name["BaseService"]]

        call_targets = {e.to_id for e in uow.edges.list_outgoing(by_name["run"], EdgeKind.CALLS)}
        assert {by_name["step"], by_name["compute"], by_name["helper"]}.issubset(call_targets)

        # Removing core.py preserves surviving AST edges and SymbolLinker resets broken targets
        core_file = uow.files.get_by_path(scan_report.repo.id, "pkg/core.py")
        assert core_file is not None
        uow.files.delete_file_graph(core_file.id, preserve_incoming_ast_edges=True)
        surviving_nodes = uow.nodes.list_for_repo(scan_report.repo.id)
        surviving_edges = uow.edges.list_for_repo(
            scan_report.repo.id, kinds=AST_DEPENDENCY_EDGE_KINDS
        )
        reset_report = linker.resolve(
            scan_report.repo.id, scan_report.repo.name, surviving_nodes, surviving_edges
        )
        assert reset_report.edges_reset >= 2


def test_edge_json_is_single_encoded_in_sqlite(uow_factory: SqlUnitOfWorkFactory) -> None:
    """Persisted edge JSON columns parse once directly to dicts (never double-encoded strings)."""
    _persist_single_file(uow_factory, FIXTURES_DIR / "simple_package", "services.py")
    res = uow_factory.execute_readonly_sql(
        "SELECT payload_json, evidence_range_json FROM edges WHERE evidence_range_json IS NOT NULL"
    )
    assert res.row_count > 0
    for row in res.rows:
        payload = json.loads(row["payload_json"])
        ev_range = json.loads(row["evidence_range_json"])
        assert isinstance(payload, dict)
        assert isinstance(ev_range, dict)


def test_duplicate_names_and_generator_scope_boundary(tmp_path: Path) -> None:
    """Duplicate same-scope defs disambiguate; outer func isn't generator for inner yield."""
    dup_ext = _extract_single_file(FIXTURES_DIR / "nested_scope_case", "duplicate_names.py")
    inner_nodes = [n for n in dup_ext.nodes if n.name == "inner"]
    assert len(inner_nodes) == 2
    assert inner_nodes[0].id.endswith("duplicate_names.outer_with_duplicates.inner")
    assert inner_nodes[1].id.endswith(":dup1")

    repo = tmp_path / "gen_repo"
    repo.mkdir()
    (repo / "gen.py").write_text(
        "def outer():\n    def inner_gen():\n        yield 1\n    return inner_gen\n",
        encoding="utf-8",
    )
    gen_ext = _extract_single_file(repo, "gen.py")
    outer = next(n for n in gen_ext.nodes if n.name == "outer")
    inner_gen = next(n for n in gen_ext.nodes if n.name == "inner_gen")
    assert outer.payload["is_generator"] is False
    assert inner_gen.payload["is_generator"] is True


def test_ast_loader_and_range_utilities(tmp_path: Path) -> None:
    """parse_python_ast wraps syntax errors in ParseError and ranges return typed spans."""
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
    text = load_python_source(bad)

    with pytest.raises(ParseError):
        parse_python_ast(text, filename=bad)

    tree = ast.parse("@dec\ndef g():\n    pass\n")
    fn = tree.body[0]
    assert make_declaration_lines(fn) == LineSpan(start_line=1, end_line=3)
    rng = make_range(fn)
    sel = make_name_selection_range(fn, "def g():")
    assert rng is not None and sel is not None
    assert sel.start.line == 1
