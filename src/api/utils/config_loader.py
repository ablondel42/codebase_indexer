"""Stateless ACP server configuration resolver powered by ``pydantic-settings`` & ``dotenv``."""

import os
import tomllib
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import SettingsError

from api.models.config import ACP_SETTINGS_SOURCES_CTX, AcpServerConfig, AcpSettings
from core.models.exceptions import AcpConfigError


def _strip_none_overrides(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """Recursively strip ``None`` CLI defaults so they do not mask env or TOML values."""
    if not mapping:
        return {}
    cleaned: dict[str, Any] = {}
    for key, val in mapping.items():
        if val is None:
            continue
        if isinstance(val, Mapping):
            nested = _strip_none_overrides(val)
            if nested:
                cleaned[key] = nested
        else:
            cleaned[key] = val
    return cleaned


@contextmanager
def _scoped_environ(environ: Mapping[str, str] | None) -> Generator[None, None, None]:
    """Temporarily replace ``os.environ`` when an explicit mapping is supplied."""
    if environ is None:
        yield
        return
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def resolve_acp_server_config(
    *,
    config_file: Path | None = None,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> AcpServerConfig:
    """Resolve ``AcpServerConfig`` via ``TOML -> .env -> CBI_ACP__* env -> CLI``."""
    if config_file is not None:
        try:
            config_file.read_bytes()
        except OSError as exc:
            raise AcpConfigError(f"Failed reading ACP config file '{config_file}': {exc}") from exc

    effective_dotenv = (
        env_file if env_file is not None else (Path(".env") if environ is None else None)
    )
    if environ is None and effective_dotenv is not None and effective_dotenv.is_file():
        load_dotenv(effective_dotenv, override=False)

    cleaned_cli = _strip_none_overrides(cli_overrides)
    ctx_token = ACP_SETTINGS_SOURCES_CTX.set((config_file, effective_dotenv))
    try:
        with _scoped_environ(environ):
            loaded = AcpSettings(**cleaned_cli)
    except tomllib.TOMLDecodeError as exc:
        raise AcpConfigError(f"Malformed TOML in ACP config file '{config_file}': {exc}") from exc
    except (OSError, SettingsError, PydanticValidationError) as exc:
        raise AcpConfigError(f"Invalid ACP server configuration: {exc}") from exc
    finally:
        ACP_SETTINGS_SOURCES_CTX.reset(ctx_token)

    return AcpServerConfig(
        transport=loaded.transport,
        agent=loaded.agent,
        capabilities=loaded.capabilities,
        mcp=loaded.mcp,
        limits=loaded.limits,
        logging=loaded.logging,
    )


__all__ = [
    "resolve_acp_server_config",
]
