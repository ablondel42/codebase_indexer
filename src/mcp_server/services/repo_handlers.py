"""Synchronous MCP handlers for repository overview, file listing, outlines, and search."""

from typing import Any

from core.models.search import CodeSearchQuery
from core.utils.logging import get_logger
from engine.models.queries import ListFilesQuery, OutlineQuery, SearchSymbolsQuery
from engine.services.facade import IndexerEngine
from mcp_server.models.inputs import (
    GetFileOutlineInput,
    GetRepoOverviewInput,
    ListFilesInput,
    SearchCodeInput,
    SearchSymbolsInput,
)
from mcp_server.utils.compact import (
    adapt_code_search,
    adapt_file_list,
    adapt_outline,
    adapt_repo_overview,
    adapt_symbol_search,
)

logger = get_logger("mcp_server.services.repo_handlers")


class RepoMcpHandlers:
    """MCP handlers for repository- and file-scoped read tools."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize repository MCP handlers over an ``IndexerEngine``."""
        self._engine = engine

    def get_repo_overview(self, inp: GetRepoOverviewInput) -> dict[str, Any]:
        """Return compact repository metadata, graph statistics, and top directories."""
        logger.debug("Executing MCP get_repo_overview (repo_id=%s)", inp.repo_id)
        overview = self._engine.for_db(inp.db_path).queries.get_repo_overview(inp.repo_id)
        return adapt_repo_overview(overview)

    def list_files(self, inp: ListFilesInput) -> dict[str, Any]:
        """Return paginated tracked repository files with optional prefix filtering."""
        logger.debug("Executing MCP list_files (repo_id=%s, limit=%d)", inp.repo_id, inp.limit)
        page = self._engine.for_db(inp.db_path).queries.list_files(
            ListFilesQuery(
                repo=inp.repo_id,
                path_prefix=inp.path_prefix,
                include_tests=inp.include_tests,
                check_freshness=False,
                limit=inp.limit,
                offset=inp.offset,
            )
        )
        return adapt_file_list(page)

    def get_file_outline(self, inp: GetFileOutlineInput) -> dict[str, Any]:
        """Return hierarchical symbol outline and deduplicated imports for a file."""
        logger.debug("Executing MCP get_file_outline (file_path=%s)", inp.file_path)
        outline = self._engine.for_db(inp.db_path).queries.get_file_outline(
            OutlineQuery(
                file_path=inp.file_path,
                repo=inp.repo_id,
                include_private=inp.include_private,
                max_depth=inp.max_depth,
            )
        )
        return adapt_outline(outline)

    def search_symbols(self, inp: SearchSymbolsInput) -> dict[str, Any]:
        """Search repository symbols via FTS5 with exact/prefix ranking."""
        logger.debug("Executing MCP search_symbols (query=%r)", inp.query)
        page = self._engine.for_db(inp.db_path).queries.search_symbols(
            SearchSymbolsQuery(
                query=inp.query,
                repo=inp.repo_id,
                kind=inp.kind,
                file_path=inp.file_path,
                limit=inp.limit,
            )
        )
        return adapt_symbol_search(page)

    def search_code(self, inp: SearchCodeInput) -> dict[str, Any]:
        """Search all UTF-8 repository text files with trigram pruning and live-disk overlay."""
        logger.debug("Executing MCP search_code (query=%r, regex=%s)", inp.query, inp.regex)
        report = self._engine.for_db(inp.db_path).queries.search_code(
            CodeSearchQuery(
                query=inp.query,
                repo=inp.repo_id,
                regex=inp.regex,
                case_sensitive=inp.case_sensitive,
                path_prefix=inp.path_prefix,
                language=inp.language,
                include_tests=inp.include_tests,
                verify_disk=True,
                context_lines=inp.context_lines,
                limit=inp.limit,
                offset=inp.offset,
            )
        )
        return adapt_code_search(report)


__all__ = [
    "RepoMcpHandlers",
]
