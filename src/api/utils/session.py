"""Stateless validation and curated MCP injection helpers for ACP session lifecycle."""

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from api.models.acp import AgentCapabilities
from api.models.config import ApiServerConfig
from api.models.session import (
    SessionCloseParams,
    SessionDeleteParams,
    SessionListParams,
    SessionLoadParams,
    SessionNewParams,
    SessionResumeParams,
    SystemPromptConfig,
)
from api.utils.security import sanitize_meta_dict

SRC_ROOT: Path = Path(__file__).resolve().parents[2]

SESSION_METHODS: frozenset[str] = frozenset(
    {
        "session/new",
        "session/load",
        "session/resume",
        "session/list",
        "session/close",
        "session/delete",
    }
)


def check_session_capability(method: str, caps: AgentCapabilities) -> str | None:
    """Return ``None`` when ``method`` is supported by ``caps``, or a denial message."""
    sc = caps.session_capabilities
    support_by_method: dict[str, bool] = {
        "session/new": True,
        "session/list": sc.list is not None,
        "session/load": bool(caps.load_session or sc.load is not None),
        "session/resume": sc.resume is not None,
        "session/close": sc.close is not None,
        "session/delete": sc.delete is not None,
    }
    if support_by_method.get(method, True):
        return None
    return f"Agent did not advertise capability support for '{method}'"


def validate_absolute_dir(raw_path: str, *, label: str) -> str:
    """Verify ``raw_path`` is an absolute path to an existing directory on disk."""
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {raw_path!r}")
    resolved = candidate.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"{label} directory does not exist: {raw_path!r}")
    return str(resolved)


def validate_additional_dirs(
    dirs: Sequence[str] | None,
    caps: AgentCapabilities,
) -> list[str] | None:
    """Validate ``additionalDirectories`` capability support and directory paths."""
    if dirs is None:
        return None
    if caps.session_capabilities.additional_directories is None:
        raise ValueError("Agent did not advertise sessionCapabilities.additionalDirectories")
    return [validate_absolute_dir(entry, label="additionalDirectories") for entry in dirs]


def validate_system_prompt_config(
    prompt: SystemPromptConfig | None,
    caps: AgentCapabilities,
) -> dict[str, Any] | None:
    """Validate ``systemPrompt`` capability and mode support against ``caps``."""
    if prompt is None:
        return None
    sys_cap = caps.session_capabilities.system_prompt
    if sys_cap is None:
        raise ValueError("Agent did not advertise sessionCapabilities.systemPrompt")
    modes = sys_cap.get("mode") if isinstance(sys_cap, Mapping) else None
    if prompt.mode == "override":
        if not isinstance(modes, Mapping) or modes.get("override") is None:
            raise ValueError("Agent did not advertise systemPrompt override mode")
    elif prompt.mode != "append" and (
        not isinstance(modes, Mapping) or modes.get(prompt.mode) is None
    ):
        raise ValueError(f"Unsupported systemPrompt mode: {prompt.mode!r}")
    return prompt.model_dump(by_alias=True)


def build_cbi_stdio_mcp_server(db_path: Path) -> dict[str, Any]:
    """Build the trusted server-owned ``codebase-indexer`` stdio MCP descriptor."""
    return {
        "name": "codebase-indexer",
        "command": sys.executable,
        "args": ["-m", "cli.main", "serve", "--db-path", str(db_path.resolve())],
        "env": [{"name": "PYTHONPATH", "value": str(SRC_ROOT)}],
    }


def _validate_remote_mcp_entry(
    entry: Mapping[str, Any],
    caps: AgentCapabilities,
) -> dict[str, Any]:
    """Reject client-supplied stdio servers and validate HTTP/SSE MCP transport support."""
    transport = entry.get("type", "stdio")
    if transport == "stdio":
        raise ValueError("Client-supplied stdio MCP servers are blocked for security")
    if transport == "http" and not caps.mcp_capabilities.http:
        raise ValueError("Agent did not advertise mcpCapabilities.http")
    if transport == "sse" and not caps.mcp_capabilities.sse:
        raise ValueError("Agent did not advertise mcpCapabilities.sse")
    if transport not in ("http", "sse"):
        raise ValueError(f"Unsupported MCP transport type: {transport!r}")

    name = entry.get("name")
    url = entry.get("url")
    headers = entry.get("headers")
    if not isinstance(name, str) or not name or not isinstance(url, str) or not url:
        raise ValueError("Remote MCP server requires non-empty 'name' and 'url'")
    if not isinstance(headers, list):
        raise ValueError("Remote MCP server requires 'headers' array")
    return {"type": transport, "name": name, "url": url, "headers": headers}


