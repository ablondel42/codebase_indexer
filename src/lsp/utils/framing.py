"""Pure JSON-RPC 2.0 ``Content-Length`` wire framing helpers for LSP stdio streams."""

import json
from collections.abc import Mapping
from typing import Any

from core.models.exceptions import LspError
from core.utils.logging import get_logger

logger = get_logger("lsp.utils.framing")


def encode_jsonrpc_message(payload: Mapping[str, Any], max_payload_bytes: int) -> bytes:
    """Encode a JSON-RPC payload into an ASCII ``Content-Length`` framed byte buffer.

    Args:
        payload: JSON-RPC message mapping to serialize.
        max_payload_bytes: Maximum permitted UTF-8 body size in bytes.

    Returns:
        Complete ``Content-Length: <n>\\r\\n\\r\\n<body>`` byte sequence.

    Raises:
        LspError: If payload serialization fails or exceeds ``max_payload_bytes``.
    """
    try:
        body = json.dumps(dict(payload)).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.debug("Failed serializing outbound JSON-RPC payload: %s", exc)
        raise LspError(f"Failed to serialize JSON-RPC payload: {exc}") from exc

    body_len = len(body)
    if body_len > max_payload_bytes:
        raise LspError(
            f"Outbound LSP payload ({body_len} bytes) exceeds "
            f"max_lsp_payload_bytes ({max_payload_bytes}).",
            context={"content_length": body_len, "limit": max_payload_bytes},
        )

    header = f"Content-Length: {body_len}\r\n\r\n".encode("ascii")
    return header + body


def parse_header_line(stripped: bytes) -> tuple[str, str]:
    """Decode and split one stripped ASCII ``Key: Value`` LSP header line.

    Args:
        stripped: Raw header line bytes with trailing ``\\r\\n`` already stripped.

    Returns:
        Tuple of ``(lowercased_header_name, stripped_header_value)``.

    Raises:
        LspError: If ``stripped`` contains non-ASCII bytes or lacks a ``':'`` separator.
    """
    try:
        decoded = stripped.decode("ascii")
    except UnicodeDecodeError as exc:
        logger.debug("Rejected non-ASCII LSP header line: %s", exc)
        raise LspError(f"Non-ASCII LSP header line: {exc}") from exc

    if ":" not in decoded:
        raise LspError(f"Malformed LSP header line: {decoded!r}")

    key, value = decoded.split(":", 1)
    return key.strip().lower(), value.strip()


def parse_content_length_header(headers: Mapping[str, str], max_payload_bytes: int) -> int:
    """Extract and validate a positive bounded ``Content-Length`` from parsed LSP headers.

    Args:
        headers: Mapping of lowercased header names to string values.
        max_payload_bytes: Maximum allowed payload length in bytes.

    Returns:
        Validated positive ``Content-Length`` integer.

    Raises:
        LspError: If ``content-length`` is missing, non-integer, ``<= 0``, or exceeds
            ``max_payload_bytes``.
    """
    raw_length = headers.get("content-length")
    if raw_length is None:
        raise LspError("Missing Content-Length header in LSP message.")

    try:
        content_length = int(raw_length)
    except ValueError as exc:
        logger.debug("Rejected non-integer LSP Content-Length %r: %s", raw_length, exc)
        raise LspError(f"Invalid Content-Length header: {raw_length!r}") from exc

    if content_length <= 0:
        raise LspError(f"Invalid non-positive Content-Length: {content_length}")

    if content_length > max_payload_bytes:
        raise LspError(
            f"LSP Content-Length {content_length} exceeds "
            f"max_lsp_payload_bytes ({max_payload_bytes}).",
            context={"content_length": content_length, "limit": max_payload_bytes},
        )

    return content_length
