"""Stateless 3-layer ACP server configuration loader (TOML -> env -> CLI)."""

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.config import AcpServerConfig
from core.models.exceptions import AcpConfigError

_ENV_PREFIX = "CBI_ACP_"

_LIST_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("transport", "allowed_origins"),
        ("agent", "command"),
        ("agent", "extra_env_allowlist"),
        ("capabilities", "terminal_allowed_commands"),
        ("capabilities", "allowed_extension_prefixes"),
        ("mcp", "allowed_stdio_commands"),
    }
)

_SHORTCUT_ENV_MAP: dict[str, tuple[str, str]] = {
    "CBI_ACP_HOST": ("transport", "host"),
    "CBI_ACP_PORT": ("transport", "port"),
    "CBI_ACP_WS_PATH": ("transport", "ws_path"),
    "CBI_ACP_ALLOW_NON_LOOPBACK": ("transport", "allow_non_loopback"),
    "CBI_ACP_AUTH_TOKEN": ("transport", "auth_token"),
    "CBI_ACP_ALLOWED_ORIGINS": ("transport", "allowed_origins"),
    "CBI_ACP_MAX_CONNECTIONS": ("transport", "max_connections"),
    "CBI_ACP_MAX_FRAME_BYTES": ("transport", "max_frame_bytes"),
    "CBI_ACP_WORKSPACE_ROOT": ("agent", "workspace_root"),
    "CBI_ACP_FS_READ": ("capabilities", "fs_read"),
    "CBI_ACP_FS_WRITE": ("capabilities", "fs_write"),
    "CBI_ACP_TERMINAL": ("capabilities", "terminal"),
    "CBI_ACP_AUTH_TERMINAL": ("capabilities", "auth_terminal"),
    "CBI_ACP_LOG_LEVEL": ("logging", "level"),
    "CBI_ACP_LOG_FORMAT": ("logging", "format"),
    "CBI_ACP_LOG_FILE": ("logging", "log_file"),
}

_SECTION_NAMES: frozenset[str] = frozenset(
    {"transport", "agent", "capabilities", "mcp", "limits", "logging"}
)


def load_toml_config_file(path: Path) -> dict[str, Any]:
    """Load and parse an optional TOML configuration file into a nested dict."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise AcpConfigError(f"Failed reading ACP config file '{path}': {exc}") from exc
    try:
        loaded = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AcpConfigError(f"Malformed TOML in ACP config file '{path}': {exc}") from exc
    if not isinstance(loaded, dict):
        raise AcpConfigError(f"ACP config file '{path}' must decode to a TOML table.")
    return loaded


def _parse_env_scalar(section: str, field: str, raw_val: str) -> Any:
    """Coerce an environment string into list/bool/int/string matching target field."""
    cleaned = raw_val.strip()
    if (section, field) in _LIST_FIELDS:
        if cleaned.startswith("["):
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise AcpConfigError(
                    f"Invalid JSON list in env var for {section}.{field}: {exc}"
                ) from exc
            if not isinstance(parsed, list):
                raise AcpConfigError(f"Env var for {section}.{field} must decode to a JSON array.")
            return [str(item) for item in parsed]
        return [part.strip() for part in cleaned.split(",") if part.strip()]
    lowered = cleaned.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    return cleaned


def load_env_config_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Extract ``CBI_ACP_*`` environment overrides into nested section dicts."""
    source = os.environ if environ is None else environ
    result: dict[str, dict[str, Any]] = {}
    for key, raw_val in source.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        mapped = _SHORTCUT_ENV_MAP.get(key)
        if mapped is not None:
            section, field = mapped
            result.setdefault(section, {})[field] = _parse_env_scalar(section, field, raw_val)
            continue
        suffix = key[len(_ENV_PREFIX) :]
        if "__" not in suffix:
            continue
        section_raw, field_raw = suffix.split("__", 1)
        section = section_raw.lower()
        field = field_raw.lower()
        if section not in _SECTION_NAMES or not field:
            continue
        result.setdefault(section, {})[field] = _parse_env_scalar(section, field, raw_val)
    return result


def deep_merge_config_dicts(*layers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deep-merge nested mapping layers left-to-right, skipping ``None`` values."""
    merged: dict[str, Any] = {}
    for layer in layers:
        if not layer:
            continue
        for key, val in layer.items():
            if val is None:
                continue
            existing = merged.get(key)
            if isinstance(existing, Mapping) and isinstance(val, Mapping):
                merged[key] = deep_merge_config_dicts(existing, val)
            elif isinstance(val, Mapping):
                merged[key] = deep_merge_config_dicts(val)
            else:
                merged[key] = val
    return merged


def resolve_acp_server_config(
    *,
    config_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> AcpServerConfig:
    """Build and validate ``AcpServerConfig`` using defaults -> TOML -> env -> CLI."""
    toml_layer = load_toml_config_file(config_file) if config_file is not None else None
    env_layer = load_env_config_overrides(environ)
    merged = deep_merge_config_dicts(toml_layer, env_layer, cli_overrides)
    try:
        return AcpServerConfig.model_validate(merged)
    except PydanticValidationError as exc:
        raise AcpConfigError(f"Invalid ACP server configuration: {exc}") from exc


__all__ = [
    "deep_merge_config_dicts",
    "load_env_config_overrides",
    "load_toml_config_file",
    "resolve_acp_server_config",
]
