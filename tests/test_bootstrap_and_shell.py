"""Tests for root `./rc` bootstrapper script and compact interactive `rc>` CLI shell."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo_context.cli.main import _preprocess_argv, _run_shell, main
from repo_context.exceptions import DatabaseNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RC_SCRIPT = PROJECT_ROOT / "rc"


def _make_sample_repo(root: Path) -> Path:
    repo = root / "sample_repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text('"""Sample pkg."""\n', encoding="utf-8")
    (repo / "pkg" / "greeter.py").write_text(
        '"""Greeter module."""\n\n\n'
        "def hello(name: str) -> str:\n"
        '    """Return greeting."""\n'
        '    return f"hi {name}"\n',
        encoding="utf-8",
    )
    return repo


def _run_root_rc(*args: str, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["RC_NO_BOOTSTRAP"] = "1"
    env["RC_PYTHON"] = sys.executable
    return subprocess.run(
        [str(RC_SCRIPT), *args],
        input=stdin_text,
        text=True,
        capture_output=True,
        cwd=PROJECT_ROOT,
        env=env,
        check=False,
    )


def test_preprocess_argv_shorthand() -> None:
    """`demo` and `<repo>` shorthand rewrite cleanly into `index` + `--shell`."""
    assert _preprocess_argv(["demo"]) == ["index", ".", "--include-tests", "--shell"]
    assert _preprocess_argv(["demo", "--cli", "--db-path", "x.db"]) == [
        "index",
        ".",
        "--include-tests",
        "--shell",
        "--db-path",
        "x.db",
    ]
    assert _preprocess_argv(["/tmp/repo", "--no-cli"]) == ["index", "/tmp/repo"]
    assert _preprocess_argv(["db", "status"]) == ["db", "status"]


def test_shell_missing_db_raises_without_creating_file(tmp_path: Path) -> None:
    """`_run_shell` against a missing DB fails cleanly without creating a file."""
    missing = tmp_path / "missing.db"
    with pytest.raises(DatabaseNotFoundError):
        _run_shell(missing)
    assert not missing.exists()


def test_interactive_shell_commands_and_error_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Interactive `rc>` REPL executes queries, unquoted SQL, and survives errors."""
    repo_root = _make_sample_repo(tmp_path)
    db_path = tmp_path / "shell.db"
    assert main(["index", str(repo_root), "--db-path", str(db_path)]) == 0
    capsys.readouterr()

    commands = "\n".join(
        [
            "help",
            "db status",
            "db sql SELECT kind, COUNT(*) AS n FROM nodes GROUP BY kind ORDER BY n DESC",
            "db sql DELETE FROM nodes",
            "outline pkg/greeter.py",
            "symbols search hello",
            "source pkg.greeter.hello",
            "mcp-call get_symbol_context hello",
            "mcp-call get_symbol_context hello include_source=true",
            "unknown-command",
            "shell",
            "exit",
        ]
    ) + "\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(commands))

    assert _run_shell(db_path) == 0
    captured = capsys.readouterr()
    assert "Interactive rc shell" in captured.out
    assert '"schema_ok": true' in captured.out
    assert "def hello(name: str) -> str" in captured.out
    assert 'return f\\"hi {name}\\"' in captured.out
    assert '"tool": "get_symbol_context"' in captured.out
    assert '"qualified_name": "pkg.greeter.hello"' in captured.out
    assert "not authorized" in captured.err
    assert "Already inside rc shell." in captured.err


def test_root_rc_modes_and_tests_runner(tmp_path: Path) -> None:
    """`./rc`, `./rc <repo> [--cli|--no-cli]`, `./rc demo`, subcommands, and `./rc tests` work."""
    assert RC_SCRIPT.is_file() and os.access(RC_SCRIPT, os.X_OK)

    usage = _run_root_rc()
    assert usage.returncode == 0
    assert "./rc path/to/repo [--cli]" in usage.stdout
    assert "./rc demo [--cli]" in usage.stdout
    assert "./rc tests" in usage.stdout

    repo_root = _make_sample_repo(tmp_path)
    db_path = tmp_path / "cli_mode.db"

    no_cli = _run_root_rc(str(repo_root), "--no-cli", "--db-path", str(db_path))
    assert no_cli.returncode == 0, no_cli.stderr
    no_cli_data = json.loads(no_cli.stdout)
    assert no_cli_data["files_indexed"] == 2
    assert "Interactive rc shell" not in no_cli.stdout

    for extra in ((), ("--cli",)):
        res = _run_root_rc(
            str(repo_root),
            *extra,
            "--db-path",
            str(db_path),
            stdin_text="symbols search hello\nexit\n",
        )
        assert res.returncode == 0, res.stderr
        assert "Interactive rc shell" in res.stdout
        assert "pkg.greeter.hello" in res.stdout

    demo_db = tmp_path / "demo.db"
    demo = _run_root_rc(
        "demo", "--cli", "--db-path", str(demo_db), stdin_text="db tables\nexit\n"
    )
    assert demo.returncode == 0, demo.stderr
    assert "Interactive rc shell" in demo.stdout
    assert "nodes_fts" in demo.stdout

    fwd = _run_root_rc("db", "tables", "--db-path", str(demo_db))
    assert fwd.returncode == 0, fwd.stderr
    fwd_tables = json.loads(fwd.stdout)["tables"]
    assert any(t["table"] == "reference_refresh" for t in fwd_tables)

    tests_res = _run_root_rc("tests", "-k", "test_import_package_version")
    assert tests_res.returncode == 0, tests_res.stderr
    assert "1 passed" in tests_res.stdout
