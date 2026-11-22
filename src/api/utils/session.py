"""Stateless validation, MCP policy enforcement, and root-set extraction for sessions."""

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError as PydanticValidationError

from api.models.acp import AgentCapabilities
from api.models.config import AcpServerConfig, McpPolicyConfig
from api.models.session import (
    McpServerHttp,
    McpServerSse,
    McpServerStdio,
    SessionCloseParams,
    SessionDeleteParams,
    SessionListParams,
    SessionLoadParams,
    SessionNewParams,
    SessionResumeParams,
    SessionRootSet,
    SystemPromptConfig,
)
from api.utils.errors import pydantic_error_to_acp_error
from api.utils.security import sanitize_meta_dict
from core.models.exceptions import AcpInvalidParamsError, AcpMethodNotFoundError, AcpSecurityError

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


def check_session_capability(method: str, caps: AgentCapabilities) -> None:
    """Verify ``method`` is advertised by ``caps``, raising ``-32601`` otherwise."""
    sc = caps.session_capabilities
    support_by_method: dict[str, bool] = {
        "session/new": True,
        "session/list": sc.list is not None,
        "session/load": bool(caps.load_session or sc.load is not None),
        "session/resume": sc.resume is not None,
        "session/close": sc.close is not None,
        "session/delete": sc.delete is not None,
    }
    if not support_by_method.get(method, True):
        raise AcpMethodNotFoundError(f"Agent did not advertise capability support for '{method}'")


def validate_absolute_dir(raw_path: str, *, label: str) -> str:
    """Verify ``raw_path`` is an absolute path to an existing directory on disk."""
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise AcpInvalidParamsError(f"{label} must be an absolute path: {raw_path!r}")
    resolved = candidate.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise AcpInvalidParamsError(f"{label} directory does not exist: {raw_path!r}")
    return str(resolved)


def validate_additional_dirs(
    dirs: Sequence[str] | None,
    caps: AgentCapabilities,
) -> list[str] | None:
    """Validate ``additionalDirectories`` capability support and directory paths."""
    if dirs is None:
        return None
    if caps.session_capabilities.additional_directories is None:
        raise AcpInvalidParamsError(
            "Agent did not advertise sessionCapabilities.additionalDirectories"
        )
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
        raise AcpInvalidParamsError("Agent did not advertise sessionCapabilities.systemPrompt")
    if prompt.mode == "append":
        return prompt.model_dump(by_alias=True, exclude_none=True)
    if prompt.mode == "override":
        if sys_cap.mode is None or sys_cap.mode.override is None:
            raise AcpInvalidParamsError("Agent did not advertise systemPrompt override mode")
        return prompt.model_dump(by_alias=True, exclude_none=True)
    raise AcpInvalidParamsError(f"Unsupported systemPrompt mode: {prompt.mode!r}")


def build_cbi_stdio_mcp_server(db_path: Path) -> dict[str, Any]:
    """Build the trusted server-owned ``codebase-indexer`` stdio MCP descriptor."""
    return {
        "name": "codebase-indexer",
        "command": sys.executable,
        "args": ["-m", "cli.main", "serve", "--db-path", str(db_path.resolve())],
        "env": [{"name": "PYTHONPATH", "value": str(SRC_ROOT)}],
    }


def _validate_stdio_mcp_entry(
    entry: Mapping[str, Any],
    mcp_policy: McpPolicyConfig,
) -> dict[str, Any]:
    """Validate and enforce fail-closed policy for client-supplied stdio MCP servers."""
    try:
        parsed = McpServerStdio.model_validate(entry)
    except PydanticValidationError as exc:
        raise pydantic_error_to_acp_error(exc, label="stdio MCP server") from exc

    cmd_path = Path(parsed.command)
    if not cmd_path.is_absolute():
        raise AcpInvalidParamsError("Stdio MCP server 'command' must be an absolute path")

    allowed_cmds = set(mcp_policy.allowed_stdio_commands)
    is_allowed = mcp_policy.allow_client_stdio or (
        parsed.command in allowed_cmds or cmd_path.name in allowed_cmds
    )
    if not is_allowed:
        raise AcpSecurityError("Client-supplied stdio MCP servers are blocked by security policy")
    return parsed.model_dump(by_alias=True, exclude_none=True)


