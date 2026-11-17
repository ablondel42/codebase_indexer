"""Handlers for `rc watch`, `rc serve`, and `rc mcp-call`."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Coroutine

from repo_context.config import get_config
from repo_context.exceptions import ValidationError
from repo_context.indexing import watch_repo
from repo_context.cli.formatting import estimate_tokens, print_json
from repo_context.cli.utils import resolve_db_path

_PRIMARY_ARG_BY_TOOL: dict[str, str] = {
    "get_repo_overview": "repo_id",
    "list_files": "path_prefix",
    "get_file_outline": "file_path",
    "search_symbols": "query",
    "get_symbol_source": "symbol",
    "get_symbol_context": "symbol",
    "get_symbol_references": "symbol",
    "analyze_risk": "symbols",
}
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


def _load_mcp_tool_registry() -> dict[str, Callable[..., Coroutine[Any, Any, str]]]:
    """Lazily import the 8 MCP tool functions so non-MCP CLI commands require no mcp package."""
    from repo_context.mcp import (
        analyze_risk,
        get_file_outline,
        get_repo_overview,
        get_symbol_context,
        get_symbol_references,
        get_symbol_source,
        list_files,
        search_symbols,
    )

    return {
        "get_repo_overview": get_repo_overview,
        "list_files": list_files,
        "get_file_outline": get_file_outline,
        "search_symbols": search_symbols,
        "get_symbol_source": get_symbol_source,
        "get_symbol_context": get_symbol_context,
        "get_symbol_references": get_symbol_references,
        "analyze_risk": analyze_risk,
    }


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch a repository and incrementally reindex changed Python files."""
    db_path = resolve_db_path(args)
    include_tests = bool(getattr(args, "include_tests", False))
    debounce_ms = int(getattr(args, "debounce_ms", 500))
    if debounce_ms < 0:
        raise ValidationError("--debounce-ms must be >= 0", context={"debounce_ms": debounce_ms})
    verbose = bool(getattr(args, "verbose", False) or getattr(args, "debug", False))
    repo_root: Path = getattr(args, "repo_path", None) or Path.cwd()

    cfg = get_config(db_path=db_path, include_tests=include_tests)
    watch_repo(
        repo_root=repo_root,
        config=cfg,
        db_path=db_path,
        debounce_ms=debounce_ms,
        verbose=verbose,
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the token-lean MCP server on stdio transport."""
    db_path = resolve_db_path(args)
    debug = bool(getattr(args, "debug", False) or getattr(args, "verbose", False))
    try:
        from repo_context.mcp import run_server
    except ImportError as exc:
        print(
            f"Error: MCP package is required for `rc serve` ({exc}).",
            file=sys.stderr,
        )
        return 1

    run_server(db_path=db_path, debug=debug)
    return 0


def _coerce_shorthand_value(key: str, raw_value: str) -> Any:
    """Coerce a CLI shorthand ``key=value`` string into bool, int, JSON, or str."""
    lower = raw_value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", raw_value):
        return int(raw_value)
    if raw_value.startswith(("[", "{")):
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            pass
    if key == "symbols":
        return [part.strip() for part in raw_value.split(",") if part.strip()]
    return raw_value


def _parse_mcp_call_args(tool_name: str, raw_tokens: list[str]) -> dict[str, Any]:
    """Parse JSON object arguments or CLI shorthand into MCP tool kwargs."""
    tokens = [tok for tok in raw_tokens if tok]
    if not tokens:
        return {}

    joined = " ".join(tokens).strip()
    if joined.startswith(("{", "[")):
        parsed = json.loads(joined)
        if not isinstance(parsed, dict):
            raise ValueError("json_args must be a JSON object.")
        return parsed

    parsed_kwargs: dict[str, Any] = {}
    positional: list[str] = []
    for tok in tokens:
        match = _KV_RE.match(tok)
        if match:
            key, val = match.group(1), match.group(2)
            parsed_kwargs[key] = _coerce_shorthand_value(key, val)
        else:
            positional.append(tok)

    if positional:
        primary_key = _PRIMARY_ARG_BY_TOOL.get(tool_name)
        if primary_key and primary_key not in parsed_kwargs:
            if primary_key == "symbols":
                symbols: list[str] = []
                for item in positional:
                    symbols.extend(part.strip() for part in item.split(",") if part.strip())
                parsed_kwargs[primary_key] = symbols
            else:
                parsed_kwargs[primary_key] = " ".join(positional)

    return parsed_kwargs


def _normalize_tool_kwargs(
    tool_name: str,
    raw_args: dict[str, Any],
    explicit_db_path: Path | None,
    default_db_path: Path,
    repo_arg: str | None = None,
) -> dict[str, Any]:
    """Normalize MCP alias keys and inject CLI ``--db-path`` / ``--repo``."""
    kwargs = dict(raw_args)
    if tool_name in ("get_symbol_source", "get_symbol_context", "get_symbol_references"):
        if "symbol" not in kwargs and "symbol_id_or_name" in kwargs:
            kwargs["symbol"] = kwargs.pop("symbol_id_or_name")
    elif tool_name == "analyze_risk":
        if "symbols" not in kwargs and "symbol_ids" in kwargs:
            kwargs["symbols"] = kwargs.pop("symbol_ids")

    if repo_arg and "repo_id" not in kwargs:
        kwargs["repo_id"] = repo_arg

    if explicit_db_path is not None:
        kwargs["db_path"] = str(explicit_db_path)
    else:
        kwargs.setdefault("db_path", str(default_db_path))
    return kwargs


def cmd_mcp_call(args: argparse.Namespace) -> int:
    """Execute one of the 8 MCP tools locally via ``asyncio.run`` and inspect payload size."""
    explicit_db_path: Path | None = getattr(args, "db_path", None)
    db_path = resolve_db_path(args)
    tool_name: str = args.tool_name
    raw_arg_value = getattr(args, "json_args", [])
    if isinstance(raw_arg_value, str):
        raw_tokens = [raw_arg_value] if raw_arg_value else []
    else:
        raw_tokens = list(raw_arg_value or [])
    repo_arg: str | None = getattr(args, "repo", None)

    try:
        tool_registry = _load_mcp_tool_registry()
    except ImportError as exc:
        print(
            f"Error: MCP package is required for `rc mcp-call` ({exc}).",
            file=sys.stderr,
        )
        return 1

    if tool_name not in tool_registry:
        available = ", ".join(tool_registry.keys())
        print(
            f"Error: Unknown MCP tool {tool_name!r}. Available tools: {available}",
            file=sys.stderr,
        )
        return 1

    try:
        parsed_args = _parse_mcp_call_args(tool_name, raw_tokens)
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON arguments: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    handler = tool_registry[tool_name]
    kwargs = _normalize_tool_kwargs(
        tool_name,
        parsed_args,
        explicit_db_path,
        db_path,
        repo_arg=repo_arg,
    )

    from repo_context.mcp import close_default_lsp_pool

    try:
        raw_response = asyncio.run(handler(**kwargs))
    except TypeError as exc:
        print(f"Error: Invalid arguments for {tool_name}: {exc}", file=sys.stderr)
        return 1
    finally:
        if tool_name == "get_symbol_references" and kwargs.get("refresh_lsp"):
            close_default_lsp_pool()

    byte_size, est_tokens = estimate_tokens(raw_response)
    try:
        parsed_response = json.loads(raw_response)
    except json.JSONDecodeError:
        parsed_response = {"raw": raw_response}

    if getattr(args, "json", False):
        print_json(
            {
                "tool": tool_name,
                "bytes": byte_size,
                "estimated_tokens": est_tokens,
                "result": parsed_response,
            }
        )
    else:
        print(f"MCP Tool: {tool_name}")
        print(f"Payload: {byte_size} bytes (~{est_tokens} tokens)")
        print_json(parsed_response)

    if isinstance(parsed_response, dict) and parsed_response.get("ok") is False:
        return 1
    return 0