def sanitize_session_mcp_servers(
    raw_servers: Sequence[Mapping[str, Any]],
    caps: AgentCapabilities,
    *,
    include_cbi_mcp: bool,
    cbi_db_path: Path | None,
) -> list[dict[str, Any]]:
    """Validate remote MCP entries and optionally inject curated ``codebase-indexer`` MCP."""
    validated = [_validate_remote_mcp_entry(srv, caps) for srv in raw_servers]
    if include_cbi_mcp and cbi_db_path is not None:
        validated.append(build_cbi_stdio_mcp_server(cbi_db_path))
    return validated


def _extract_meta_with_cbi_flag(
    meta: Mapping[str, Any] | None,
    *,
    enable_cbi_mcp: bool,
) -> tuple[bool, dict[str, Any] | None]:
    """Consume ``_meta.includeCbiMcp`` and scrub remaining metadata keys."""
    if not meta:
        return False, None
    raw_flag = meta.get("includeCbiMcp")
    include_cbi = bool(raw_flag) and enable_cbi_mcp if isinstance(raw_flag, bool) else False
    remainder = {k: v for k, v in meta.items() if k != "includeCbiMcp"}
    return include_cbi, sanitize_meta_dict(remainder)


def _build_workspace_session_params(
    parsed: SessionNewParams | SessionLoadParams | SessionResumeParams,
    caps: AgentCapabilities,
    config: ApiServerConfig,
) -> dict[str, Any]:
    """Assemble sanitized outbound payload for ``session/new``, ``load``, or ``resume``."""
    include_cbi, scrubbed_meta = _extract_meta_with_cbi_flag(
        parsed.meta,
        enable_cbi_mcp=config.enable_cbi_mcp,
    )
    payload: dict[str, Any] = {
        "cwd": validate_absolute_dir(parsed.cwd, label="cwd"),
        "mcpServers": sanitize_session_mcp_servers(
            parsed.mcp_servers,
            caps,
            include_cbi_mcp=include_cbi,
            cbi_db_path=config.cbi_db_path,
        ),
    }
    if isinstance(parsed, (SessionLoadParams, SessionResumeParams)):
        payload["sessionId"] = parsed.session_id

    extra_dirs = validate_additional_dirs(parsed.additional_directories, caps)
    if extra_dirs is not None:
        payload["additionalDirectories"] = extra_dirs

    sys_prompt = validate_system_prompt_config(parsed.system_prompt, caps)
    if sys_prompt is not None:
        payload["systemPrompt"] = sys_prompt

    if scrubbed_meta is not None:
        payload["_meta"] = scrubbed_meta
    return payload


def _build_list_params(raw_params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and sanitize ``session/list`` filter and pagination parameters."""
    parsed = SessionListParams.model_validate(raw_params)
    payload: dict[str, Any] = {}
    if parsed.cwd is not None:
        payload["cwd"] = validate_absolute_dir(parsed.cwd, label="cwd")
    if parsed.cursor is not None:
        payload["cursor"] = parsed.cursor
    scrubbed_meta = sanitize_meta_dict(parsed.meta)
    if scrubbed_meta is not None:
        payload["_meta"] = scrubbed_meta
    return payload


def prepare_session_params(
    method: str,
    raw_params: object,
    caps: AgentCapabilities,
    config: ApiServerConfig,
) -> dict[str, Any]:
    """Validate and sanitize parameters for any ``session/*`` setup, list, or delete call."""
    if raw_params is None:
        params_map: Mapping[str, Any] = {}
    elif isinstance(raw_params, Mapping):
        params_map = raw_params
    else:
        raise ValueError(f"Invalid {method} params: expected JSON object")

    if method == "session/new":
        return _build_workspace_session_params(
            SessionNewParams.model_validate(params_map),
            caps,
            config,
        )
    if method == "session/load":
        return _build_workspace_session_params(
            SessionLoadParams.model_validate(params_map),
            caps,
            config,
        )
    if method == "session/resume":
        return _build_workspace_session_params(
            SessionResumeParams.model_validate(params_map),
            caps,
            config,
        )
    if method == "session/list":
        return _build_list_params(params_map)

    parsed_id = (
        SessionCloseParams.model_validate(params_map)
        if method == "session/close"
        else SessionDeleteParams.model_validate(params_map)
    )
    out: dict[str, Any] = {"sessionId": parsed_id.session_id}
    scrubbed_meta = sanitize_meta_dict(parsed_id.meta)
    if scrubbed_meta is not None:
        out["_meta"] = scrubbed_meta
    return out


__all__ = [
    "SESSION_METHODS",
    "SRC_ROOT",
    "build_cbi_stdio_mcp_server",
    "check_session_capability",
    "prepare_session_params",
    "sanitize_session_mcp_servers",
    "validate_absolute_dir",
    "validate_additional_dirs",
    "validate_system_prompt_config",
]
