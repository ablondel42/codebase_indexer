"""Grouped `rc` CLI entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import logging
import os
from pathlib import Path
import shlex
import sys
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from repo_context import __version__
from repo_context.constants import VALID_SYMBOL_KINDS
from repo_context.exceptions import DatabaseNotFoundError, RepoContextError
import repo_context.logging_config as logging_config
from repo_context.logging_config import get_logger, setup_logging
from repo_context.storage import connection_scope, list_repos, resolve_repo_id
from repo_context.cli.commands import (
    cmd_db_repos,
    cmd_db_runs,
    cmd_db_sql,
    cmd_db_status,
    cmd_db_tables,
    cmd_files,
    cmd_index,
    cmd_mcp_call,
    cmd_outline,
    cmd_refs,
    cmd_risk,
    cmd_serve,
    cmd_source,
    cmd_symbols_list,
    cmd_symbols_search,
    cmd_symbols_show,
    cmd_watch,
)
from repo_context.cli.utils import resolve_db_path

_KNOWN_COMMANDS = frozenset(
    {
        "index",
        "db",
        "files",
        "outline",
        "symbols",
        "source",
        "refs",
        "risk",
        "shell",
        "watch",
        "serve",
        "mcp-call",
    }
)
_REPO_SCOPED_ROOT = frozenset({"files", "outline", "source", "refs", "risk", "mcp-call"})
_REPO_SCOPED_SUBS = {("db", "status"), ("db", "runs"), ("symbols", "search"), ("symbols", "list"), ("symbols", "show")}

logger = get_logger("cli.main")


def _make_subcommand_common_parser() -> argparse.ArgumentParser:
    """Create shared parent flags for subcommands using SUPPRESS so root flags are never clobbered."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db-path",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to SQLite database file (default: repo_context.db)",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    """Build the grouped ``rc`` CLI argument parser."""
    sub_common = _make_subcommand_common_parser()

    parser = argparse.ArgumentParser(
        prog="rc",
        description="Repository intelligence CLI and token-lean MCP server",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to SQLite database file (default: repo_context.db)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable debug logging to stderr",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug logging to stderr",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # rc index
    index_parser = subparsers.add_parser(
        "index",
        parents=[sub_common],
        help="Scan, extract AST/calls, link edges, and optionally enrich LSP references",
    )
    index_parser.add_argument(
        "repo_path",
        type=Path,
        nargs="?",
        default=None,
        help="Path to repository root (default: current working directory)",
    )
    index_parser.add_argument(
        "--include-tests",
        action="store_true",
        default=False,
        help="Include test files under test directories",
    )
    index_parser.add_argument(
        "--lsp",
        action="store_true",
        default=False,
        help="Enrich LSP references after AST indexing",
    )
    index_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-extract all files even if content hashes are unchanged",
    )
    index_parser.add_argument(
        "--shell",
        action="store_true",
        default=False,
        help="Start interactive `rc>` CLI shell immediately after indexing",
    )
    index_parser.set_defaults(func=cmd_index)

    # rc db ...
    db_parser = subparsers.add_parser(
        "db",
        parents=[sub_common],
        help="Inspect SQLite database state, repositories, tables, runs, and read-only SQL",
    )
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)

    db_status_parser = db_subparsers.add_parser(
        "status",
        parents=[sub_common],
        help="Show DB size, schema version, table counts, edge breakdown, LSP coverage, and latest run",
    )
    db_status_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    db_status_parser.set_defaults(func=cmd_db_status)

    db_repos_parser = db_subparsers.add_parser(
        "repos",
        parents=[sub_common],
        help="List indexed repositories",
    )
    db_repos_parser.set_defaults(func=cmd_db_repos)

    db_tables_parser = db_subparsers.add_parser(
        "tables",
        parents=[sub_common],
        help="List SQLite tables and exact row counts",
    )
    db_tables_parser.set_defaults(func=cmd_db_tables)

    db_runs_parser = db_subparsers.add_parser(
        "runs",
        parents=[sub_common],
        help="List recent indexing runs",
    )
    db_runs_parser.add_argument("--repo", type=str, default=None, help="Filter by repository ID, name, or path")
    db_runs_parser.add_argument("--limit", type=int, default=20, help="Maximum runs to show (default: 20)")
    db_runs_parser.set_defaults(func=cmd_db_runs)

    db_sql_parser = db_subparsers.add_parser(
        "sql",
        parents=[sub_common],
        help="Run a strictly read-only SELECT/PRAGMA query against the SQLite database",
    )
    db_sql_parser.add_argument("query", type=str, help="Read-only SQL SELECT/PRAGMA query")
    db_sql_parser.add_argument("--limit", type=int, default=100, help="Maximum rows to display (default: 100)")
    db_sql_parser.set_defaults(func=cmd_db_sql)

    # rc files
    files_parser = subparsers.add_parser(
        "files",
        parents=[sub_common],
        help="List indexed files with line count, symbol count, and live disk freshness",
    )
    files_parser.add_argument(
        "pattern",
        type=str,
        nargs="?",
        default=None,
        help="Optional substring or glob filter on file/module path",
    )
    files_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    files_parser.add_argument(
        "--include-tests",
        action="store_true",
        default=False,
        help="Include indexed test files",
    )
    files_parser.add_argument("--limit", type=int, default=50, help="Maximum files to show (default: 50)")
    files_parser.set_defaults(func=cmd_files)

    # rc outline
    outline_parser = subparsers.add_parser(
        "outline",
        parents=[sub_common],
        help="Show hierarchical symbol outline for an indexed file",
    )
    outline_parser.add_argument("file_path", type=str, help="Repository-relative file path")
    outline_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    outline_parser.add_argument(
        "--no-private",
        action="store_true",
        default=False,
        help="Exclude private and function-local symbols",
    )
    outline_parser.add_argument("--max-depth", type=int, default=3, help="Maximum symbol tree depth (default: 3)")
    outline_parser.set_defaults(func=cmd_outline)

    # rc symbols ...
    symbols_parser = subparsers.add_parser(
        "symbols",
        parents=[sub_common],
        help="Search, browse, and inspect indexed symbols",
    )
    symbols_subparsers = symbols_parser.add_subparsers(dest="symbols_command", required=True)
    kind_choices = sorted(VALID_SYMBOL_KINDS)

    sym_search_parser = symbols_subparsers.add_parser(
        "search",
        parents=[sub_common],
        help="Search symbols via FTS5 with exact/prefix ranking and LIKE fallback",
    )
    sym_search_parser.add_argument("query", type=str, help="Search query")
    sym_search_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    sym_search_parser.add_argument("--kind", type=str, choices=kind_choices, default=None, help="Filter by symbol kind")
    sym_search_parser.add_argument("--file", type=str, default=None, help="Filter by file or directory prefix")
    sym_search_parser.add_argument("--limit", type=int, default=20, help="Maximum results (default: 20)")
    sym_search_parser.set_defaults(func=cmd_symbols_search)

    sym_list_parser = symbols_subparsers.add_parser(
        "list",
        parents=[sub_common],
        help="Browse indexed symbols with pagination",
    )
    sym_list_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    sym_list_parser.add_argument("--kind", type=str, choices=kind_choices, default=None, help="Filter by symbol kind")
    sym_list_parser.add_argument("--file", type=str, default=None, help="Filter by file or directory prefix")
    sym_list_parser.add_argument("--limit", type=int, default=50, help="Maximum symbols to show (default: 50)")
    sym_list_parser.add_argument("--offset", type=int, default=0, help="Pagination offset (default: 0)")
    sym_list_parser.set_defaults(func=cmd_symbols_list)

    sym_show_parser = symbols_subparsers.add_parser(
        "show",
        parents=[sub_common],
        help="Show full symbol context including hierarchy, relationships, and freshness",
    )
    sym_show_parser.add_argument("id_or_name", type=str, help="Symbol ID (sym:...), qualified name, or short name")
    sym_show_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    sym_show_parser.add_argument(
        "--source",
        action="store_true",
        default=False,
        help="Include bounded line-numbered symbol source snippet",
    )
    sym_show_parser.set_defaults(func=cmd_symbols_show)

    # rc source
    source_parser = subparsers.add_parser(
        "source",
        parents=[sub_common],
        help="Print line-numbered source snippet for a symbol",
    )
    source_parser.add_argument("id_or_name", type=str, help="Symbol ID (sym:...), qualified name, or short name")
    source_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    source_parser.add_argument("--max-lines", type=int, default=120, help="Maximum lines to print (default: 120)")
    source_parser.add_argument("--offset", type=int, default=0, help="Line offset within symbol (default: 0)")
    source_parser.set_defaults(func=cmd_source)

    # rc refs
    refs_parser = subparsers.add_parser(
        "refs",
        parents=[sub_common],
        help="Show incoming/outgoing calls and references with 1-line previews",
    )
    refs_parser.add_argument("id_or_name", type=str, help="Symbol ID (sym:...), qualified name, or short name")
    refs_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    refs_parser.add_argument(
        "--direction",
        type=str,
        choices=["incoming", "outgoing", "both"],
        default="incoming",
        help="Edge direction to inspect (default: incoming)",
    )
    refs_parser.add_argument(
        "--refresh",
        action="store_true",
        default=False,
        help="Refresh LSP references via pyright-langserver before querying",
    )
    refs_parser.add_argument("--limit", type=int, default=50, help="Maximum edges to display (default: 50)")
    refs_parser.add_argument("--offset", type=int, default=0, help="Pagination offset (default: 0)")
    refs_parser.set_defaults(func=cmd_refs)

    # rc risk
    risk_parser = subparsers.add_parser(
        "risk",
        parents=[sub_common],
        help="Analyze change impact and risk for one or more symbols",
    )
    risk_parser.add_argument(
        "id_or_names",
        type=str,
        nargs="+",
        help="One or more symbol IDs, qualified names, or unique short names",
    )
    risk_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    risk_parser.set_defaults(func=cmd_risk)

    # rc shell
    shell_parser = subparsers.add_parser(
        "shell",
        parents=[sub_common],
        help="Start interactive `rc>` shell to inspect an indexed database",
    )
    shell_parser.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
    shell_parser.set_defaults(func=_cmd_shell)

    # rc watch
    watch_parser = subparsers.add_parser(
        "watch",
        parents=[sub_common],
        help="Watch a repository for changes and incrementally update the index",
    )
    watch_parser.add_argument(
        "repo_path",
        type=Path,
        nargs="?",
        default=None,
        help="Path to repository root (default: current working directory)",
    )
    watch_parser.add_argument(
        "--debounce-ms",
        type=int,
        default=500,
        help="Debounce window in milliseconds (default: 500)",
    )
    watch_parser.add_argument(
        "--include-tests",
        action="store_true",
        default=False,
        help="Watch and reindex test files",
    )
    watch_parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable verbose debug logging to stderr",
    )
    watch_parser.set_defaults(func=cmd_watch)

    # rc serve
    serve_parser = subparsers.add_parser(
        "serve",
        parents=[sub_common],
        help="Start the token-lean MCP server on stdio transport",
    )
    serve_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable debug logging to stderr",
    )
    serve_parser.set_defaults(func=cmd_serve)

    # rc mcp-call
    mcp_call_parser = subparsers.add_parser(
        "mcp-call",
        parents=[sub_common],
        help="Execute an MCP tool locally and show payload byte/token metrics",
    )
    mcp_call_parser.add_argument("tool_name", type=str, help="MCP tool name")
    mcp_call_parser.add_argument(
        "json_args",
        type=str,
        nargs="*",
        default=[],
        help=(
            "JSON object (e.g. '{\"symbol\": \"Foo\"}') or shorthand arg(s) "
            "(e.g. Foo include_source=true)"
        ),
    )
    mcp_call_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Repository ID, name, or root path",
    )
    mcp_call_parser.set_defaults(func=cmd_mcp_call)

    return parser


def _print_cli_error(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an indented JSON error object to stderr."""
    payload = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": details,
        },
    }
    print(json.dumps(payload, indent=2), file=sys.stderr)


