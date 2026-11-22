"""JSON-RPC 2.0 protocol constants, error envelopes, and step-outcome models."""

from typing import Any, Literal

from pydantic import StrictInt

from core.models.base import FrozenModel

JSONRPC_PARSE_ERROR: int = -32700
JSONRPC_INVALID_REQUEST: int = -32600
JSONRPC_METHOD_NOT_FOUND: int = -32601
JSONRPC_INVALID_PARAMS: int = -32602
JSONRPC_INTERNAL_ERROR: int = -32603
ACP_REQUEST_CANCELLED: int = -32800
ACP_AUTH_REQUIRED: int = -32000
ACP_RESOURCE_NOT_FOUND: int = -32002

RESERVED_ERROR_RANGES: tuple[tuple[int, int], ...] = (
    (-32768, -32000),
    (-32899, -32800),
)


class JsonRpcErrorObject(FrozenModel):
    """JSON-RPC 2.0 error payload."""

    code: int
    message: str
    data: Any | None = None


class JsonRpcErrorResponse(FrozenModel):
    """JSON-RPC 2.0 error envelope."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: StrictInt | str | None = None
    error: JsonRpcErrorObject


class JsonRpcSuccessResponse(FrozenModel):
    """JSON-RPC 2.0 success response envelope."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: StrictInt | str
    result: Any


class SessionStepOutcome(FrozenModel):
    """Outbound WebSocket frames and optional close directive from one session step."""

    frames: tuple[str, ...] = ()
    close_code: int | None = None


__all__ = [
    "ACP_AUTH_REQUIRED",
    "ACP_REQUEST_CANCELLED",
    "ACP_RESOURCE_NOT_FOUND",
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "JsonRpcErrorObject",
    "JsonRpcErrorResponse",
    "JsonRpcSuccessResponse",
    "RESERVED_ERROR_RANGES",
    "SessionStepOutcome",
]
