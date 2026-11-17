"""CLI command handlers package."""

from repo_context.cli.commands.db import (
    cmd_db_repos,
    cmd_db_runs,
    cmd_db_sql,
    cmd_db_status,
    cmd_db_tables,
)
from repo_context.cli.commands.files import cmd_files, cmd_outline
from repo_context.cli.commands.index import cmd_index
from repo_context.cli.commands.refs import cmd_refs
from repo_context.cli.commands.risk import cmd_risk
from repo_context.cli.commands.server import cmd_mcp_call, cmd_serve, cmd_watch
from repo_context.cli.commands.symbols import (
    cmd_source,
    cmd_symbols_list,
    cmd_symbols_search,
    cmd_symbols_show,
)

__all__ = [
    "cmd_index",
    "cmd_db_status",
    "cmd_db_repos",
    "cmd_db_tables",
    "cmd_db_runs",
    "cmd_db_sql",
    "cmd_files",
    "cmd_outline",
    "cmd_symbols_search",
    "cmd_symbols_list",
    "cmd_symbols_show",
    "cmd_source",
    "cmd_refs",
    "cmd_risk",
    "cmd_watch",
    "cmd_serve",
    "cmd_mcp_call",
]