def dispatch_args(args: argparse.Namespace) -> int:
    """Execute parsed CLI arguments with consistent structured JSON error handling."""
    try:
        return int(args.func(args))
    except DatabaseNotFoundError as exc:
        _print_cli_error(
            exc.error_code,
            f"{exc}. Run `rc index` first.",
            details=exc.context,
        )
        return 1
    except RepoContextError as exc:
        _print_cli_error(exc.error_code, exc.message, details=exc.context)
        return 1
    except (
        FileNotFoundError,
        NotADirectoryError,
        SQLAlchemyError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        _print_cli_error("error", str(exc))
        return 1
    except BrokenPipeError:
        try:
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull_fd, sys.stdout.fileno())
            finally:
                os.close(devnull_fd)
        except OSError:
            pass
        return 0
    except KeyboardInterrupt:
        _print_cli_error("interrupted", "Interrupted.")
        return 130
    except Exception as exc:
        logger.exception("CLI command failed")
        _print_cli_error("internal_error", str(exc))
        return 1


def _preprocess_argv(tokens: list[str]) -> list[str]:
    """Rewrite ``demo`` or ``<repo_path>`` shorthand into ``index`` arguments."""
    if not tokens:
        return tokens
    first = tokens[0]
    if first != "demo" and (first.startswith("-") or first in _KNOWN_COMMANDS):
        return tokens

    is_demo = first == "demo"
    target = "." if is_demo else first
    rest = tokens[1:]
    use_shell = "--no-cli" not in rest
    cleaned = [t for t in rest if t not in ("--cli", "--no-cli")]

    out = ["index", target]
    if is_demo and "--include-tests" not in cleaned:
        out.append("--include-tests")
    if use_shell and "--shell" not in cleaned:
        out.append("--shell")
    out.extend(cleaned)
    return out


