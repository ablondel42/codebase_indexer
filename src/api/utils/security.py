"""Stateless security, bind-host, origin, env, and capability-clamping helpers."""

import ipaddress
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from api.models.acp import (
    LATEST_ACP_VERSION,
    AuthenticateParams,
    AuthMethod,
    InitializeParams,
)
from api.models.config import ApiServerConfig
from core.models.exceptions import AcpError
from core.utils.logging import get_logger

logger = get_logger("api.utils.security")

FORBIDDEN_META_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "password",
        "cookie",
        "set-cookie",
        "traceparent",
        "tracestate",
        "baggage",
    }
)

SAFE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "TERM",
        "COLORTERM",
        "SYSTEMROOT",
        "APPDATA",
        "LOCALAPPDATA",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)

SAFE_ENV_PREFIXES: tuple[str, ...] = (
    "LC_",
    "XDG_",
    "GOOGLE_",
    "GEMINI_",
    "AGY_",
    "ANTIGRAVITY_",
    "CLOUDSDK_",
)


def is_loopback_host(host: str) -> bool:
    """Return ``True`` when ``host`` is ``localhost`` or a loopback IP literal."""
    cleaned = host.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if not cleaned:
        return False
    if cleaned.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError as exc:
        logger.debug("Rejecting non-IP non-localhost host %r: %s", host, exc)
        return False


def validate_bind_host(host: str, *, allow_non_loopback: bool) -> str:
    """Validate that server bind ``host`` is loopback unless explicitly overridden."""
    cleaned = host.strip()
    if not cleaned:
        raise AcpError("API server bind host cannot be empty.")
    if not allow_non_loopback and not is_loopback_host(cleaned):
        raise AcpError(
            f"Refusing non-loopback bind host '{cleaned}' without allow_non_loopback=True.",
            context={"host": cleaned},
        )
    return cleaned


def extract_host_name(host_header: str) -> str:
    """Extract lowercase hostname without port from an HTTP ``Host`` header."""
    raw = host_header.strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(f"//{raw}")
    except ValueError as exc:
        logger.debug("Invalid Host header %r: %s", host_header, exc)
        return ""
    return (parsed.hostname or "").lower()


def is_allowed_ws_host(host_header: str | None, *, allow_non_loopback: bool = False) -> bool:
    """Validate WebSocket ``Host`` header against DNS-rebinding attacks."""
    if not host_header:
        return False
    hostname = extract_host_name(host_header)
    if not hostname:
        return False
    return True if allow_non_loopback else is_loopback_host(hostname)


def normalize_origin(origin: str) -> str:
    """Normalize an HTTP(S) ``Origin`` to ``scheme://netloc`` in lowercase."""
    raw = origin.strip().rstrip("/")
    if not raw or raw.lower() == "null":
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        logger.debug("Invalid Origin header %r: %s", origin, exc)
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def is_allowed_ws_origin(origin_header: str | None, allowed_origins: Sequence[str]) -> bool:
    """Return ``True`` when WebSocket ``Origin`` strictly matches an allowlisted origin."""
    if not origin_header:
        return False
    normalized = normalize_origin(origin_header)
    if not normalized:
        return False
    allowed = {item for entry in allowed_origins if (item := normalize_origin(entry))}
    return normalized in allowed


def build_agy_acp_command(config: ApiServerConfig) -> tuple[str, ...]:
    """Build argument vector for launching ``agy acp`` without shell expansion."""
    if not config.agy_command:
        raise AcpError("ACP agent command cannot be empty.")
    cmd = list(config.agy_command)
    is_agy = Path(cmd[0]).name == "agy"
    if is_agy and config.agy_sandbox and "--sandbox" not in cmd:
        cmd.append("--sandbox")
    return tuple(cmd)


def _is_safe_env_key(key: str) -> bool:
    """Return ``True`` when an environment variable key is allowlisted for ``agy``."""
    return key in SAFE_ENV_KEYS or key.startswith(SAFE_ENV_PREFIXES)


def build_sanitized_agy_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an allowlisted environment mapping for the ``agy acp`` child process."""
    source = os.environ if base_env is None else base_env
    return {key: value for key, value in source.items() if _is_safe_env_key(key)}


def sanitize_meta_dict(meta: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Strip credentials and trace-propagation headers from ACP ``_meta`` dictionaries."""
    if not meta:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in meta.items():
        if isinstance(key, str) and key.lower() not in FORBIDDEN_META_KEYS:
            nested = sanitize_meta_dict(value) if isinstance(value, Mapping) else value
            if nested is not None or not isinstance(value, Mapping):
                cleaned[key] = nested
    return cleaned or None


def clamp_client_capabilities(params: InitializeParams) -> dict[str, Any]:
    """Clamp client ``initialize`` parameters to safe bridge-supported capabilities."""
    safe_caps: dict[str, Any] = {
        "auth": {"terminal": False},
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }
    raw_caps = params.client_capabilities
    if raw_caps.elicitation is not None:
        safe_caps["elicitation"] = raw_caps.elicitation
    if raw_caps.plan is not None:
        safe_caps["plan"] = raw_caps.plan
    if raw_caps.session is not None:
        safe_caps["session"] = raw_caps.session
    if raw_caps.subagents is not None:
        safe_caps["subagents"] = raw_caps.subagents

    sanitized: dict[str, Any] = {
        "protocolVersion": min(params.protocol_version, LATEST_ACP_VERSION),
        "clientCapabilities": safe_caps,
    }
    if params.client_info is not None:
        sanitized["clientInfo"] = params.client_info.model_dump(
            by_alias=True,
            exclude_none=True,
        )
    scrubbed_meta = sanitize_meta_dict(params.meta)
    if scrubbed_meta is not None:
        sanitized["_meta"] = scrubbed_meta
    return sanitized


def find_advertised_auth_method(
    method_id: str,
    auth_methods: Sequence[AuthMethod],
) -> AuthMethod | None:
    """Locate ``method_id`` among ``authMethods`` advertised during ``initialize``."""
    for method in auth_methods:
        if method.id == method_id:
            return method
    return None


def sanitize_authenticate_params(params: AuthenticateParams) -> dict[str, Any]:
    """Scrub ``authenticate`` ``_meta`` while allowing ``apiKey`` for ``gemini-api-key``."""
    scrubbed_meta = sanitize_meta_dict(params.meta) or {}
    if params.method_id == "gemini-api-key" and params.meta is not None:
        raw_key = params.meta.get("apiKey")
        if isinstance(raw_key, str) and raw_key.strip():
            scrubbed_meta["apiKey"] = raw_key.strip()
    sanitized: dict[str, Any] = {"methodId": params.method_id}
    if scrubbed_meta:
        sanitized["_meta"] = scrubbed_meta
    return sanitized


__all__ = [
    "FORBIDDEN_META_KEYS",
    "SAFE_ENV_KEYS",
    "SAFE_ENV_PREFIXES",
    "build_agy_acp_command",
    "build_sanitized_agy_env",
    "clamp_client_capabilities",
    "extract_host_name",
    "find_advertised_auth_method",
    "is_allowed_ws_host",
    "is_allowed_ws_origin",
    "is_loopback_host",
    "normalize_origin",
    "sanitize_authenticate_params",
    "sanitize_meta_dict",
    "validate_bind_host",
]
