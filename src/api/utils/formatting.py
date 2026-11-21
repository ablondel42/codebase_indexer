"""Stateless terminal log formatting and secret-redacted JSON indentation helpers."""

import json
import logging
import sys
import textwrap
from collections.abc import Mapping
from typing import Any

from api.utils.security import FORBIDDEN_META_KEYS
from core.utils.logging import ROOT_LOGGER_NAME, get_logger

logger = get_logger("api.utils.formatting")

API_CONSOLE_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
API_CONSOLE_DATEFMT: str = "%H:%M:%S"


def _redact_named_pairs(items: list[Any] | tuple[Any, ...]) -> list[Any]:
    """Redact ``value`` fields in MCP ``env`` or ``headers`` ``{name, value}`` lists."""
    redacted: list[Any] = []
    for entry in items:
        if isinstance(entry, Mapping) and "name" in entry and "value" in entry:
            copied = {str(k): redact_for_log(v) for k, v in entry.items()}
            copied["value"] = "[REDACTED]"
            redacted.append(copied)
        else:
            redacted.append(redact_for_log(entry))
    return redacted


def _redact_mapping_entry(key: object, val: Any) -> Any:
    """Redact a single mapping value based on credential and MCP env/header rules."""
    if isinstance(key, str):
        lowered = key.lower()
        if lowered in FORBIDDEN_META_KEYS:
            return "[REDACTED]"
        if lowered in ("env", "headers") and isinstance(val, (list, tuple)):
            return _redact_named_pairs(val)
    return redact_for_log(val)


def redact_for_log(value: Any) -> Any:
    """Recursively replace sensitive credential/tracing keys with ``'[REDACTED]'``."""
    if isinstance(value, Mapping):
        return {str(k): _redact_mapping_entry(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_for_log(item) for item in value]
    if isinstance(value, tuple):
        return [redact_for_log(item) for item in value]
    return value


def describe_jsonrpc_frame(payload: Mapping[str, Any]) -> str:
    """Return a concise human-readable label for a JSON-RPC 2.0 message dict."""
    method = payload.get("method")
    req_id = payload.get("id")
    if isinstance(method, str) and method:
        if "id" in payload and req_id is not None:
            return f"request #{req_id} · {method}"
        return f"notification · {method}"
    if "error" in payload:
        err_obj = payload.get("error")
        code = err_obj.get("code") if isinstance(err_obj, Mapping) else "unknown"
        return f"error #{req_id} · code={code}"
    if "result" in payload:
        return f"response #{req_id}"
    return "frame"


def indent_json_payload(payload: Any, *, indent_spaces: int = 2) -> str:
    """Pretty-print ``payload`` as 2-space JSON with secrets redacted and block-indented."""
    pretty = json.dumps(redact_for_log(payload), indent=2, ensure_ascii=False)
    return textwrap.indent(pretty, " " * indent_spaces)


def format_raw_frame_for_log(
    raw_text: str,
    *,
    indent_spaces: int = 2,
) -> tuple[str, str]:
    """Parse ``raw_text`` and return ``(summary_label, indented_pretty_block)``."""
    prefix = " " * indent_spaces
    try:
        loaded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.debug("Formatting non-JSON frame as raw text: %s", exc)
        return "malformed JSON", textwrap.indent(raw_text.strip(), prefix)

    if isinstance(loaded, Mapping):
        return (
            describe_jsonrpc_frame(loaded),
            indent_json_payload(loaded, indent_spaces=indent_spaces),
        )
    return "non-object JSON", indent_json_payload(loaded, indent_spaces=indent_spaces)


def format_kv_block(fields: Mapping[str, Any], *, indent_spaces: int = 2) -> str:
    """Render an aligned multi-line ``key: value`` block indented for terminal logs."""
    if not fields:
        return ""
    pad = " " * indent_spaces
    width = max(len(str(key)) for key in fields) + 1
    lines = [f"{pad}{f'{key}:':<{width}} {value}" for key, value in fields.items()]
    return "\n".join(lines)


def configure_api_terminal_logging(*, debug: bool = False) -> None:
    """Ensure ``indexer`` logs emit cleanly to ``stderr`` during ``cbi api`` runs."""
    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.NullHandler):
            root_logger.removeHandler(handler)

    formatter = logging.Formatter(API_CONSOLE_FORMAT, datefmt=API_CONSOLE_DATEFMT)
    has_stream_handler = False
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler,
            logging.FileHandler,
        ):
            handler.setFormatter(formatter)
            has_stream_handler = True

    if not has_stream_handler:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG)
        console.setFormatter(formatter)
        root_logger.addHandler(console)

    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    root_logger.propagate = False


__all__ = [
    "API_CONSOLE_DATEFMT",
    "API_CONSOLE_FORMAT",
    "configure_api_terminal_logging",
    "describe_jsonrpc_frame",
    "format_kv_block",
    "format_raw_frame_for_log",
    "indent_json_payload",
    "redact_for_log",
]
