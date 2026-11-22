"""Stateless JSON-RPC 2.0 parsing, classification, and stdio NDJSON framing helpers."""

import json
from collections.abc import Mapping
from typing import Any, Literal

from api.models.jsonrpc import JSONRPC_INVALID_REQUEST, JSONRPC_PARSE_ERROR
from api.utils.errors import format_jsonrpc_error
from core.models.exceptions import AcpTransportError
from core.utils.logging import get_logger

logger = get_logger("api.utils.jsonrpc")

JsonRpcMessageKind = Literal["request", "notification", "response", "invalid"]


def extract_request_id(payload: Mapping[str, Any]) -> int | str | None:
    """Return a valid JSON-RPC ``id`` (``str`` or ``int``, excluding ``bool``), or ``None``."""
    raw_id = payload.get("id")
    if isinstance(raw_id, bool):
        return None
    if isinstance(raw_id, (int, str)):
        return raw_id
    return None


def classify_jsonrpc_message(msg: Mapping[str, Any]) -> JsonRpcMessageKind:
    """Classify a decoded JSON-RPC 2.0 object as request, notification, response, or invalid."""
    has_method = "method" in msg
    has_result = "result" in msg
    has_error = "error" in msg
    kind: JsonRpcMessageKind = "invalid"
    if has_method and not (has_result or has_error):
        method = msg.get("method")
        if isinstance(method, str) and method:
            if "id" not in msg:
                kind = "notification"
            elif extract_request_id(msg) is not None:
                kind = "request"
    elif not has_method and (has_result != has_error) and extract_request_id(msg) is not None:
        kind = "response"
    return kind


def format_jsonrpc_result(req_id: int | str, result: Any) -> str:
    """Serialize a compact JSON-RPC 2.0 success response frame."""
    payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def format_jsonrpc_notification(method: str, params: Mapping[str, Any] | None = None) -> str:
    """Serialize a compact JSON-RPC 2.0 notification frame."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = dict(params)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _validate_decoded_jsonrpc_object(loaded: object) -> str | None:
    """Return formatted JSON-RPC error frame if ``loaded`` violates 2.0 envelope rules."""
    if not isinstance(loaded, dict):
        return format_jsonrpc_error(
            None,
            JSONRPC_INVALID_REQUEST,
            "Invalid request: expected single JSON-RPC object",
        )
    if loaded.get("jsonrpc") != "2.0":
        return format_jsonrpc_error(
            extract_request_id(loaded),
            JSONRPC_INVALID_REQUEST,
            "Invalid request: jsonrpc must be '2.0'",
        )
    if classify_jsonrpc_message(loaded) == "invalid":
        return format_jsonrpc_error(
            extract_request_id(loaded),
            JSONRPC_INVALID_REQUEST,
            "Invalid JSON-RPC 2.0 message structure",
        )
    return None


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

    envelope_err = _validate_decoded_jsonrpc_object(loaded)
    if envelope_err is not None:
        return None, envelope_err
    return loaded, None


def encode_stdio_line(payload: Mapping[str, Any], *, max_bytes: int) -> bytes:
    """Encode a JSON-RPC message as single-line newline-delimited UTF-8 for ACP stdio."""
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if "\n" in serialized or "\r" in serialized:
        raise AcpTransportError("Encoded ACP stdio message contains embedded newline.")
    encoded = (serialized + "\n").encode("utf-8")
    if len(encoded) > max_bytes:
        raise AcpTransportError(
            f"Outbound ACP message ({len(encoded)} bytes) exceeds max_frame_bytes={max_bytes}."
        )
    return encoded


__all__ = [
    "JsonRpcMessageKind",
    "classify_jsonrpc_message",
    "encode_stdio_line",
    "extract_request_id",
    "format_jsonrpc_error",
    "format_jsonrpc_notification",
    "format_jsonrpc_result",
    "parse_client_frame",
]
