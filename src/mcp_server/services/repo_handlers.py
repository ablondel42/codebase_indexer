"""Async MCP handlers for repository overview, file listing, outlines, and symbol search."""

import asyncio

from core.utils.logging import get_logger
from engine.models.queries import ListFilesQuery, OutlineQuery, SearchSymbolsQuery
from engine.services.facade import IndexerEngine
from mcp_server.models.inputs import (
    GetFileOutlineInput,
    GetRepoOverviewInput,
    ListFilesInput,
    SearchSymbolsInput,
)
from mcp_server.utils.compact import (
    adapt_file_list,
    adapt_outline,
    adapt_repo_overview,
    adapt_symbol_search,
    serialize_mcp_json,
)
from mcp_server.utils.errors import success_result

logger = get_logger("mcp_server.services.repo_handlers")


class RepoMcpHandlers:
    """Async MCP handlers for repository- and file-scoped read tools."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize repository MCP handlers over an ``IndexerEngine``.

        Args:
            engine: Headless ``IndexerEngine`` application facade.
        """
        self._engine = engine

    def _run_get_repo_overview(self, inp: GetRepoOverviewInput) -> str:
        """Execute ``get_repo_overview`` synchronously on a worker thread.

        Args:
            inp: Validated ``GetRepoOverviewInput`` model.

        Returns:
            Serialized compact JSON success envelope.
        """
        scoped = self._engine.for_db(inp.db_path)
        overview = scoped.queries.get_repo_overview(inp.repo_id)
        return serialize_mcp_json(success_result(adapt_repo_overview(overview)))

    def _run_list_files(self, inp: ListFilesInput) -> str:
        """Execute ``list_files`` synchronously on a worker thread.

        Args:
            inp: Validated ``ListFilesInput`` model.

        Returns:
            Serialized compact JSON success envelope.
        """
        scoped = self._engine.for_db(inp.db_path)
        page = scoped.queries.list_files(
            ListFilesQuery(
                repo=inp.repo_id,
                path_prefix=inp.path_prefix,
                include_tests=inp.include_tests,
                check_freshness=False,
                limit=inp.limit,
                offset=inp.offset,
            )
        )
        return serialize_mcp_json(success_result(adapt_file_list(page)))

    def _run_get_file_outline(self, inp: GetFileOutlineInput) -> str:
        """Execute ``get_file_outline`` synchronously on a worker thread.

        Args:
            inp: Validated ``GetFileOutlineInput`` model.

        Returns:
            Serialized compact JSON success envelope.
        """
        scoped = self._engine.for_db(inp.db_path)
        outline = scoped.queries.get_file_outline(
            OutlineQuery(
                file_path=inp.file_path,
                repo=inp.repo_id,
                include_private=inp.include_private,
                max_depth=inp.max_depth,
            )
        )
        return serialize_mcp_json(success_result(adapt_outline(outline)))

    def _run_search_symbols(self, inp: SearchSymbolsInput) -> str:
        """Execute ``search_symbols`` synchronously on a worker thread.

        Args:
            inp: Validated ``SearchSymbolsInput`` model.

        Returns:
            Serialized compact JSON success envelope.
        """
        scoped = self._engine.for_db(inp.db_path)
        page = scoped.queries.search_symbols(
            SearchSymbolsQuery(
                query=inp.query,
                repo=inp.repo_id,
                kind=inp.kind,
                file_path=inp.file_path,
                limit=inp.limit,
            )
        )
        return serialize_mcp_json(success_result(adapt_symbol_search(page)))

    async def get_repo_overview(self, inp: GetRepoOverviewInput) -> str:
        """Return compact repository metadata, graph statistics, and top directories.

        Args:
            inp: Validated ``GetRepoOverviewInput`` model.

        Returns:
            Serialized compact JSON MCP success envelope string.
        """
        logger.debug("Executing MCP get_repo_overview (repo_id=%s)", inp.repo_id)
        return await asyncio.to_thread(self._run_get_repo_overview, inp)

    async def list_files(self, inp: ListFilesInput) -> str:
        """Return paginated tracked repository files with optional prefix filtering.

        Args:
            inp: Validated ``ListFilesInput`` model.

        Returns:
            Serialized compact JSON MCP success envelope string.
        """
        logger.debug("Executing MCP list_files (repo_id=%s, limit=%d)", inp.repo_id, inp.limit)
        return await asyncio.to_thread(self._run_list_files, inp)

    async def get_file_outline(self, inp: GetFileOutlineInput) -> str:
        """Return hierarchical symbol outline and deduplicated imports for a file.

        Args:
            inp: Validated ``GetFileOutlineInput`` model.

        Returns:
            Serialized compact JSON MCP success envelope string.
        """
        logger.debug("Executing MCP get_file_outline (file_path=%s)", inp.file_path)
        return await asyncio.to_thread(self._run_get_file_outline, inp)

    async def search_symbols(self, inp: SearchSymbolsInput) -> str:
        """Search repository symbols via FTS5 with exact/prefix ranking.

        Args:
            inp: Validated ``SearchSymbolsInput`` model.

        Returns:
            Serialized compact JSON MCP success envelope string.
        """
        logger.debug("Executing MCP search_symbols (query=%r)", inp.query)
        return await asyncio.to_thread(self._run_search_symbols, inp)


__all__ = [
    "RepoMcpHandlers",
]
