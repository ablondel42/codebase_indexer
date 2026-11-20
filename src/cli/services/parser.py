"""Grouped ``cbi`` CLI ``ArgumentParser`` builder service."""

import argparse
from collections.abc import Callable, Mapping
from pathlib import Path

from core.models.enums import APP_VERSION, SymbolKind
from core.utils.logging import get_logger

logger = get_logger("cli.services.parser")


class CliParserBuilder:
    """Builds the grouped ``cbi`` CLI ``ArgumentParser`` and binds subcommand handlers."""

    def __init__(
        self,
        handlers: Mapping[str, Callable[[argparse.Namespace], int]] | None = None,
    ) -> None:
        """Initialize the CLI parser builder with optional subcommand callbacks.

        Args:
            handlers: Optional mapping from dotted handler keys (e.g. ``"db.status"``)
                to callable command handlers.
        """
        self._handlers = dict(handlers) if handlers is not None else {}

    @staticmethod
    def _make_subcommand_common_parser() -> argparse.ArgumentParser:
        """Create shared parent flags using ``SUPPRESS`` so root flags stay intact.

        Returns:
            Parent ``ArgumentParser`` defining trailing ``--db-path``.
        """
        common = argparse.ArgumentParser(add_help=False)
        common.add_argument(
            "--db-path",
            type=Path,
            default=argparse.SUPPRESS,
            help="Path to SQLite database file (default: codebase_indexer.db)",
        )
        return common

    def _bind_handler(self, parser: argparse.ArgumentParser, key: str) -> None:
        """Attach ``handler_key`` and optional ``func`` callback onto ``parser``.

        Args:
            parser: Subcommand ``ArgumentParser`` instance.
            key: Canonical handler lookup key.
        """
        callback = self._handlers.get(key)
        if callback is not None:
            parser.set_defaults(handler_key=key, func=callback)
        else:
            parser.set_defaults(handler_key=key)

    def _add_index_parser(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register the ``cbi index`` subcommand parser.

        Args:
            subparsers: Root subparsers action.
            sub_common: Shared subcommand parent parser.
        """
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
            help="Start interactive `cbi>` CLI shell immediately after indexing",
        )
        self._bind_handler(index_parser, "index")

    def _add_db_inspection_parsers(
        self,
        db_subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register ``cbi db {status,repos,tables}`` subparsers.

        Args:
            db_subparsers: ``db`` group subparsers action.
            sub_common: Shared subcommand parent parser.
        """
        status_parser = db_subparsers.add_parser(
            "status",
            parents=[sub_common],
            help=(
                "Show DB size, schema version, table counts, edge breakdown, "
                "LSP coverage, and latest run"
            ),
        )
        status_parser.add_argument(
            "--repo",
            type=str,
            default=None,
            help="Repository ID, name, or root path",
        )
        self._bind_handler(status_parser, "db.status")

        repos_parser = db_subparsers.add_parser(
            "repos",
            parents=[sub_common],
            help="List indexed repositories",
        )
        self._bind_handler(repos_parser, "db.repos")

        tables_parser = db_subparsers.add_parser(
            "tables",
            parents=[sub_common],
            help="List SQLite tables and exact row counts",
        )
        self._bind_handler(tables_parser, "db.tables")

    def _add_db_query_parsers(
        self,
        db_subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register ``cbi db {runs,sql}`` subparsers.

        Args:
            db_subparsers: ``db`` group subparsers action.
            sub_common: Shared subcommand parent parser.
        """
        runs_parser = db_subparsers.add_parser(
            "runs",
            parents=[sub_common],
            help="List recent indexing runs",
        )
        runs_parser.add_argument(
            "--repo",
            type=str,
            default=None,
            help="Filter by repository ID, name, or path",
        )
        runs_parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Maximum runs to show (default: 20)",
        )
        self._bind_handler(runs_parser, "db.runs")

        sql_parser = db_subparsers.add_parser(
            "sql",
            parents=[sub_common],
            help="Run a strictly read-only SELECT/PRAGMA query against the SQLite database",
        )
        sql_parser.add_argument("query", type=str, help="Read-only SQL SELECT/PRAGMA query")
        sql_parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum rows to display (default: 100)",
        )
        self._bind_handler(sql_parser, "db.sql")

    def _add_db_parser(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register the ``cbi db`` subcommand group and its subcommands.

        Args:
            subparsers: Root subparsers action.
            sub_common: Shared subcommand parent parser.
        """
        db_parser = subparsers.add_parser(
            "db",
            parents=[sub_common],
            help="Inspect SQLite database state, repositories, tables, runs, and read-only SQL",
        )
        db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
        self._add_db_inspection_parsers(db_subparsers, sub_common)
        self._add_db_query_parsers(db_subparsers, sub_common)

    def _add_symbols_search_parser(
        self,
        symbols_subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
        kind_choices: list[str],
    ) -> None:
        """Register ``cbi symbols search`` subparser.

        Args:
            symbols_subparsers: ``symbols`` group subparsers action.
            sub_common: Shared subcommand parent parser.
            kind_choices: Sorted valid ``SymbolKind`` string values.
        """
        search_parser = symbols_subparsers.add_parser(
            "search",
            parents=[sub_common],
            help="Search symbols via FTS5 with exact/prefix ranking and LIKE fallback",
        )
        search_parser.add_argument("query", type=str, help="Search query")
        search_parser.add_argument(
            "--repo",
            type=str,
            default=None,
            help="Repository ID, name, or root path",
        )
        search_parser.add_argument(
            "--kind",
            type=str,
            choices=kind_choices,
            default=None,
            help="Filter by symbol kind",
        )
        search_parser.add_argument(
            "--file",
            type=str,
            default=None,
            help="Filter by file or directory prefix",
        )
        search_parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Maximum results (default: 20)",
        )
        self._bind_handler(search_parser, "symbols.search")

    def _add_symbols_browse_parsers(
        self,
        symbols_subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
        kind_choices: list[str],
    ) -> None:
        """Register ``cbi symbols {list,show}`` subparsers.

        Args:
            symbols_subparsers: ``symbols`` group subparsers action.
            sub_common: Shared subcommand parent parser.
            kind_choices: Sorted valid ``SymbolKind`` string values.
        """
        list_parser = symbols_subparsers.add_parser(
            "list",
            parents=[sub_common],
            help="Browse indexed symbols with pagination",
        )
        list_parser.add_argument(
            "--repo",
            type=str,
            default=None,
            help="Repository ID, name, or root path",
        )
        list_parser.add_argument(
            "--kind",
            type=str,
            choices=kind_choices,
            default=None,
            help="Filter by symbol kind",
        )
        list_parser.add_argument(
            "--file",
            type=str,
            default=None,
            help="Filter by file or directory prefix",
        )
        list_parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum symbols to show (default: 50)",
        )
        list_parser.add_argument(
            "--offset",
            type=int,
            default=0,
            help="Pagination offset (default: 0)",
        )
        self._bind_handler(list_parser, "symbols.list")

        show_parser = symbols_subparsers.add_parser(
            "show",
            parents=[sub_common],
            help="Show full symbol context including hierarchy, relationships, and freshness",
        )
        show_parser.add_argument(
            "id_or_name",
            type=str,
            help="Symbol ID (sym:...), qualified name, or short name",
        )
        show_parser.add_argument(
            "--repo",
            type=str,
            default=None,
            help="Repository ID, name, or root path",
        )
        show_parser.add_argument(
            "--source",
            action="store_true",
            default=False,
            help="Include bounded line-numbered symbol source snippet",
        )
        self._bind_handler(show_parser, "symbols.show")

    def _add_symbols_parser(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register the ``cbi symbols`` subcommand group and its subcommands.

        Args:
            subparsers: Root subparsers action.
            sub_common: Shared subcommand parent parser.
        """
        symbols_parser = subparsers.add_parser(
            "symbols",
            parents=[sub_common],
            help="Search, browse, and inspect indexed symbols",
        )
        symbols_subparsers = symbols_parser.add_subparsers(
            dest="symbols_command",
            required=True,
        )
        kind_choices = sorted(kind.value for kind in SymbolKind)
        self._add_symbols_search_parser(symbols_subparsers, sub_common, kind_choices)
        self._add_symbols_browse_parsers(symbols_subparsers, sub_common, kind_choices)

    def _add_file_parsers(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register ``files``, ``outline``, and ``source`` subcommands.

        Args:
            subparsers: Root subparsers action.
            sub_common: Shared subcommand parent parser.
        """
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
        files_parser.add_argument("--repo", type=str, default=None, help="Repository selector")
        files_parser.add_argument(
            "--include-tests",
            action="store_true",
            default=False,
            help="Include indexed test files",
        )
        files_parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum files to show (default: 50)",
        )
        self._bind_handler(files_parser, "files")

        outline_parser = subparsers.add_parser(
            "outline",
            parents=[sub_common],
            help="Show hierarchical symbol outline for an indexed file",
        )
        outline_parser.add_argument("file_path", type=str, help="Repository-relative file path")
        outline_parser.add_argument("--repo", type=str, default=None, help="Repository selector")
        outline_parser.add_argument(
            "--no-private",
            action="store_true",
            default=False,
            help="Exclude private and function-local symbols",
        )
        outline_parser.add_argument(
            "--max-depth",
            type=int,
            default=3,
            help="Maximum symbol tree depth (default: 3)",
        )
        self._bind_handler(outline_parser, "outline")

        source_parser = subparsers.add_parser(
            "source",
            parents=[sub_common],
            help="Print line-numbered source snippet for a symbol",
        )
        source_parser.add_argument("id_or_name", type=str, help="Symbol ID or name")
        source_parser.add_argument("--repo", type=str, default=None, help="Repository selector")
        source_parser.add_argument(
            "--max-lines",
            type=int,
            default=120,
            help="Maximum lines to print (default: 120)",
        )
        source_parser.add_argument(
            "--offset",
            type=int,
            default=0,
            help="Line offset within symbol (default: 0)",
        )
        self._bind_handler(source_parser, "source")

    def _add_analysis_parsers(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register ``refs`` and ``risk`` subcommands.

        Args:
            subparsers: Root subparsers action.
            sub_common: Shared subcommand parent parser.
        """
        refs_parser = subparsers.add_parser(
            "refs",
            parents=[sub_common],
            help="Show incoming/outgoing calls and references with 1-line previews",
        )
        refs_parser.add_argument(
            "id_or_name",
            type=str,
            help="Symbol ID (sym:...), qualified name, or short name",
        )
        refs_parser.add_argument("--repo", type=str, default=None, help="Repository selector")
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
        refs_parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum edges to display (default: 50)",
        )
        refs_parser.add_argument(
            "--offset",
            type=int,
            default=0,
            help="Pagination offset (default: 0)",
        )
        self._bind_handler(refs_parser, "refs")

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
        risk_parser.add_argument("--repo", type=str, default=None, help="Repository selector")
        self._bind_handler(risk_parser, "risk")

    def _add_daemon_parsers(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register ``shell``, ``watch``, ``serve``, and ``mcp-call`` subcommands.

        Args:
            subparsers: Root subparsers action.
            sub_common: Shared subcommand parent parser.
        """
        shell_parser = subparsers.add_parser(
            "shell",
            parents=[sub_common],
            help="Start interactive `cbi>` shell to inspect an indexed database",
        )
        shell_parser.add_argument("--repo", type=str, default=None, help="Repository selector")
        self._bind_handler(shell_parser, "shell")

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
        self._bind_handler(watch_parser, "watch")

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
        self._bind_handler(serve_parser, "serve")

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
        mcp_call_parser.add_argument("--repo", type=str, default=None, help="Repository selector")
        self._bind_handler(mcp_call_parser, "mcp-call")

    def _add_search_parser(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register top-level ``cbi search`` full-codebase text/regex search subcommand."""
        p = subparsers.add_parser(
            "search",
            parents=[sub_common],
            help="Search all UTF-8 repository text files (literal or regex) with trigram pruning",
        )
        p.add_argument("query", type=str, help="Literal substring or Python regex pattern")
        p.add_argument("--repo", type=str, default=None, help="Repository ID, name, or root path")
        p.add_argument("--regex", action="store_true", default=False, help="Treat query as regex")
        p.add_argument(
            "--case-sensitive",
            action="store_true",
            default=False,
            help="Match case-sensitively (default: case-insensitive)",
        )
        p.add_argument("--path", dest="path_prefix", type=str, default=None, help="Path prefix")
        p.add_argument("--lang", dest="language", type=str, default=None, help="Language filter")
        p.add_argument(
            "--no-tests",
            action="store_true",
            default=False,
            help="Exclude test files from search results",
        )
        p.add_argument("-C", "--context", dest="context_lines", type=int, default=0, help="Context")
        p.add_argument("--limit", type=int, default=50, help="Maximum matches per page (1..200)")
        p.add_argument("--offset", type=int, default=0, help="Match offset (>= 0)")
        self._bind_handler(p, "search")

    def build(self) -> argparse.ArgumentParser:
        """Construct the complete ``cbi`` CLI ``ArgumentParser``.

        Returns:
            Configured root ``ArgumentParser`` instance.
        """
        sub_common = self._make_subcommand_common_parser()
        parser = argparse.ArgumentParser(
            prog="cbi",
            description="Repository intelligence CLI and token-lean MCP server",
        )
        parser.add_argument(
            "--version",
            action="version",
            version=f"%(prog)s {APP_VERSION}",
        )
        parser.add_argument(
            "--db-path",
            type=Path,
            default=None,
            help="Path to SQLite database file (default: codebase_indexer.db)",
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
        self._add_index_parser(subparsers, sub_common)
        self._add_db_parser(subparsers, sub_common)
        self._add_symbols_parser(subparsers, sub_common)
        self._add_search_parser(subparsers, sub_common)
        self._add_file_parsers(subparsers, sub_common)
        self._add_analysis_parsers(subparsers, sub_common)
        self._add_daemon_parsers(subparsers, sub_common)
        return parser


__all__ = [
    "CliParserBuilder",
]
