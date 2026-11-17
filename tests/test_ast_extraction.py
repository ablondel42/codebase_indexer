"""AST extraction, hierarchy, UTF-8 range, assignment, call, and linker tests (v0.2.0)."""

import ast
from collections.abc import Iterator
import json
from pathlib import Path
import sqlite3

import pytest

from repo_context.config import AppConfig
from repo_context.constants import EDGE_KIND_CALLS, EDGE_KIND_CONTAINS, EDGE_KIND_SCOPE_PARENT
from repo_context.exceptions import ParseError
from repo_context.parsing.ast_loader import load_file_text, parse_file
from repo_context.parsing.linker import resolve_repo_edges
from repo_context.parsing.pathing import derive_repo_id
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.ranges import make_declaration_lines, make_name_selection_range, make_range
from repo_context.parsing.scanner import build_file_record, build_repo_record, scan_repository
from repo_context.storage import (
    close_connection,
    get_connection,
    initialize_database,
    replace_file_graph,
    upsert_files,
    upsert_repo,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def initialized_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield an initialized v2 SQLite connection and close it on teardown."""
    conn = get_connection(tmp_path / "ast.db")
    try:
        initialize_database(conn)
        yield conn
    finally:
        close_connection(conn)


def _persist_single_file(
    conn: sqlite3.Connection,
    repo_root: Path,
    rel_path: str,
) -> tuple[str, list[dict], list[dict]]:
    """Index and persist a single file inside ``repo_root`` respecting v2 FKs."""
    repo = build_repo_record(repo_root)
    file_rec = build_file_record(repo.id, repo_root, repo_root / rel_path)
    nodes, edges, _ = extract_file_graph(repo.id, file_rec, repo_root)
    upsert_repo(conn, repo)
    upsert_files(conn, [file_rec])
    replace_file_graph(conn, file_rec.id, nodes, edges)
    conn.commit()
    return repo.id, nodes, edges


def test_contains_hierarchy_and_no_module_to_method_edges(tmp_path: Path) -> None:
    """Module contains only top-level symbols; classes contain their direct methods."""
    repo_root = FIXTURES_DIR / "simple_package"
    repo_id = derive_repo_id(repo_root)
    file_rec = build_file_record(repo_id, repo_root, repo_root / "services.py")
    nodes, edges, _ = extract_file_graph(repo_id, file_rec, repo_root)

    by_id = {n["id"]: n for n in nodes}
    module_node = next(n for n in nodes if n["kind"] == "module")
    auth_cls = next(n for n in nodes if n["name"] == "AuthService")
    login_method = next(n for n in nodes if n["name"] == "login")
    create_fn = next(n for n in nodes if n["name"] == "create_service")

    assert auth_cls["parent_id"] == module_node["id"]
    assert auth_cls["lexical_parent_id"] is None
    assert login_method["parent_id"] == auth_cls["id"]
    assert login_method["lexical_parent_id"] is None
    assert login_method["scope"] == "class"
    assert create_fn["parent_id"] == module_node["id"]

    contains = [e for e in edges if e["kind"] == EDGE_KIND_CONTAINS]
    pairs = {(e["from_id"], e["to_id"]) for e in contains}
    assert (module_node["id"], auth_cls["id"]) in pairs
    assert (module_node["id"], create_fn["id"]) in pairs
    assert (auth_cls["id"], login_method["id"]) in pairs

    # Strictly zero module -> method contains edges
    for src, dst in pairs:
        if by_id[src]["kind"] == "module":
            assert by_id[dst]["kind"] not in ("method", "async_method")


def test_selection_range_for_class_def_async_def_and_utf8_columns(tmp_path: Path) -> None:
    """selection_range_json pinpoints identifier names on class/def/async def and UTF-8 lines."""
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
    repo_id = derive_repo_id(repo)
    file_rec = build_file_record(repo_id, repo, src_file)
    nodes, _, _ = extract_file_graph(repo_id, file_rec, repo)
    lines = src_file.read_text(encoding="utf-8").splitlines()

    def _slice_sel(node: dict) -> str:
        sel = node["selection_range_json"]
        line_txt = lines[sel["start"]["line"]]
        return line_txt[sel["start"]["character"] : sel["end"]["character"]]

    foo = next(n for n in nodes if n["name"] == "Foo")
    fn_f = next(n for n in nodes if n["name"] == "f")
    async_sync = next(n for n in nodes if n["name"] == "sync")
    cafe = next(n for n in nodes if n["name"] == "café")

    assert _slice_sel(foo) == "Foo"
    assert _slice_sel(fn_f) == "f"
    assert _slice_sel(async_sync) == "sync"
    assert _slice_sel(cafe) == "café"


def test_decorator_inclusive_lines() -> None:
    """Decorated functions/classes include decorator lines in start_line..end_line."""
    repo_root = FIXTURES_DIR / "decorators_case"
    repo_id = derive_repo_id(repo_root)
    file_rec = build_file_record(repo_id, repo_root, repo_root / "decorated.py")
    nodes, _, _ = extract_file_graph(repo_id, file_rec, repo_root)

    compute = next(n for n in nodes if n["name"] == "compute")
    cached_cls = next(n for n in nodes if n["name"] == "CachedClass")
    get_val = next(n for n in nodes if n["name"] == "get_value")

    assert compute["start_line"] == 34  # @logged line above def compute on line 36
    assert compute["end_line"] == 38
    assert cached_cls["start_line"] == 41
    assert get_val["start_line"] == 48


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
    repo_id = derive_repo_id(repo)
    file_rec = build_file_record(repo_id, repo, repo / "nested.py")
    nodes, edges, _ = extract_file_graph(repo_id, file_rec, repo)

    outer = next(n for n in nodes if n["name"] == "Outer")
    inner = next(n for n in nodes if n["name"] == "Inner")
    inner_val = next(n for n in nodes if n["name"] == "VALUE")
    ping = next(n for n in nodes if n["name"] == "ping")

    assert inner["qualified_name"] == "nested.Outer.Inner"
    assert inner["parent_id"] == outer["id"]
    assert inner["lexical_parent_id"] is None
    assert inner["scope"] == "class"
    assert inner_val["kind"] == "attribute"
    assert inner_val["parent_id"] == inner["id"]
    assert ping["parent_id"] == inner["id"]
    assert ping["qualified_name"] == "nested.Outer.Inner.ping"

    # Local class from fixture must not double the factory function name
    local_root = FIXTURES_DIR / "nested_scope_case"
    local_repo_id = derive_repo_id(local_root)
    local_rec = build_file_record(local_repo_id, local_root, local_root / "local_classes.py")
    local_nodes, local_edges, _ = extract_file_graph(local_repo_id, local_rec, local_root)

    factory = next(n for n in local_nodes if n["name"] == "factory")
    local_cls = next(n for n in local_nodes if n["name"] == "LocalClass")
    local_method = next(n for n in local_nodes if n["name"] == "method")

    assert local_cls["qualified_name"] == "local_classes.factory.LocalClass"
    assert local_cls["scope"] == "function"
    assert local_cls["parent_id"] == factory["id"]
    assert local_cls["lexical_parent_id"] == factory["id"]
    assert local_method["parent_id"] == local_cls["id"]
    assert local_method["lexical_parent_id"] is None
    assert local_method["scope"] == "class"
    assert any(
        e["kind"] == EDGE_KIND_SCOPE_PARENT
        and e["from_id"] == local_cls["id"]
        and e["to_id"] == factory["id"]
        for e in local_edges
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
    repo_id = derive_repo_id(repo)
    file_rec = build_file_record(repo_id, repo, repo / "defs.py")
    nodes, _, summary = extract_file_graph(repo_id, file_rec, repo)

    by_name = {n["name"]: n for n in nodes}
    assert by_name["MAX_SIZE"]["kind"] == "constant"
    assert by_name["A"]["kind"] == "constant"
    assert by_name["B"]["kind"] == "constant"
    assert by_name["UserId"]["kind"] == "type_alias"
    assert by_name["AliasList"]["kind"] == "type_alias"
    assert by_name["timeout"]["kind"] == "attribute"
    assert by_name["RETRIES"]["kind"] == "attribute"
    assert summary["constant_count"] == 3
    assert summary["attribute_count"] == 2
    assert summary["type_alias_count"] == 2


def test_relative_imports_calls_and_linker_resolution(
    initialized_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Extracts relative imports & call qualifiers, and resolves intra-repo edges cleanly."""
    repo = tmp_path / "app_repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text(
        "from .core import BaseService\n", encoding="utf-8"
    )
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
    repo_rec, files = scan_repository(repo, cfg)
    upsert_repo(initialized_conn, repo_rec)
    upsert_files(initialized_conn, files)
    for file_rec in files:
        nodes, edges, _ = extract_file_graph(repo_rec.id, file_rec, repo, config=cfg)
        replace_file_graph(initialized_conn, file_rec.id, nodes, edges)

    link_stats = resolve_repo_edges(initialized_conn, repo_rec.id)
    initialized_conn.commit()

    assert link_stats["imports_resolved"] >= 3
    assert link_stats["inherits_resolved"] >= 1
    assert link_stats["calls_resolved"] >= 3

    worker_cls = initialized_conn.execute(
        "SELECT id FROM nodes WHERE name = 'Worker'"
    ).fetchone()["id"]
    base_cls = initialized_conn.execute(
        "SELECT id FROM nodes WHERE name = 'BaseService'"
    ).fetchone()["id"]
    step_fn = initialized_conn.execute(
        "SELECT id FROM nodes WHERE name = 'step'"
    ).fetchone()["id"]
    compute_fn = initialized_conn.execute(
        "SELECT id FROM nodes WHERE name = 'compute'"
    ).fetchone()["id"]
    helper_fn = initialized_conn.execute(
        "SELECT id FROM nodes WHERE name = 'helper'"
    ).fetchone()["id"]
    run_fn = initialized_conn.execute(
        "SELECT id FROM nodes WHERE name = 'run'"
    ).fetchone()["id"]

    inh_edge = initialized_conn.execute(
        "SELECT to_id FROM edges WHERE kind = 'inherits' AND from_id = ?",
        (worker_cls,),
    ).fetchone()
    assert inh_edge["to_id"] == base_cls

    call_targets = {
        row["to_id"]
        for row in initialized_conn.execute(
            "SELECT to_id FROM edges WHERE kind = ? AND from_id = ?",
            (EDGE_KIND_CALLS, run_fn),
        ).fetchall()
    }
    assert {step_fn, compute_fn, helper_fn}.issubset(call_targets)


