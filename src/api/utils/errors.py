"""Stateless JSON-RPC 2.0 wire error serialization, redaction, and exception mapping."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.jsonrpc import JsonRpcErrorObject, JsonRpcErrorResponse
from core.models.exceptions import AcpError, AcpInvalidParamsError

_SENSITIVE_ERROR_KEYS: frozenset[str] = frozenset(
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
        "auth_token",
        "bearer",
        "private_key",
    }
)


def _sanitize_named_secret_pairs(entries: list[Any] | tuple[Any, ...]) -> list[Any]:
    """Redact ``value`` fields in ``env``/``headers`` ``{name, value}`` lists."""
    sanitized: list[Any] = []
    for item in entries:
        if isinstance(item, Mapping) and "name" in item and "value" in item:
            copied = {str(k): _sanitize_error_value(v) for k, v in item.items()}
            copied["value"] = "[REDACTED]"
            sanitized.append(copied)
        else:
            sanitized.append(_sanitize_error_value(item))
    return sanitized


def _sanitize_error_value(value: Any) -> Any:
    """Recursively redact sensitive keys and ``env``/``headers`` values."""
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, raw_val in value.items():
            key_str = str(key)
            lowered = key_str.lower()
            if lowered in _SENSITIVE_ERROR_KEYS:
                cleaned[key_str] = "[REDACTED]"
            elif lowered in ("env", "headers") and isinstance(raw_val, (list, tuple)):
                cleaned[key_str] = _sanitize_named_secret_pairs(raw_val)
            else:
                cleaned[key_str] = _sanitize_error_value(raw_val)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_error_value(item) for item in value]
    return value


def sanitize_error_data(data: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a JSON-safe redacted copy of ``data``, or ``None`` if empty."""
    if not data:
        return None
    sanitized = _sanitize_error_value(data)
    return sanitized if isinstance(sanitized, dict) and sanitized else None


def pydantic_error_to_acp_error(
    exc: PydanticValidationError,
    *,
    label: str,
) -> AcpInvalidParamsError:
    """Convert a Pydantic ``ValidationError`` into a redacted ``AcpInvalidParamsError``."""
    fields: list[str] = []
    for err in exc.errors(include_url=False, include_context=False):
        loc = err.get("loc", ())
        dotted = ".".join(str(part) for part in loc)
        if dotted and dotted not in fields:
            fields.append(dotted)
    wire_data = {"fields": fields} if fields else None
    return AcpInvalidParamsError(f"Invalid {label} parameters", wire_data=wire_data)


def build_jsonrpc_error_dict(
    req_id: int | str | None,
    code: int,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error dict with redacted optional ``data``."""
    safe_data = sanitize_error_data(data)
    envelope = JsonRpcErrorResponse(
        id=req_id,
        error=JsonRpcErrorObject(code=code, message=message, data=safe_data),
    )
    payload = envelope.model_dump()
    if safe_data is None:
        payload["error"].pop("data", None)
    return payload


def format_jsonrpc_error(
    req_id: int | str | None,
    code: int,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> str:
    """Serialize a compact JSON-RPC 2.0 error response frame preserving ``id: null``."""
    payload = build_jsonrpc_error_dict(req_id, code, message, data=data)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def exception_to_jsonrpc_frame(
    exc: AcpError,
    req_id: int | str | None,
    *,
    is_notification: bool = False,
    debug: bool = False,
) -> str | None:
    """Serialize an ``AcpError`` to a JSON-RPC frame, or ``None`` for notifications."""
    if is_notification:
        return None
    message = exc.message if (exc.expose_message or debug) else exc.wire_message
    data = exc.wire_data if (exc.expose_message or debug) else None
    return format_jsonrpc_error(req_id, exc.jsonrpc_code, message, data=data)


__all__ = [
    "build_jsonrpc_error_dict",
    "exception_to_jsonrpc_frame",
    "format_jsonrpc_error",
    "pydantic_error_to_acp_error",
    "sanitize_error_data",
]
