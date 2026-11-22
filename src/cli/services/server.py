"""CLI command handlers for ``cbi serve``, ``cbi mcp-call``, and ``cbi api``."""

import argparse
import asyncio
import json
from typing import Any

from api import DEFAULT_ALLOWED_ORIGINS, ApiServerRunner, resolve_acp_server_config
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
    """Executes ``cbi serve``, ``cbi mcp-call``, and ``cbi api`` CLI commands."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize MCP server and ACP gateway CLI command handlers."""
        self._engine = engine

    @staticmethod
    def _extract_raw_tokens(args: argparse.Namespace) -> list[str]:
        """Extract raw ``json_args`` CLI tokens as a normalized list of strings."""
        raw_arg_value = getattr(args, "json_args", [])
        if isinstance(raw_arg_value, str):
            return [raw_arg_value] if raw_arg_value else []
        return list(raw_arg_value or [])

    def run_serve(self, args: argparse.Namespace) -> int:
        """Start the token-lean MCP server on ``stdio`` transport."""
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

    @staticmethod
    def _build_api_transport_overrides(args: argparse.Namespace) -> dict[str, Any]:
        """Extract CLI transport overrides without masking TOML/env defaults."""
        extra_origins = getattr(args, "allow_origin", None)
        return {
            "host": getattr(args, "host", None),
            "port": getattr(args, "port", None),
            "ws_path": getattr(args, "ws_path", None),
            "allow_non_loopback": getattr(args, "allow_non_loopback", None),
            "allowed_origins": (
                (*DEFAULT_ALLOWED_ORIGINS, *extra_origins) if extra_origins else None
            ),
            "auth_token": getattr(args, "auth_token", None),
        }

    @staticmethod
    def _build_api_agent_overrides(args: argparse.Namespace) -> dict[str, Any]:
        """Extract CLI ACP agent overrides without masking TOML/env defaults."""
        agent_cmd = getattr(args, "agent_command", None)
        return {
            "command": tuple(agent_cmd) if agent_cmd else None,
            "workspace_root": getattr(args, "workspace", None),
            "agy_sandbox": False if getattr(args, "no_agy_sandbox", False) else None,
            "agy_app_data_dir": getattr(args, "agy_app_data_dir", None),
            "agy_gemini_dir": getattr(args, "agy_gemini_dir", None),
        }

    @staticmethod
    def _build_api_cli_overrides(args: argparse.Namespace, *, debug: bool) -> dict[str, Any]:
        """Collect non-None CLI overrides across composed ``AcpServerConfig`` sections."""
        log_level = getattr(args, "log_level", None) or ("DEBUG" if debug else None)
        return {
            "transport": ServerCliCommands._build_api_transport_overrides(args),
            "agent": ServerCliCommands._build_api_agent_overrides(args),
            "capabilities": {
                "fs_read": getattr(args, "fs_read", None),
                "fs_write": getattr(args, "fs_write", None),
                "terminal": getattr(args, "terminal", None),
                "auth_terminal": getattr(args, "auth_terminal", None),
            },
            "logging": {
                "format": getattr(args, "log_format", None),
                "level": log_level,
                "log_file": getattr(args, "log_file", None),
            },
        }

    def run_api(self, args: argparse.Namespace) -> int:
        """Start the pure-WebSocket ACP v1 server after resolving 3-layer config."""
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        debug = bool(getattr(args, "debug", self._engine.config.debug))
        scoped = self._engine.for_db(db_path)
        app_cfg = scoped.config.model_copy(update={"debug": debug})
        api_cfg = resolve_acp_server_config(
            config_file=getattr(args, "config", None),
            cli_overrides=self._build_api_cli_overrides(args, debug=debug),
        )
        runner = ApiServerRunner(api_cfg, app_cfg, engine=scoped)
        runner.run()
        return 0


__all__ = [
    "ServerCliCommands",
]
