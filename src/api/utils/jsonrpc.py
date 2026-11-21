"""Stateless JSON-RPC 2.0 parsing and ACP stdio NDJSON framing helpers."""

import json
from collections.abc import Mapping
from typing import Any

from api.models.acp import (
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    JsonRpcErrorObject,
    JsonRpcErrorResponse,
)
from core.models.exceptions import AcpError
from core.utils.logging import get_logger

logger = get_logger("api.utils.jsonrpc")


def extract_request_id(payload: Mapping[str, Any]) -> int | str | None:
    """Return a valid JSON-RPC ``id`` (``str`` or ``int``, excluding ``bool``), or ``None``."""
    raw_id = payload.get("id")
    if isinstance(raw_id, bool):
        return None
    if isinstance(raw_id, (int, str)):
        return raw_id
    return None


def format_jsonrpc_error(
    req_id: int | str | None,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> str:
    """Serialize a compact JSON-RPC 2.0 error response frame preserving ``id: null``."""
    envelope = JsonRpcErrorResponse(
        id=req_id,
        error=JsonRpcErrorObject(code=code, message=message, data=data),
    )
    payload = envelope.model_dump()
    if data is None:
        payload["error"].pop("data", None)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def parse_client_frame(
    raw_text: str,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Decode and validate one incoming JSON-RPC 2.0 WebSocket frame against ACP rules."""
    if len(raw_text.encode("utf-8")) > max_bytes:
        return None, format_jsonrpc_error(
            None,
            JSONRPC_INVALID_REQUEST,
            "Frame exceeds maximum allowed size",
        )
    try:
        loaded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.debug("Invalid JSON received from /acp client: %s", exc)
        return None, format_jsonrpc_error(None, JSONRPC_PARSE_ERROR, "Parse error")

    if not isinstance(loaded, dict):
        return None, format_jsonrpc_error(
            None,
            JSONRPC_INVALID_REQUEST,
            "Invalid request: expected single JSON-RPC object",
        )
    if loaded.get("jsonrpc") != "2.0":
        return None, format_jsonrpc_error(
            extract_request_id(loaded),
            JSONRPC_INVALID_REQUEST,
            "Invalid request: jsonrpc must be '2.0'",
        )
    return loaded, None


def encode_stdio_line(payload: Mapping[str, Any], *, max_bytes: int) -> bytes:
    """Encode a JSON-RPC message as single-line newline-delimited UTF-8 for ACP stdio."""
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if "\n" in serialized or "\r" in serialized:
        raise AcpError("Encoded ACP stdio message contains embedded newline.")
    encoded = (serialized + "\n").encode("utf-8")
    if len(encoded) > max_bytes:
        raise AcpError(
            f"Outbound ACP message ({len(encoded)} bytes) exceeds max_frame_bytes={max_bytes}."
        )
    return encoded


__all__ = [
    "encode_stdio_line",
    "extract_request_id",
    "format_jsonrpc_error",
    "parse_client_frame",
]
