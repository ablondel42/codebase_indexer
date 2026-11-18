"""Handlers for `rc watch`, `rc serve`, and `rc mcp-call`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from repo_context.cli.utils import resolve_db_path
from repo_context.config import get_config
from repo_context.exceptions import ValidationError
from repo_context.indexing import watch_repo
from repo_context.mcp import (
    analyze_risk,
    close_default_lsp_pool,
    get_file_outline,
    get_repo_overview,
    get_symbol_context,
    get_symbol_references,
    get_symbol_source,
    list_files,
    run_server,
    search_symbols,
)

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
_MCP_TOOL_REGISTRY: dict[str, Callable[..., Coroutine[Any, Any, str]]] = {
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
    run_server(db_path=db_path, debug=debug)
    return 0


_LITERAL_SHORTHAND_VALUES: dict[str, bool | None] = {
    "true": True,
    "false": False,
    "null": None,
    "none": None,
}


def _coerce_shorthand_value(key: str, raw_value: str) -> Any:
    """Coerce a CLI shorthand ``key=value`` string into bool, int, JSON, or str."""
    lower = raw_value.lower()
    if lower in _LITERAL_SHORTHAND_VALUES:
        return _LITERAL_SHORTHAND_VALUES[lower]
    if re.fullmatch(r"-?\d+", raw_value):
        return int(raw_value)
    if raw_value.startswith(("[", "{")):
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(raw_value)
    if key == "symbols":
        return [part.strip() for part in raw_value.split(",") if part.strip()]
    return raw_value


def _assign_positional_args(
    tool_name: str,
    positional: list[str],
    parsed_kwargs: dict[str, Any],
) -> None:
    """Bind positional shorthand tokens to ``tool_name``'s primary parameter."""
    if not positional:
        return
    primary_key = _PRIMARY_ARG_BY_TOOL.get(tool_name)
    if not primary_key or primary_key in parsed_kwargs:
        return
    if primary_key == "symbols":
        parsed_kwargs[primary_key] = [
            part.strip()
            for item in positional
            for part in item.split(",")
            if part.strip()
        ]
        return
    parsed_kwargs[primary_key] = " ".join(positional)


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

    _assign_positional_args(tool_name, positional, parsed_kwargs)
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
    elif tool_name == "analyze_risk" and "symbols" not in kwargs and "symbol_ids" in kwargs:
        kwargs["symbols"] = kwargs.pop("symbol_ids")

    if repo_arg and "repo_id" not in kwargs:
        kwargs["repo_id"] = repo_arg

    if explicit_db_path is not None:
        kwargs["db_path"] = str(explicit_db_path)
    else:
        kwargs.setdefault("db_path", str(default_db_path))
    return kwargs


def _extract_raw_tokens(args: argparse.Namespace) -> list[str]:
    """Extract raw ``json_args`` CLI tokens as a normalized list of strings."""
    raw_arg_value = getattr(args, "json_args", [])
    if isinstance(raw_arg_value, str):
        return [raw_arg_value] if raw_arg_value else []
    return list(raw_arg_value or [])


def _invoke_mcp_tool(tool_name: str, kwargs: dict[str, Any]) -> str:
    """Execute a registered MCP tool coroutine via ``asyncio.run``."""
    handler = _MCP_TOOL_REGISTRY.get(tool_name)
    if handler is None:
        available = ", ".join(_MCP_TOOL_REGISTRY.keys())
        raise ValidationError(
            f"Unknown MCP tool {tool_name!r}. Available tools: {available}",
            context={"tool": tool_name},
        )
    try:
        return asyncio.run(handler(**kwargs))
    except TypeError as exc:
        raise ValidationError(
            f"Invalid arguments for {tool_name}: {exc}",
            context={"tool": tool_name},
        ) from exc
    finally:
        if tool_name == "get_symbol_references" and kwargs.get("refresh_lsp"):
            close_default_lsp_pool()


def _format_mcp_call_output(tool_name: str, raw_response: str) -> tuple[dict[str, Any], int]:
    """Measure payload bytes/tokens and decode tool response JSON."""
    byte_size = len(raw_response.encode("utf-8"))
    est_tokens = max(1, (byte_size + 3) // 4) if byte_size > 0 else 0
    try:
        parsed_response = json.loads(raw_response)
    except json.JSONDecodeError:
        parsed_response = {"raw": raw_response}

    exit_code = 1 if isinstance(parsed_response, dict) and parsed_response.get("ok") is False else 0
    return (
        {
            "tool": tool_name,
            "bytes": byte_size,
            "estimated_tokens": est_tokens,
            "result": parsed_response,
        },
        exit_code,
    )


def cmd_mcp_call(args: argparse.Namespace) -> int:
    """Execute one of the 8 MCP tools locally via ``asyncio.run`` and inspect payload size."""
    tool_name: str = args.tool_name
    if tool_name not in _MCP_TOOL_REGISTRY:
        available = ", ".join(_MCP_TOOL_REGISTRY.keys())
        raise ValidationError(
            f"Unknown MCP tool {tool_name!r}. Available tools: {available}",
            context={"tool": tool_name},
        )

    try:
        parsed_args = _parse_mcp_call_args(tool_name, _extract_raw_tokens(args))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON arguments: {exc}") from exc
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    kwargs = _normalize_tool_kwargs(
        tool_name,
        parsed_args,
        getattr(args, "db_path", None),
        resolve_db_path(args),
        repo_arg=getattr(args, "repo", None),
    )
    raw_response = _invoke_mcp_tool(tool_name, kwargs)
    payload, exit_code = _format_mcp_call_output(tool_name, raw_response)
    print(json.dumps(payload, indent=2))
    return exit_code
