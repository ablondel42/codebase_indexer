"""Comprehensive tests for all grouped ``rc`` CLI commands (ASCII + JSON)."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"


def _run_rc(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``python -m repo_context.cli.main`` with ``src/`` prioritized on ``PYTHONPATH``."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return subprocess.run(
        [sys.executable, "-m", "repo_context.cli.main", *args],
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
        "def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    db_path = tmp_path / "cli_test.db"
    return repo_root, db_path


@pytest.fixture
def indexed_workspace(cli_workspace: tuple[Path, Path]) -> tuple[Path, Path]:
    """Run ``rc index`` on ``cli_workspace`` and return ``(repo_root, db_path)``."""
    repo_root, db_path = cli_workspace
    res = _run_rc("index", str(repo_root), "--db-path", str(db_path), "--json", cwd=repo_root)
    assert res.returncode == 0, res.stderr
    return repo_root, db_path


def test_rc_index_initial_incremental_force_and_include_tests(
    cli_workspace: tuple[Path, Path],
) -> None:
    """`rc index` handles initial, unchanged incremental, --force, and --include-tests."""
    repo_root, db_path = cli_workspace

    init_res = _run_rc("index", str(repo_root), "--db-path", str(db_path), "--json", cwd=repo_root)
    assert init_res.returncode == 0, init_res.stderr
    init_data = json.loads(init_res.stdout)
    assert init_data["files_scanned"] == 3
    assert init_data["files_indexed"] == 3
    assert init_data["linker"]["total_resolved"] >= 3

    inc_res = _run_rc("index", str(repo_root), "--db-path", str(db_path), "--json", cwd=repo_root)
    assert inc_res.returncode == 0
    inc_data = json.loads(inc_res.stdout)
    assert inc_data["files_indexed"] == 0
    assert inc_data["files_unchanged"] == 3

    force_res = _run_rc(
        "index", str(repo_root), "--db-path", str(db_path), "--force", "--include-tests", "--json", cwd=repo_root
    )
    assert force_res.returncode == 0
    force_data = json.loads(force_res.stdout)
    assert force_data["files_scanned"] == 4
    assert force_data["files_indexed"] == 4


def test_rc_db_subcommands_and_read_only_sql_guard(
    indexed_workspace: tuple[Path, Path],
) -> None:
    """`rc db {status,repos,tables,runs,sql}` work in ASCII & JSON and block SQL mutations."""
    repo_root, db_path = indexed_workspace

    for sub in ("status", "repos", "tables", "runs"):
        ascii_res = _run_rc("db", sub, "--db-path", str(db_path), cwd=repo_root)
        assert ascii_res.returncode == 0, f"db {sub} failed: {ascii_res.stderr}"
        json_res = _run_rc("db", sub, "--db-path", str(db_path), "--json", cwd=repo_root)
        assert json_res.returncode == 0, json_res.stderr
        parsed = json.loads(json_res.stdout)
        assert isinstance(parsed, dict)

    # SELECT and PRAGMA allowed
    select_res = _run_rc(
        "db", "sql", "SELECT name, kind FROM nodes WHERE kind = 'class' ORDER BY name",
        "--db-path", str(db_path), "--json", cwd=repo_root
    )
    assert select_res.returncode == 0, select_res.stderr
    rows = json.loads(select_res.stdout)["rows"]
    assert rows == [{"name": "Calculator", "kind": "class"}]

    select_ascii = _run_rc(
        "db", "sql", "SELECT name FROM nodes WHERE kind = 'class'",
        "--db-path", str(db_path), cwd=repo_root
    )
    assert select_ascii.returncode == 0
    assert "Calculator" in select_ascii.stdout

    # Write/DDL mutations MUST be blocked by SQLite authorizer
    for forbidden_sql in (
        "DELETE FROM nodes",
        "DROP TABLE nodes",
        "UPDATE nodes SET name = 'hacked'",
        "INSERT INTO repos (id, root_path, name, default_language, created_at) VALUES ('a','b','c','python','2026-01-01T00:00:00Z')",
    ):
        bad_res = _run_rc("db", "sql", forbidden_sql, "--db-path", str(db_path), cwd=repo_root)
        assert bad_res.returncode != 0, f"Expected failure for {forbidden_sql}"


def test_rc_files_and_outline_ascii_and_json(
    indexed_workspace: tuple[Path, Path],
) -> None:
    """`rc files` reflects live disk freshness and `rc outline` renders AST hierarchy."""
    repo_root, db_path = indexed_workspace

    files_ascii = _run_rc("files", "--db-path", str(db_path), cwd=repo_root)
    assert files_ascii.returncode == 0
    assert "pkg/math_ops.py" in files_ascii.stdout
    assert "fresh" in files_ascii.stdout

    # Modify one file on disk and verify rc files detects 'modified'
    math_file = repo_root / "pkg" / "math_ops.py"
    math_file.write_text(math_file.read_text(encoding="utf-8") + "\n# modified\n", encoding="utf-8")
    files_json = _run_rc("files", "math_ops", "--db-path", str(db_path), "--json", cwd=repo_root)
    assert files_json.returncode == 0
    entries = json.loads(files_json.stdout)["files"]
    assert len(entries) == 1
    assert entries[0]["freshness"] == "modified"

    outline_ascii = _run_rc("outline", "pkg/math_ops.py", "--no-private", "--db-path", str(db_path), cwd=repo_root)
    assert outline_ascii.returncode == 0, outline_ascii.stderr
    assert "sym:" in outline_ascii.stdout
    assert "Calculator" in outline_ascii.stdout
    assert "_internal" not in outline_ascii.stdout

    outline_json = _run_rc("outline", "pkg/math_ops.py", "--db-path", str(db_path), "--json", cwd=repo_root)
    assert outline_json.returncode == 0
    assert json.loads(outline_json.stdout)["module_path"] == "pkg.math_ops"


def test_rc_symbols_source_refs_risk_and_mcp_call(
    indexed_workspace: tuple[Path, Path],
) -> None:
    """`rc symbols`, `rc source`, `rc refs`, `rc risk`, and `rc mcp-call` emit valid ASCII and JSON."""
    repo_root, db_path = indexed_workspace

    # symbols search & list (ASCII asserts untruncated sym:... IDs present)
    search_ascii = _run_rc("symbols", "search", "Calculator", "--db-path", str(db_path), cwd=repo_root)
    assert search_ascii.returncode == 0
    assert "sym:" in search_ascii.stdout
    assert "pkg.math_ops.Calculator" in search_ascii.stdout

    list_json = _run_rc(
        "symbols", "list", "--kind", "module", "--db-path", str(db_path), "--json", cwd=repo_root
    )
    assert list_json.returncode == 0
    modules = json.loads(list_json.stdout)["symbols"]
    assert len(modules) == 3
    mod_id = modules[0]["id"]

    # symbols show on module (structural_parent is None) must not crash in ASCII or JSON
    show_mod_ascii = _run_rc("symbols", "show", mod_id, "--db-path", str(db_path), cwd=repo_root)
    assert show_mod_ascii.returncode == 0, show_mod_ascii.stderr
    assert mod_id in show_mod_ascii.stdout
    assert "Structural Parent : None" in show_mod_ascii.stdout

    show_fn_ascii = _run_rc(
        "symbols", "show", "pkg.math_ops.add", "--source", "--db-path", str(db_path), cwd=repo_root
    )
    assert show_fn_ascii.returncode == 0
    assert "def add(" in show_fn_ascii.stdout

    # rc source
    src_ascii = _run_rc("source", "pkg.math_ops.add", "--db-path", str(db_path), cwd=repo_root)
    assert src_ascii.returncode == 0
    assert "sym:" in src_ascii.stdout
    assert "return a + b" in src_ascii.stdout

    src_json = _run_rc("source", "pkg.math_ops.add", "--db-path", str(db_path), "--json", cwd=repo_root)
    assert src_json.returncode == 0
    assert "def add(" in json.loads(src_json.stdout)["source"]

    # rc refs
    refs_ascii = _run_rc(
        "refs", "pkg.math_ops.add", "--direction", "incoming", "--db-path", str(db_path), cwd=repo_root
    )
    assert refs_ascii.returncode == 0
    assert "sym:" in refs_ascii.stdout
    assert "pkg.runner.run_calc" in refs_ascii.stdout

    refs_json = _run_rc(
        "refs", "pkg.math_ops.add", "--direction", "incoming", "--db-path", str(db_path), "--json", cwd=repo_root
    )
    assert refs_json.returncode == 0
    assert json.loads(refs_json.stdout)["total_count"] >= 2

    # rc risk
    risk_ascii = _run_rc(
        "risk", "pkg.math_ops.add", "pkg.runner.run_calc", "--db-path", str(db_path), cwd=repo_root
    )
    assert risk_ascii.returncode == 0
    assert "sym:" in risk_ascii.stdout
    assert re.search(r"Targets Multi-Module\s+:\s+True", risk_ascii.stdout)

    risk_json = _run_rc("risk", "pkg.math_ops.add", "--db-path", str(db_path), "--json", cwd=repo_root)
    assert risk_json.returncode == 0
    assert json.loads(risk_json.stdout)["facts"]["cross_module_impact"] is True

    # rc watch / serve help & rc mcp-call
    assert _run_rc("watch", "--help").returncode == 0
    assert _run_rc("serve", "--help").returncode == 0
    mcp_res = _run_rc(
        "mcp-call", "get_repo_overview", "{}", "--db-path", str(db_path), "--json", cwd=repo_root
    )
    assert mcp_res.returncode == 0, mcp_res.stderr
    assert json.loads(mcp_res.stdout)["result"]["ok"] is True


def test_rc_symbols_list_and_search_preserve_dot_prefix_paths(tmp_path: Path) -> None:
    """`rc symbols list/search --file .github/` must preserve leading dots without stripping."""
    repo_root = tmp_path / "dot_cli_repo"
    (repo_root / ".github").mkdir(parents=True)
    (repo_root / "github").mkdir()
    (repo_root / ".github" / "ci.py").write_text(
        "def check_ci() -> int:\n    return 1\n", encoding="utf-8"
    )
    (repo_root / "github" / "ci.py").write_text(
        "def check_non_dot() -> int:\n    return 2\n", encoding="utf-8"
    )

    db_path = tmp_path / "dot_cli.db"
    idx_res = _run_rc("index", str(repo_root), "--db-path", str(db_path), "--json", cwd=repo_root)
    assert idx_res.returncode == 0, idx_res.stderr

    for file_arg in (".github/", "./.github/", "./.github/ci.py"):
        list_res = _run_rc(
            "symbols",
            "list",
            "--file",
            file_arg,
            "--db-path",
            str(db_path),
            "--json",
            cwd=repo_root,
        )
        assert list_res.returncode == 0, list_res.stderr
        syms = json.loads(list_res.stdout)["symbols"]
        assert syms
        assert all(s["file_path"] == ".github/ci.py" for s in syms)
        assert any(s["name"] == "check_ci" for s in syms)

    search_res = _run_rc(
        "symbols",
        "search",
        "check",
        "--file",
        ".github",
        "--db-path",
        str(db_path),
        "--json",
        cwd=repo_root,
    )
    assert search_res.returncode == 0, search_res.stderr
    search_syms = json.loads(search_res.stdout)["symbols"]
    assert [s["name"] for s in search_syms] == ["check_ci"]
