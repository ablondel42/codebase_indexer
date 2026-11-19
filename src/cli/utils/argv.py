"""CLI argv preprocessing, shell token normalization, and ``mcp-call`` shorthand parsers."""

import json
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from core.utils.logging import get_logger

logger = get_logger("cli.utils.argv")

KNOWN_COMMANDS: frozenset[str] = frozenset(
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

_REPO_SCOPED_ROOT: frozenset[str] = frozenset(
    {"files", "outline", "source", "refs", "risk", "mcp-call"}
)
_REPO_SCOPED_SUBS: frozenset[tuple[str, str]] = frozenset(
    {
        ("db", "status"),
        ("db", "runs"),
        ("symbols", "search"),
        ("symbols", "list"),
        ("symbols", "show"),
    }
)
_PRIMARY_ARG_BY_TOOL: Mapping[str, str] = MappingProxyType(
    {
        "get_repo_overview": "repo_id",
        "list_files": "path_prefix",
        "get_file_outline": "file_path",
        "search_symbols": "query",
        "get_symbol_source": "symbol",
        "get_symbol_context": "symbol",
        "get_symbol_references": "symbol",
        "analyze_risk": "symbols",
    }
)
_LITERAL_SHORTHAND_VALUES: Mapping[str, bool | None] = MappingProxyType(
    {
        "true": True,
        "false": False,
        "null": None,
        "none": None,
    }
)
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


def preprocess_argv(tokens: Sequence[str]) -> list[str]:
    """Rewrite ``demo`` or ``<repo_path>`` shorthand tokens into ``index`` arguments.

    Args:
        tokens: Raw command-line argument sequence excluding program name.

    Returns:
        Normalized argument token list ready for ``ArgumentParser.parse_args``.
    """
    raw = list(tokens)
    if not raw:
        return raw
    first = raw[0]
    if first != "demo" and (first.startswith("-") or first in KNOWN_COMMANDS):
        return raw

    is_demo = first == "demo"
    target = "." if is_demo else first
    rest = raw[1:]
    use_shell = "--no-cli" not in rest
    cleaned = [tok for tok in rest if tok not in ("--cli", "--no-cli")]

    rewritten = ["index", target]
    if is_demo and "--include-tests" not in cleaned:
        rewritten.append("--include-tests")
    if use_shell and "--shell" not in cleaned:
        rewritten.append("--shell")
    rewritten.extend(cleaned)
    return rewritten


def tokenize_shell_line(line: str) -> list[str]:
    """Split a non-empty shell line via ``shlex`` and strip a leading ``rc`` prefix.

    Args:
        line: Trimmed interactive shell command line.

    Returns:
        Tokenized list of CLI arguments.

    Raises:
        ValueError: If ``line`` contains unbalanced quotes rejected by ``shlex.split``.
    """
    tokens = shlex.split(line)
    if len(tokens) > 1 and tokens[0] == "rc":
        return tokens[1:]
    return tokens


def inject_shell_defaults(
    tokens: Sequence[str],
    db_path: Path,
    active_repo: str | None,
) -> list[str]:
    """Join unquoted ``db sql`` tokens and append active ``--db-path`` / ``--repo`` flags.

    Args:
        tokens: Tokenized shell command arguments.
        db_path: Active session SQLite database path.
        active_repo: Active session repository identifier or ``None``.

    Returns:
        Token list with session defaults injected when omitted by the user.
    """
    normalized = list(tokens)
    if (
        len(normalized) >= 3
        and normalized[:2] == ["db", "sql"]
        and not any(tok.startswith("-") for tok in normalized[2:])
    ):
        normalized = ["db", "sql", " ".join(normalized[2:])]

    if any(tok in ("-h", "--help", "--version") for tok in normalized):
        return normalized

    if not any(tok == "--db-path" or tok.startswith("--db-path=") for tok in normalized):
        normalized.extend(["--db-path", str(db_path)])

    takes_repo = normalized[0] in _REPO_SCOPED_ROOT or (
        len(normalized) >= 2 and (normalized[0], normalized[1]) in _REPO_SCOPED_SUBS
    )
    if (
        active_repo
        and takes_repo
        and not any(tok == "--repo" or tok.startswith("--repo=") for tok in normalized)
    ):
        normalized.extend(["--repo", active_repo])
    return normalized


def _coerce_shorthand_value(key: str, raw_value: str) -> Any:
    """Coerce a CLI shorthand ``key=value`` string into ``bool``, ``int``, JSON, or ``str``.

    Args:
        key: Shorthand parameter name.
        raw_value: Raw value substring following ``=``.

    Returns:
        Coerced Python value suitable for MCP tool input validation.
    """
    lower = raw_value.lower()
    if lower in _LITERAL_SHORTHAND_VALUES:
        return _LITERAL_SHORTHAND_VALUES[lower]
    if re.fullmatch(r"-?\d+", raw_value):
        return int(raw_value)
    if raw_value.startswith(("[", "{")):
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError as exc:
            logger.debug("Keeping shorthand value for %s as raw string: %s", key, exc)
    if key == "symbols":
        return [part.strip() for part in raw_value.split(",") if part.strip()]
    return raw_value


def _assign_positional_args(
    tool_name: str,
    positional: Sequence[str],
    parsed_kwargs: dict[str, Any],
) -> None:
    """Bind positional shorthand tokens onto ``tool_name``'s primary parameter.

    Args:
        tool_name: Target MCP tool name.
        positional: Positional shorthand tokens without ``key=value`` syntax.
        parsed_kwargs: Mutable keyword argument dictionary to populate.
    """
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


def parse_mcp_call_args(tool_name: str, raw_tokens: Sequence[str]) -> dict[str, Any]:
    """Parse JSON object arguments or CLI shorthand tokens into MCP tool kwargs.

    Args:
        tool_name: Target MCP tool name.
        raw_tokens: Raw ``json_args`` positional tokens from ``argparse``.

    Returns:
        Parsed dictionary of raw MCP tool arguments.

    Raises:
        json.JSONDecodeError: If ``raw_tokens`` starts as JSON and fails decoding.
        ValueError: If decoded JSON is not an object dictionary.
    """
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
            parsed_kwargs[match.group(1)] = _coerce_shorthand_value(
                match.group(1),
                match.group(2),
            )
        else:
            positional.append(tok)

    _assign_positional_args(tool_name, positional, parsed_kwargs)
    return parsed_kwargs


def normalize_mcp_tool_kwargs(
    tool_name: str,
    raw_args: Mapping[str, Any],
    explicit_db_path: Path | None,
    default_db_path: Path,
    *,
    repo_arg: str | None = None,
) -> dict[str, Any]:
    """Normalize MCP alias keys and inject CLI ``--db-path`` and ``--repo`` overrides.

    Args:
        tool_name: Target MCP tool name.
        raw_args: Parsed raw argument mapping.
        explicit_db_path: Explicit ``--db-path`` supplied on CLI, or ``None``.
        default_db_path: Fallback ``AppConfig.db_path`` when ``--db-path`` is omitted.
        repo_arg: Optional ``--repo`` CLI argument value.

    Returns:
        Normalized argument dictionary ready for ``McpToolRegistry.call_tool``.
    """
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


__all__ = [
    "KNOWN_COMMANDS",
    "inject_shell_defaults",
    "normalize_mcp_tool_kwargs",
    "parse_mcp_call_args",
    "preprocess_argv",
    "tokenize_shell_line",
]
