"""Unit tests for grouped ``cbi`` CLI parsing, ``CliApplication``, and missing-DB safety."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli.main import main
from cli.services.app import CliApplication
from cli.services.parser import CliParserBuilder
from core.models.config import AppConfig
from core.models.enums import SymbolKind
from core.utils.logging import reset_logging
from engine.models.queries import ListSymbolsQuery
from engine.services.facade import IndexerEngine


@pytest.fixture(autouse=True)
def _reset_cli_logging() -> Iterator[None]:
    """Reset the ``indexer`` root logger before and after every CLI invocation test."""
    reset_logging()
    yield
    reset_logging()


def test_build_parser_supports_root_and_trailing_db_path_and_rejects_json_flag(
    tmp_path: Path,
) -> None:
    """``--db-path`` works before and after subcommands; legacy ``--json`` flag is rejected."""
    parser = CliParserBuilder().build()
    db_file = tmp_path / "custom.db"

    ns_before = parser.parse_args(["--db-path", str(db_file), "db", "status"])
    assert ns_before.db_path == db_file

    ns_after = parser.parse_args(["db", "status", "--db-path", str(db_file)])
    assert ns_after.db_path == db_file

    with pytest.raises(SystemExit):
        parser.parse_args(["db", "status", "--json"])


def test_missing_db_path_fails_without_creating_zero_byte_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read-only CLI commands fail with JSON error on missing ``--db-path`` and create 0 files."""
    missing_db = tmp_path / "does_not_exist.db"
    invocations = [
        ["db", "status", "--db-path", str(missing_db)],
        ["db", "repos", "--db-path", str(missing_db)],
        ["db", "tables", "--db-path", str(missing_db)],
        ["db", "runs", "--db-path", str(missing_db)],
        ["db", "sql", "SELECT 1", "--db-path", str(missing_db)],
        ["files", "--db-path", str(missing_db)],
        ["outline", "app.py", "--db-path", str(missing_db)],
        ["symbols", "list", "--db-path", str(missing_db)],
        ["symbols", "search", "foo", "--db-path", str(missing_db)],
        ["symbols", "show", "foo", "--db-path", str(missing_db)],
        ["source", "foo", "--db-path", str(missing_db)],
        ["refs", "foo", "--db-path", str(missing_db)],
        ["risk", "foo", "--db-path", str(missing_db)],
    ]
    for argv in invocations:
        rc = main(argv)
        assert rc == 1
        assert not missing_db.exists(), f"{argv} created {missing_db}!"
        captured = capsys.readouterr()
        assert captured.out == ""
        err = json.loads(captured.err)
        assert err["ok"] is False
        assert err["error"]["code"] == "database_not_found"
        assert "cbi index" in err["error"]["message"]


def test_symbols_show_module_with_none_structural_parent_and_ambiguous_json_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``cbi symbols show`` outputs JSON and emits structured candidates on ambiguity."""
    repo_root = tmp_path / "sample"
    repo_root.mkdir()
    (repo_root / "mod.py").write_text(
        '"""Sample module."""\n\ndef greet(name: str) -> str:\n    return name\n',
        encoding="utf-8",
    )
    (repo_root / "other.py").write_text(
        '"""Other module."""\n\ndef greet(name: str) -> str:\n    return name\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "cli.db"
    with IndexerEngine(AppConfig(db_path=db_path)) as engine:
        engine.commands.index_repository(repo_root)
        modules = engine.queries.list_symbols(ListSymbolsQuery(kind=SymbolKind.MODULE)).symbols
        assert len(modules) == 2
        mod_id = modules[0].id

        app = CliApplication(engine=engine)
        assert app.run(["symbols", "show", mod_id, "--db-path", str(db_path)]) == 0
        json_out = json.loads(capsys.readouterr().out)
        assert json_out["focus_symbol"]["id"] == mod_id
        assert json_out["structural_parent"] is None

        assert app.run(["symbols", "show", "greet", "--db-path", str(db_path)]) == 1
        ambig_captured = capsys.readouterr()
        assert ambig_captured.out == ""
        ambig_err = json.loads(ambig_captured.err)
        assert ambig_err["ok"] is False
        assert ambig_err["error"]["code"] == "ambiguous_symbol"
        assert len(ambig_err["error"]["details"]["candidates"]) == 2