def _validate_remote_mcp_url(url: str, *, allow_insecure_http: bool) -> None:
    """Validate remote MCP endpoint scheme and reject embedded credentials."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError as exc:
        raise AcpInvalidParamsError(f"Invalid MCP server URL: {exc}") from exc
    scheme = parsed.scheme.lower()
    allowed = ("https", "http") if allow_insecure_http else ("https",)
    if scheme not in allowed or not parsed.netloc:
        raise AcpSecurityError("Remote MCP server URL must use an allowed HTTPS scheme and host")
    if parsed.username or parsed.password:
        raise AcpSecurityError("Remote MCP server URL must not embed credentials")


def _select_remote_mcp_model(
    transport: object,
    caps: AgentCapabilities,
    mcp_policy: McpPolicyConfig,
) -> type[McpServerHttp] | type[McpServerSse]:
    """Verify HTTP/SSE MCP transport policy & capabilities and return model class."""
    if transport == "http":
        if not mcp_policy.allow_http:
            raise AcpSecurityError("HTTP MCP servers are disabled by server policy")
        if not caps.mcp_capabilities.http:
            raise AcpInvalidParamsError("Agent did not advertise mcpCapabilities.http")
        return McpServerHttp
    if transport == "sse":
        if not mcp_policy.allow_sse:
            raise AcpSecurityError("SSE MCP servers are disabled by server policy")
        if not caps.mcp_capabilities.sse:
            raise AcpInvalidParamsError("Agent did not advertise mcpCapabilities.sse")
        return McpServerSse
    raise AcpInvalidParamsError(f"Unsupported MCP transport type: {transport!r}")


def _validate_remote_mcp_entry(
    entry: Mapping[str, Any],
    caps: AgentCapabilities,
    mcp_policy: McpPolicyConfig,
) -> dict[str, Any]:
    """Validate HTTP/SSE/stdio MCP server transport, agent capabilities, and URL hygiene."""
    transport = entry.get("type", "stdio")
    if transport == "stdio":
        return _validate_stdio_mcp_entry(entry, mcp_policy)
    model_cls = _select_remote_mcp_model(transport, caps, mcp_policy)
    try:
        parsed = model_cls.model_validate(entry)
    except PydanticValidationError as exc:
        raise pydantic_error_to_acp_error(exc, label="remote MCP server") from exc
    _validate_remote_mcp_url(parsed.url, allow_insecure_http=mcp_policy.allow_insecure_http_urls)
    return parsed.model_dump(by_alias=True, exclude_none=True)


def sanitize_session_mcp_servers(
    raw_servers: Sequence[Mapping[str, Any]],
    caps: AgentCapabilities,
    mcp_policy: McpPolicyConfig,
    *,
    include_cbi_mcp: bool = False,
) -> list[dict[str, Any]]:
    """Validate MCP servers against ``mcp_policy`` and optionally inject curated CBI MCP."""
    validated = [_validate_remote_mcp_entry(srv, caps, mcp_policy) for srv in raw_servers]
    if include_cbi_mcp and mcp_policy.enable_cbi_mcp and mcp_policy.cbi_db_path is not None:
        validated.append(build_cbi_stdio_mcp_server(mcp_policy.cbi_db_path))
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
    mcp_policy: McpPolicyConfig,
) -> tuple[dict[str, Any], SessionRootSet]:
    """Assemble sanitized payload and ``SessionRootSet`` for ``new``/``load``/``resume``."""
    include_cbi, scrubbed_meta = _extract_meta_with_cbi_flag(
        parsed.meta,
        enable_cbi_mcp=mcp_policy.enable_cbi_mcp,
    )
    resolved_cwd = validate_absolute_dir(parsed.cwd, label="cwd")
    extra_dirs = validate_additional_dirs(parsed.additional_directories, caps)
    payload: dict[str, Any] = {
        "cwd": resolved_cwd,
        "mcpServers": sanitize_session_mcp_servers(
            parsed.mcp_servers,
            caps,
            mcp_policy,
            include_cbi_mcp=include_cbi,
        ),
    }
    if isinstance(parsed, (SessionLoadParams, SessionResumeParams)):
        payload["sessionId"] = parsed.session_id
    if extra_dirs is not None:
        payload["additionalDirectories"] = extra_dirs
    sys_prompt = validate_system_prompt_config(parsed.system_prompt, caps)
    if sys_prompt is not None:
        payload["systemPrompt"] = sys_prompt
    if scrubbed_meta is not None:
        payload["_meta"] = scrubbed_meta

    root_set = SessionRootSet(
        cwd=resolved_cwd,
        additional_directories=tuple(extra_dirs or ()),
    )
    return payload, root_set


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


def _parse_setup_model(
    method: str,
    params_map: Mapping[str, Any],
) -> SessionNewParams | SessionLoadParams | SessionResumeParams:
    """Validate ``session/new``, ``session/load``, or ``session/resume`` request params."""
    if method == "session/new":
        return SessionNewParams.model_validate(params_map)
    if method == "session/load":
        return SessionLoadParams.model_validate(params_map)
    return SessionResumeParams.model_validate(params_map)


def prepare_session_params(
    method: str,
    raw_params: object,
    caps: AgentCapabilities,
    config: AcpServerConfig,
) -> tuple[dict[str, Any], SessionRootSet | None]:
    """Validate capability & params for ``session/*`` calls, returning ``(payload, root_set)``."""
    check_session_capability(method, caps)
    if raw_params is None:
        params_map: Mapping[str, Any] = {}
    elif isinstance(raw_params, Mapping):
        params_map = raw_params
    else:
        raise AcpInvalidParamsError(f"Invalid {method} params: expected JSON object")

    try:
        if method in ("session/new", "session/load", "session/resume"):
            parsed_setup = _parse_setup_model(method, params_map)
            return _build_workspace_session_params(parsed_setup, caps, config.mcp)
        if method == "session/list":
            return _build_list_params(params_map), None
        parsed_id = (
            SessionCloseParams.model_validate(params_map)
            if method == "session/close"
            else SessionDeleteParams.model_validate(params_map)
        )
    except PydanticValidationError as exc:
        raise pydantic_error_to_acp_error(exc, label=method) from exc

    out: dict[str, Any] = {"sessionId": parsed_id.session_id}
    scrubbed_meta = sanitize_meta_dict(parsed_id.meta)
    if scrubbed_meta is not None:
        out["_meta"] = scrubbed_meta
    return out, None


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
