"""CLI command handlers for ``cbi serve`` and ``cbi mcp-call`` (sole CLI-to-MCP bridge)."""

import argparse
import asyncio
import json

from cli.utils.argv import normalize_mcp_tool_kwargs, parse_mcp_call_args
from cli.utils.output import (
    emit_json,
    format_mcp_call_envelope,
    resolve_cli_db_path,
)
from core.models.exceptions import ValidationError
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine
from mcp_server import McpServerRunner, McpToolRegistry

logger = get_logger("cli.services.server")


class ServerCliCommands:
    """Executes ``cbi serve`` stdio MCP server and ``cbi mcp-call`` local tool inspection."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize MCP server and local call CLI command handlers.

        Args:
            engine: Headless ``IndexerEngine`` application facade.
        """
        self._engine = engine

    @staticmethod
    def _extract_raw_tokens(args: argparse.Namespace) -> list[str]:
        """Extract raw ``json_args`` CLI tokens as a normalized list of strings.

        Args:
            args: Parsed ``cbi mcp-call`` CLI arguments.

        Returns:
            List of raw string tokens supplied after ``tool_name``.
        """
        raw_arg_value = getattr(args, "json_args", [])
        if isinstance(raw_arg_value, str):
            return [raw_arg_value] if raw_arg_value else []
        return list(raw_arg_value or [])

    def run_serve(self, args: argparse.Namespace) -> int:
        """Start the token-lean MCP server on ``stdio`` transport.

        Args:
            args: Parsed ``cbi serve`` CLI arguments.

        Returns:
            Process exit code ``0`` when the ``stdio`` loop exits cleanly.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        debug = bool(getattr(args, "debug", False) or getattr(args, "verbose", False))
        logger.debug("Running cbi serve (db_path=%s, debug=%s)", db_path, debug)
        runner = McpServerRunner(self._engine.config, engine=self._engine)
        runner.run_stdio(db_path=db_path, debug=debug)
        return 0

    def run_mcp_call(self, args: argparse.Namespace) -> int:
        """Execute a registered MCP tool in-process and emit byte/token telemetry."""
        tool_name: str = args.tool_name
        try:
            parsed_args = parse_mcp_call_args(tool_name, self._extract_raw_tokens(args))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON arguments: {exc}") from exc
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        resolved_default = resolve_cli_db_path(args, self._engine.db_path)
        kwargs = normalize_mcp_tool_kwargs(
            parsed_args,
            getattr(args, "db_path", None),
            resolved_default,
            repo_arg=getattr(args, "repo", None),
        )
        scoped = self._engine.for_db(kwargs.get("db_path"))
        registry = McpToolRegistry(scoped)
        raw_response = asyncio.run(registry.call_tool(tool_name, kwargs))

        envelope, exit_code = format_mcp_call_envelope(tool_name, raw_response)
        emit_json(envelope.model_dump())
        return exit_code


__all__ = [
    "ServerCliCommands",
]
