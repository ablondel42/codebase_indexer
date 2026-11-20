"""Comprehensive subprocess tests for all grouped ``cbi`` CLI commands via ``cli.main``."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"


def _run_cbi(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``python -m cli.main`` with ``src/`` prioritized on ``PYTHONPATH``."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return subprocess.run(
        [sys.executable, "-m", "cli.main", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or PROJECT_ROOT),
        env=env,
    )


@pytest.fixture
def cli_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a hermetic multi-module repo and temp DB path inside ``tmp_path``."""
    repo_root = tmp_path / "cli_repo"
    (repo_root / "pkg").mkdir(parents=True)
    (repo_root / "tests").mkdir()

    (repo_root / "pkg" / "__init__.py").write_text('"""Pkg init."""\n', encoding="utf-8")
    (repo_root / "pkg" / "math_ops.py").write_text(
        '"""Math operations."""\n\n'
        "def add(a: int, b: int) -> int:\n"
        '    """Add two numbers."""\n'
        "    return a + b\n\n"
        "class Calculator:\n"
        '    """Simple calculator."""\n\n'
        "    def double(self, val: int) -> int:\n"
        "        return add(val, val)\n\n"
        "    def _internal(self) -> int:\n"
        "        return 0\n",
        encoding="utf-8",
    )
    (repo_root / "pkg" / "runner.py").write_text(
        '"""Runner module."""\n'
        "from pkg.math_ops import Calculator, add\n\n"
        "def run_calc() -> int:\n"
        "    calc = Calculator()\n"
        "    return calc.double(2) + add(1, 2)\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "test_math.py").write_text(
        "def test_add() -> None:\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "cli_test.db"
    return repo_root, db_path


@pytest.fixture
def indexed_workspace(cli_workspace: tuple[Path, Path]) -> tuple[Path, Path]:
    """Run ``cbi index`` on ``cli_workspace`` and return ``(repo_root, db_path)``."""
    repo_root, db_path = cli_workspace
    res = _run_cbi("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert res.returncode == 0, res.stderr
    return repo_root, db_path


def test_cbi_index_initial_incremental_force_and_include_tests(
    cli_workspace: tuple[Path, Path],
) -> None:
    """``cbi index`` handles initial, incremental, ``--force``, and ``--include-tests`` runs."""
    repo_root, db_path = cli_workspace

    init_res = _run_cbi("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert init_res.returncode == 0, init_res.stderr
    init_data = json.loads(init_res.stdout)
    assert init_data["files_scanned"] == 3
    assert init_data["files_indexed"] == 3
    assert init_data["linker"]["total_resolved"] >= 3

    inc_res = _run_cbi("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert inc_res.returncode == 0
    inc_data = json.loads(inc_res.stdout)
    assert inc_data["files_indexed"] == 0
    assert inc_data["files_unchanged"] == 3

    force_res = _run_cbi(
        "index",
        str(repo_root),
        "--db-path",
        str(db_path),
        "--force",
        "--include-tests",
        cwd=repo_root,
    )
    assert force_res.returncode == 0
    force_data = json.loads(force_res.stdout)
    assert force_data["files_scanned"] == 4
    assert force_data["files_indexed"] == 4


def test_cbi_db_subcommands_and_read_only_sql_guard(
    indexed_workspace: tuple[Path, Path],
) -> None:
    """``cbi db {status,repos,tables,runs,sql}`` emit JSON objects and block SQL mutations."""
    repo_root, db_path = indexed_workspace

    for sub in ("status", "repos", "tables", "runs"):
        res = _run_cbi("db", sub, "--db-path", str(db_path), cwd=repo_root)
        assert res.returncode == 0, f"db {sub} failed: {res.stderr}"
        parsed = json.loads(res.stdout)
        assert isinstance(parsed, dict)

    select_res = _run_cbi(
        "db",
        "sql",
        "SELECT name, kind FROM nodes WHERE kind = 'class' ORDER BY name",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert select_res.returncode == 0, select_res.stderr
    rows = json.loads(select_res.stdout)["rows"]
    assert rows == [{"name": "Calculator", "kind": "class"}]

    forbidden_queries = (
        "DELETE FROM nodes",
        "DROP TABLE nodes",
        "UPDATE nodes SET name = 'hacked'",
        (
            "INSERT INTO repos (id, root_path, name, default_language, created_at) "
            "VALUES ('a','b','c','python','2026-01-01T00:00:00Z')"
        ),
    )
    for forbidden_sql in forbidden_queries:
        bad_res = _run_cbi("db", "sql", forbidden_sql, "--db-path", str(db_path), cwd=repo_root)
        assert bad_res.returncode != 0, f"Expected failure for {forbidden_sql}"
        bad_err = json.loads(bad_res.stderr)
        assert bad_err["ok"] is False
        assert "not authorized" in bad_err["error"]["message"]


def test_cbi_files_and_outline_json(
    indexed_workspace: tuple[Path, Path],
) -> None:
    """``cbi files`` reflects live disk freshness and ``cbi outline`` emits hierarchical JSON."""
    repo_root, db_path = indexed_workspace

    files_res = _run_cbi("files", "--db-path", str(db_path), cwd=repo_root)
    assert files_res.returncode == 0
    files_payload = json.loads(files_res.stdout)
    fresh_paths = {
        item["file_path"]: item["freshness"]
        for item in files_payload["files"]
    }
    assert fresh_paths.get("pkg/math_ops.py") == "fresh"

    math_file = repo_root / "pkg" / "math_ops.py"
    math_file.write_text(math_file.read_text(encoding="utf-8") + "\n# modified\n", encoding="utf-8")
    files_mod = _run_cbi("files", "math_ops", "--db-path", str(db_path), cwd=repo_root)
    assert files_mod.returncode == 0
    entries = json.loads(files_mod.stdout)["files"]
    assert len(entries) == 1
    assert entries[0]["freshness"] == "modified"

    outline_public = _run_cbi(
        "outline",
        "pkg/math_ops.py",
        "--no-private",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert outline_public.returncode == 0, outline_public.stderr
    outline_pub_data = json.loads(outline_public.stdout)
    calc_node = next(s for s in outline_pub_data["symbols"] if s["name"] == "Calculator")
    assert calc_node["id"].startswith("node:")
    assert [c["name"] for c in calc_node["children"]] == ["double"]

    outline_full = _run_cbi("outline", "pkg/math_ops.py", "--db-path", str(db_path), cwd=repo_root)
    assert outline_full.returncode == 0
    outline_full_data = json.loads(outline_full.stdout)
    assert outline_full_data["module_path"] == "pkg.math_ops"
    full_calc = next(s for s in outline_full_data["symbols"] if s["name"] == "Calculator")
    assert "_internal" in [c["name"] for c in full_calc["children"]]


def test_cbi_symbols_and_source_commands(
    indexed_workspace: tuple[Path, Path],
) -> None:
    """``cbi symbols {search,list,show}`` and ``cbi source`` emit valid structured JSON."""
    repo_root, db_path = indexed_workspace

    search_res = _run_cbi(
        "symbols",
        "search",
        "Calculator",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert search_res.returncode == 0
    search_data = json.loads(search_res.stdout)
    qualified = {s["qualified_name"] for s in search_data["symbols"]}
    assert "pkg.math_ops.Calculator" in qualified

    list_res = _run_cbi(
        "symbols",
        "list",
        "--kind",
        "module",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert list_res.returncode == 0
    modules = json.loads(list_res.stdout)["symbols"]
    assert len(modules) == 3
    mod_id = modules[0]["id"]

    show_mod = _run_cbi("symbols", "show", mod_id, "--db-path", str(db_path), cwd=repo_root)
    assert show_mod.returncode == 0, show_mod.stderr
    show_mod_data = json.loads(show_mod.stdout)
    assert show_mod_data["focus_symbol"]["id"] == mod_id
    assert show_mod_data["structural_parent"] is None

    show_fn = _run_cbi(
        "symbols",
        "show",
        "pkg.math_ops.add",
        "--source",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert show_fn.returncode == 0
    assert "def add(" in json.loads(show_fn.stdout)["focus_symbol"]["source"]

    src_res = _run_cbi("source", "pkg.math_ops.add", "--db-path", str(db_path), cwd=repo_root)
    assert src_res.returncode == 0
    src_data = json.loads(src_res.stdout)
    assert src_data["symbol_id"].startswith("node:")
    assert "def add(" in src_data["source"]
    assert "return a + b" in src_data["source"]


def test_cbi_refs_risk_and_mcp_call(
    indexed_workspace: tuple[Path, Path],
) -> None:
    """``cbi refs``, ``cbi risk``, daemon ``--help``, and ``cbi mcp-call`` emit valid JSON."""
    repo_root, db_path = indexed_workspace

    refs_res = _run_cbi(
        "refs",
        "pkg.math_ops.add",
        "--direction",
        "incoming",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert refs_res.returncode == 0
    refs_data = json.loads(refs_res.stdout)
    assert refs_data["total_count"] >= 2
    peers = {r["peer_name"] for r in refs_data["references"]}
    assert "pkg.runner.run_calc" in peers

    risk_multi = _run_cbi(
        "risk",
        "pkg.math_ops.add",
        "pkg.runner.run_calc",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert risk_multi.returncode == 0
    assert json.loads(risk_multi.stdout)["facts"]["target_spans_multiple_modules"] is True

    risk_single = _run_cbi("risk", "pkg.math_ops.add", "--db-path", str(db_path), cwd=repo_root)
    assert risk_single.returncode == 0
    assert json.loads(risk_single.stdout)["facts"]["cross_module_impact"] is True

    assert _run_cbi("watch", "--help").returncode == 0
    assert _run_cbi("serve", "--help").returncode == 0
    mcp_res = _run_cbi(
        "mcp-call",
        "get_repo_overview",
        "{}",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert mcp_res.returncode == 0, mcp_res.stderr
    assert json.loads(mcp_res.stdout)["result"]["ok"] is True


def test_cbi_symbols_list_and_search_preserve_dot_prefix_paths(tmp_path: Path) -> None:
    """``cbi symbols list/search --file .github/`` preserves leading dots cleanly."""
    repo_root = tmp_path / "dot_cli_repo"
    (repo_root / ".github").mkdir(parents=True)
    (repo_root / "github").mkdir()
    (repo_root / ".github" / "ci.py").write_text(
        "def check_ci() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (repo_root / "github" / "ci.py").write_text(
        "def check_non_dot() -> int:\n    return 2\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "dot_cli.db"
    idx_res = _run_cbi("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert idx_res.returncode == 0, idx_res.stderr

    for file_arg in (".github/", "./.github/", "./.github/ci.py"):
        list_res = _run_cbi(
            "symbols",
            "list",
            "--file",
            file_arg,
            "--db-path",
            str(db_path),
            cwd=repo_root,
        )
        assert list_res.returncode == 0, list_res.stderr
        syms = json.loads(list_res.stdout)["symbols"]
        assert syms
        assert {s["file_path"] for s in syms} == {".github/ci.py"}
        assert "check_ci" in {s["name"] for s in syms}

    search_res = _run_cbi(
        "symbols",
        "search",
        "check",
        "--file",
        ".github",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert search_res.returncode == 0, search_res.stderr
    search_syms = json.loads(search_res.stdout)["symbols"]
    assert [s["name"] for s in search_syms] == ["check_ci"]