def _run_shell(db_path: Path, repo_arg: str | None = None) -> int:
    """Run a compact interactive ``rc>`` REPL over the existing CLI parser."""
    with connection_scope(db_path, read_only=True) as conn:
        repos = list_repos(conn)
        if repo_arg:
            active_repo: str | None = resolve_repo_id(conn, repo_arg)
        elif len(repos) == 1:
            active_repo = repos[0].id
        else:
            active_repo = None

    print(
        f"Interactive rc shell (db: {db_path}, repo: {active_repo or 'auto'}). "
        "Type 'help' or 'exit'."
    )
    parser = build_parser()

    while True:
        try:
            if sys.stdin.isatty():
                line = input("rc> ")
            else:
                sys.stdout.write("rc> ")
                sys.stdout.flush()
                raw = sys.stdin.readline()
                if raw == "":
                    break
                line = raw.rstrip("\r\n")
        except (EOFError, BrokenPipeError):
            break
        except KeyboardInterrupt:
            print("", file=sys.stderr)
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError as exc:
            _print_cli_error("parse_error", str(exc))
            continue
        if tokens[0] == "rc" and len(tokens) > 1:
            tokens = tokens[1:]
        if tokens[0] in ("exit", "quit", "q"):
            break
        if tokens[0] in ("help", "?"):
            parser.print_help()
            continue
        if tokens[0] == "shell":
            _print_cli_error("invalid_command", "Already inside rc shell.")
            continue

        if (
            len(tokens) >= 3
            and tokens[:2] == ["db", "sql"]
            and not any(t.startswith("-") for t in tokens[2:])
        ):
            tokens = ["db", "sql", " ".join(tokens[2:])]

        if not any(t in ("-h", "--help", "--version") for t in tokens):
            if not any(t == "--db-path" or t.startswith("--db-path=") for t in tokens):
                tokens.extend(["--db-path", str(db_path)])
            takes_repo = tokens[0] in _REPO_SCOPED_ROOT or (
                len(tokens) >= 2 and (tokens[0], tokens[1]) in _REPO_SCOPED_SUBS
            )
            if (
                active_repo
                and takes_repo
                and not any(t == "--repo" or t.startswith("--repo=") for t in tokens)
            ):
                tokens.extend(["--repo", active_repo])

        try:
            parsed = parser.parse_args(tokens)
        except SystemExit:
            continue
        dispatch_args(parsed)

    return 0


def _cmd_shell(args: argparse.Namespace) -> int:
    """Start the interactive ``rc>`` shell for an existing indexed database."""
    return _run_shell(resolve_db_path(args), getattr(args, "repo", None))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI main entry point."""
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_preprocess_argv(raw_argv))

    is_debug = bool(getattr(args, "debug", False) or getattr(args, "verbose", False))
    if is_debug:
        if not logging_config.LOGGING_INITIALIZED:
            setup_logging(level="DEBUG")
    else:
        root_logger = logging.getLogger("repo_context")
        if not root_logger.handlers:
            root_logger.addHandler(logging.NullHandler())

    rc = dispatch_args(args)
    if rc == 0 and getattr(args, "shell", False):
        repo_arg = getattr(args, "repo_path", None)
        return _run_shell(
            resolve_db_path(args),
            str(Path(repo_arg).resolve()) if repo_arg else str(Path.cwd().resolve()),
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
