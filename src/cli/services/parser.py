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
        """Initialize the CLI parser builder with optional subcommand callbacks."""
        self._handlers = dict(handlers) if handlers is not None else {}

    @staticmethod
    def _make_subcommand_common_parser() -> argparse.ArgumentParser:
        """Create shared parent flags using ``SUPPRESS`` so root flags stay intact."""
        common = argparse.ArgumentParser(add_help=False)
        common.add_argument(
            "--db-path",
            type=Path,
            default=argparse.SUPPRESS,
            help="Path to SQLite database file (default: codebase_indexer.db)",
        )
        return common

    @staticmethod
    def _add_repo_arg(
        parser: argparse.ArgumentParser,
        help_text: str = "Repository ID, name, or root path",
    ) -> None:
        """Attach the standard ``--repo`` selector flag to ``parser``."""
        parser.add_argument("--repo", type=str, default=None, help=help_text)

    @staticmethod
    def _add_pagination_args(
        parser: argparse.ArgumentParser,
        *,
        default_limit: int,
        limit_help: str | None = None,
        include_offset: bool = False,
    ) -> None:
        """Attach ``--limit`` (and optional ``--offset``) flags to ``parser``."""
        parser.add_argument(
            "--limit",
            type=int,
            default=default_limit,
            help=limit_help or f"Maximum items to show (default: {default_limit})",
        )
        if include_offset:
            parser.add_argument(
                "--offset",
                type=int,
                default=0,
                help="Pagination offset (default: 0)",
            )

    @classmethod
    def _add_symbol_filter_args(
        cls,
        parser: argparse.ArgumentParser,
        kind_choices: list[str],
    ) -> None:
        """Attach ``--repo``, ``--kind``, and ``--file`` symbol filter flags to ``parser``."""
        cls._add_repo_arg(parser)
        parser.add_argument(
            "--kind",
            type=str,
            choices=kind_choices,
            default=None,
            help="Filter by symbol kind",
        )
        parser.add_argument(
            "--file",
            type=str,
            default=None,
            help="Filter by file or directory prefix",
        )

    def _bind_handler(self, parser: argparse.ArgumentParser, key: str) -> None:
        """Attach ``handler_key`` and optional ``func`` callback onto ``parser``."""
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
        """Register the ``cbi index`` subcommand parser."""
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
        for flag, help_text in (
            ("--include-tests", "Include test files under test directories"),
            ("--lsp", "Enrich LSP references after AST indexing"),
            ("--force", "Re-extract all files even if content hashes are unchanged"),
            ("--shell", "Start interactive `cbi>` CLI shell immediately after indexing"),
        ):
            index_parser.add_argument(flag, action="store_true", default=False, help=help_text)
        self._bind_handler(index_parser, "index")

    def _add_db_parser(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register the ``cbi db`` subcommand group and its subcommands."""
        db_parser = subparsers.add_parser(
            "db",
            parents=[sub_common],
            help="Inspect SQLite database state, repositories, tables, runs, and read-only SQL",
        )
        db_sub = db_parser.add_subparsers(dest="db_command", required=True)

        status_p = db_sub.add_parser(
            "status",
            parents=[sub_common],
            help="Show DB size, schema version, table counts, edge breakdown, and LSP coverage",
        )
        self._add_repo_arg(status_p)
        self._bind_handler(status_p, "db.status")

        self._bind_handler(
            db_sub.add_parser("repos", parents=[sub_common], help="List indexed repositories"),
            "db.repos",
        )
        self._bind_handler(
            db_sub.add_parser(
                "tables",
                parents=[sub_common],
                help="List SQLite tables and exact row counts",
            ),
            "db.tables",
        )

        runs_p = db_sub.add_parser("runs", parents=[sub_common], help="List recent indexing runs")
        self._add_repo_arg(runs_p, "Filter by repository ID, name, or path")
        self._add_pagination_args(
            runs_p, default_limit=20, limit_help="Maximum runs to show (default: 20)"
        )
        self._bind_handler(runs_p, "db.runs")

        sql_p = db_sub.add_parser(
            "sql",
            parents=[sub_common],
            help="Run a strictly read-only SELECT/PRAGMA query against the SQLite database",
        )
        sql_p.add_argument("query", type=str, help="Read-only SQL SELECT/PRAGMA query")
        self._add_pagination_args(
            sql_p, default_limit=100, limit_help="Maximum rows to display (default: 100)"
        )
        self._bind_handler(sql_p, "db.sql")

    def _add_symbols_parser(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register the ``cbi symbols`` subcommand group and its subcommands."""
        symbols_parser = subparsers.add_parser(
            "symbols",
            parents=[sub_common],
            help="Search, browse, and inspect indexed symbols",
        )
        sym_sub = symbols_parser.add_subparsers(dest="symbols_command", required=True)
        kind_choices = sorted(kind.value for kind in SymbolKind)

        search_p = sym_sub.add_parser(
            "search",
            parents=[sub_common],
            help="Search symbols via FTS5 with exact/prefix ranking and LIKE fallback",
        )
        search_p.add_argument("query", type=str, help="Search query")
        self._add_symbol_filter_args(search_p, kind_choices)
        self._add_pagination_args(
            search_p, default_limit=20, limit_help="Maximum results (default: 20)"
        )
        self._bind_handler(search_p, "symbols.search")

        list_p = sym_sub.add_parser(
            "list",
            parents=[sub_common],
            help="Browse indexed symbols with pagination",
        )
        self._add_symbol_filter_args(list_p, kind_choices)
        self._add_pagination_args(
            list_p,
            default_limit=50,
            limit_help="Maximum symbols to show (default: 50)",
            include_offset=True,
        )
        self._bind_handler(list_p, "symbols.list")

        show_p = sym_sub.add_parser(
            "show",
            parents=[sub_common],
            help="Show full symbol context including hierarchy, relationships, and freshness",
        )
        show_p.add_argument("id_or_name", type=str, help="Symbol ID, qualified name, or short name")
        self._add_repo_arg(show_p)
        show_p.add_argument(
            "--source",
            action="store_true",
            default=False,
            help="Include bounded line-numbered symbol source snippet",
        )
        self._bind_handler(show_p, "symbols.show")

    def _add_file_parsers(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register ``files``, ``outline``, and ``source`` subcommands."""
        files_p = subparsers.add_parser(
            "files",
            parents=[sub_common],
            help="List indexed files with line count, symbol count, and live disk freshness",
        )
        files_p.add_argument(
            "pattern", type=str, nargs="?", default=None, help="Path/module filter"
        )
        self._add_repo_arg(files_p, "Repository selector")
        files_p.add_argument(
            "--include-tests", action="store_true", default=False, help="Include indexed test files"
        )
        self._add_pagination_args(
            files_p, default_limit=50, limit_help="Maximum files to show (default: 50)"
        )
        self._bind_handler(files_p, "files")

        outline_p = subparsers.add_parser(
            "outline",
            parents=[sub_common],
            help="Show hierarchical symbol outline for an indexed file",
        )
        outline_p.add_argument("file_path", type=str, help="Repository-relative file path")
        self._add_repo_arg(outline_p, "Repository selector")
        outline_p.add_argument(
            "--no-private", action="store_true", default=False, help="Exclude private/local symbols"
        )
        outline_p.add_argument(
            "--max-depth", type=int, default=3, help="Maximum symbol tree depth (default: 3)"
        )
        self._bind_handler(outline_p, "outline")

        source_p = subparsers.add_parser(
            "source",
            parents=[sub_common],
            help="Print line-numbered source snippet for a symbol",
        )
        source_p.add_argument("id_or_name", type=str, help="Symbol ID or name")
        self._add_repo_arg(source_p, "Repository selector")
        source_p.add_argument(
            "--max-lines", type=int, default=120, help="Maximum lines to print (default: 120)"
        )
        source_p.add_argument(
            "--offset", type=int, default=0, help="Line offset within symbol (default: 0)"
        )
        self._bind_handler(source_p, "source")

    def _add_analysis_parsers(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register ``refs`` and ``risk`` subcommands."""
        refs_p = subparsers.add_parser(
            "refs",
            parents=[sub_common],
            help="Show incoming/outgoing calls and references with 1-line previews",
        )
        refs_p.add_argument("id_or_name", type=str, help="Symbol ID, qualified name, or short name")
        self._add_repo_arg(refs_p, "Repository selector")
        refs_p.add_argument(
            "--direction",
            type=str,
            choices=["incoming", "outgoing", "both"],
            default="incoming",
            help="Edge direction to inspect (default: incoming)",
        )
        refs_p.add_argument(
            "--refresh", action="store_true", default=False, help="Refresh LSP references first"
        )
        self._add_pagination_args(
            refs_p,
            default_limit=50,
            limit_help="Maximum edges to display (default: 50)",
            include_offset=True,
        )
        self._bind_handler(refs_p, "refs")

        risk_p = subparsers.add_parser(
            "risk",
            parents=[sub_common],
            help="Analyze change impact and risk for one or more symbols",
        )
        risk_p.add_argument("id_or_names", type=str, nargs="+", help="One or more symbol selectors")
        self._add_repo_arg(risk_p, "Repository selector")
        self._bind_handler(risk_p, "risk")

    def _add_daemon_parsers(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register ``shell``, ``watch``, ``serve``, and ``mcp-call`` subcommands."""
        shell_p = subparsers.add_parser(
            "shell",
            parents=[sub_common],
            help="Start interactive `cbi>` shell to inspect an indexed database",
        )
        self._add_repo_arg(shell_p, "Repository selector")
        self._bind_handler(shell_p, "shell")

        watch_p = subparsers.add_parser(
            "watch",
            parents=[sub_common],
            help="Watch a repository for changes and incrementally update the index",
        )
        watch_p.add_argument(
            "repo_path", type=Path, nargs="?", default=None, help="Repository root path"
        )
        watch_p.add_argument(
            "--debounce-ms", type=int, default=500, help="Debounce window in ms (default: 500)"
        )
        watch_p.add_argument(
            "--include-tests", action="store_true", default=False, help="Watch test files"
        )
        watch_p.add_argument(
            "--verbose", action="store_true", default=argparse.SUPPRESS, help="Enable debug logs"
        )
        self._bind_handler(watch_p, "watch")

        serve_p = subparsers.add_parser(
            "serve",
            parents=[sub_common],
            help="Start the token-lean MCP server on stdio transport",
        )
        serve_p.add_argument(
            "--debug", action="store_true", default=argparse.SUPPRESS, help="Enable debug logs"
        )
        self._bind_handler(serve_p, "serve")

        mcp_call_p = subparsers.add_parser(
            "mcp-call",
            parents=[sub_common],
            help="Execute an MCP tool locally and show payload byte/token metrics",
        )
        mcp_call_p.add_argument("tool_name", type=str, help="MCP tool name")
        mcp_call_p.add_argument(
            "json_args", type=str, nargs="*", default=[], help="JSON object or shorthand args"
        )
        self._add_repo_arg(mcp_call_p, "Repository selector")
        self._bind_handler(mcp_call_p, "mcp-call")

        self._add_api_parser(subparsers, sub_common)

    def _add_api_parser(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register the ``cbi api`` FastAPI ACP WebSocket server subcommand."""
        api_p = subparsers.add_parser(
            "api",
            parents=[sub_common],
            help="Start FastAPI server hosting the hardened /acp WebSocket bridge",
        )
        api_p.add_argument(
            "--host",
            type=str,
            default="127.0.0.1",
            help="Bind address (default: 127.0.0.1 loopback-only)",
        )
        api_p.add_argument(
            "--port",
            type=int,
            default=8000,
            help="Listening TCP port (default: 8000)",
        )
        api_p.add_argument(
            "--allow-non-loopback",
            action="store_true",
            default=False,
            help="Allow binding non-loopback network interfaces (unsafe on untrusted LANs)",
        )
        api_p.add_argument(
            "--allow-origin",
            action="append",
            default=[],
            help="Additional allowed WebSocket Origin (repeatable)",
        )
        api_p.add_argument(
            "--workspace",
            type=Path,
            default=None,
            help="Optional workspace root directory passed as cwd for agy acp",
        )
        api_p.add_argument(
            "--no-agy-sandbox",
            action="store_true",
            default=False,
            help="Disable --sandbox flag when launching agy acp",
        )
        api_p.add_argument(
            "--agy-app-data-dir",
            type=str,
            default=None,
            help="Directory under ~/.gemini for agy acp data (default: antigravity)",
        )
        api_p.add_argument(
            "--agy-gemini-dir",
            type=str,
            default=None,
            help="Optional absolute root directory override instead of ~/.gemini",
        )
        api_p.add_argument(
            "--debug",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Enable debug logs",
        )
        self._bind_handler(api_p, "api")

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
        self._add_repo_arg(p)
        p.add_argument("--regex", action="store_true", default=False, help="Treat query as regex")
        p.add_argument(
            "--case-sensitive", action="store_true", default=False, help="Match case-sensitively"
        )
        p.add_argument("--path", dest="path_prefix", type=str, default=None, help="Path prefix")
        p.add_argument("--lang", dest="language", type=str, default=None, help="Language filter")
        p.add_argument("--no-tests", action="store_true", default=False, help="Exclude test files")
        p.add_argument("-C", "--context", dest="context_lines", type=int, default=0, help="Context")
        self._add_pagination_args(
            p,
            default_limit=50,
            limit_help="Maximum matches per page (1..200)",
            include_offset=True,
        )
        self._bind_handler(p, "search")

    def _add_view_parser(
        self,
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
        sub_common: argparse.ArgumentParser,
    ) -> None:
        """Register the ``cbi view`` subcommand group and its subcommands."""
        view_parser = subparsers.add_parser(
            "view",
            parents=[sub_common],
            help="Render high-level structural views of an indexed repository",
        )
        view_sub = view_parser.add_subparsers(dest="view_command", required=True)

        tree_p = view_sub.add_parser(
            "tree",
            parents=[sub_common],
            help="Emit folder and file Explorer tree JSON for the indexed codebase",
        )
        tree_p.add_argument(
            "path", nargs="?", default=None, help="Optional repository folder path to focus on"
        )
        self._add_repo_arg(tree_p)
        tree_p.add_argument(
            "--max-depth", type=int, default=None, help="Maximum folder/file nesting depth"
        )
        tree_p.add_argument(
            "--no-tests",
            action="store_true",
            default=False,
            help="Exclude test files and test-only folders",
        )
        self._bind_handler(tree_p, "view.tree")

    def build(self) -> argparse.ArgumentParser:
        """Construct the complete ``cbi`` CLI ``ArgumentParser``."""
        sub_common = self._make_subcommand_common_parser()
        parser = argparse.ArgumentParser(
            prog="cbi",
            description="Repository intelligence CLI and token-lean MCP server",
        )
        parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
        parser.add_argument(
            "--db-path",
            type=Path,
            default=None,
            help="Path to SQLite database file (default: codebase_indexer.db)",
        )
        parser.add_argument(
            "--verbose", action="store_true", default=False, help="Enable debug logging"
        )
        parser.add_argument(
            "--debug", action="store_true", default=False, help="Enable debug logging"
        )

        subparsers = parser.add_subparsers(dest="command", required=True)
        self._add_index_parser(subparsers, sub_common)
        self._add_db_parser(subparsers, sub_common)
        self._add_symbols_parser(subparsers, sub_common)
        self._add_search_parser(subparsers, sub_common)
        self._add_file_parsers(subparsers, sub_common)
        self._add_analysis_parsers(subparsers, sub_common)
        self._add_view_parser(subparsers, sub_common)
        self._add_daemon_parsers(subparsers, sub_common)
        return parser


__all__ = [
    "CliParserBuilder",
]