def test_edge_json_is_single_encoded_in_sqlite(
    initialized_conn: sqlite3.Connection,
) -> None:
    """Persisted edge JSON columns parse once directly to dicts (never double-encoded strings)."""
    _persist_single_file(initialized_conn, FIXTURES_DIR / "simple_package", "services.py")
    rows = initialized_conn.execute(
        "SELECT payload_json, evidence_range_json FROM edges WHERE evidence_range_json IS NOT NULL"
    ).fetchall()
    assert len(rows) > 0
    for row in rows:
        payload = json.loads(row["payload_json"])
        ev_range = json.loads(row["evidence_range_json"])
        assert isinstance(payload, dict)
        assert isinstance(ev_range, dict)


def test_duplicate_names_and_generator_scope_boundary(tmp_path: Path) -> None:
    """Duplicate same-scope declarations disambiguate cleanly; outer func isn't generator for inner yield."""
    local_root = FIXTURES_DIR / "nested_scope_case"
    repo_id = derive_repo_id(local_root)
    dup_rec = build_file_record(repo_id, local_root, local_root / "duplicate_names.py")
    nodes, _, _ = extract_file_graph(repo_id, dup_rec, local_root)
    inner_nodes = [n for n in nodes if n["name"] == "inner"]
    assert len(inner_nodes) == 2
    assert inner_nodes[0]["id"].endswith("duplicate_names.outer_with_duplicates.inner")
    assert inner_nodes[1]["id"].endswith(":dup1")

    repo = tmp_path / "gen_repo"
    repo.mkdir()
    (repo / "gen.py").write_text(
        "def outer():\n"
        "    def inner_gen():\n"
        "        yield 1\n"
        "    return inner_gen\n",
        encoding="utf-8",
    )
    gen_repo_id = derive_repo_id(repo)
    gen_rec = build_file_record(gen_repo_id, repo, repo / "gen.py")
    gen_nodes, _, _ = extract_file_graph(gen_repo_id, gen_rec, repo)
    outer = next(n for n in gen_nodes if n["name"] == "outer")
    inner_gen = next(n for n in gen_nodes if n["name"] == "inner_gen")
    assert json.loads(outer["payload_json"])["is_generator"] is False
    assert json.loads(inner_gen["payload_json"])["is_generator"] is True


def test_ast_loader_and_range_utilities(tmp_path: Path) -> None:
    """parse_file wraps syntax errors in ParseError and make_declaration_lines tracks decorators."""
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
    text = load_file_text(bad)

    with pytest.raises(ParseError):
        parse_file(text, filename=bad)

    tree = ast.parse("@dec\ndef g():\n    pass\n")
    fn = tree.body[0]
    assert make_declaration_lines(fn) == (1, 3)
    rng = make_range(fn)
    sel = make_name_selection_range(fn, "def g():")
    assert rng is not None and sel is not None
    assert sel["start"]["line"] == 1
