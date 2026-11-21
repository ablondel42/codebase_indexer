"""Stateless terminal log formatting and secret-redacted JSON indentation helpers."""

import difflib
import json
import logging
import sys
import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
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


def _build_unified_diff_text(old_text: str, new_text: str, raw_path: object) -> str:
    """Compute a compact unified diff string between ``old_text`` and ``new_text``."""
    label = Path(raw_path).name if isinstance(raw_path, str) and raw_path else "file"
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            lineterm="",
        )
    )
    return "\n".join(diff_lines) if diff_lines else "(no textual changes)"


def _collapse_diff_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Replace full-file ``oldText``/``newText`` diff pairs with ``unifiedDiff``."""
    old_text = mapping.get("oldText")
    new_text = mapping.get("newText")
    if mapping.get("type") != "diff" or not (
        isinstance(old_text, str) and isinstance(new_text, str)
    ):
        return {str(k): v for k, v in mapping.items()}

    collapsed = {
        str(key): val
        for key, val in mapping.items()
        if key not in ("oldText", "newText")
    }
    collapsed["unifiedDiff"] = _build_unified_diff_text(
        old_text,
        new_text,
        mapping.get("path"),
    )
    return collapsed


def _format_multiline_str(text: str, depth: int) -> str:
    """Render a multiline string as an indented ``|`` block with real line breaks."""
    child_pad = "  " * (depth + 1)
    lines = text.splitlines() or [""]
    body = "\n".join(f"{child_pad}{line}" if line else "" for line in lines)
    return f"|\n{body}"


def _format_mapping_for_log(mapping: Mapping[str, Any], depth: int) -> str:
    """Render a mapping with 2-space indentation and multiline string blocks."""
    prepared = _collapse_diff_mapping(mapping)
    if not prepared:
        return "{}"
    pad = "  " * depth
    child_pad = "  " * (depth + 1)
    items = list(prepared.items())
    lines: list[str] = ["{"]
    for idx, (key, val) in enumerate(items):
        key_json = json.dumps(str(key), ensure_ascii=False)
        rendered = _format_value_for_log(val, depth + 1)
        is_block = isinstance(val, str) and "\n" in val
        suffix = "" if (idx == len(items) - 1 or is_block) else ","
        lines.append(f"{child_pad}{key_json}: {rendered}{suffix}")
    lines.append(f"{pad}}}")
    return "\n".join(lines)


def _format_sequence_for_log(items: Sequence[Any], depth: int) -> str:
    """Render a list or tuple with 2-space indentation and multiline string blocks."""
    if not items:
        return "[]"
    pad = "  " * depth
    child_pad = "  " * (depth + 1)
    lines: list[str] = ["["]
    for idx, val in enumerate(items):
        rendered = _format_value_for_log(val, depth + 1)
        is_block = isinstance(val, str) and "\n" in val
        suffix = "" if (idx == len(items) - 1 or is_block) else ","
        lines.append(f"{child_pad}{rendered}{suffix}")
    lines.append(f"{pad}]")
    return "\n".join(lines)


def _format_value_for_log(value: Any, depth: int) -> str:
    """Dispatch ``value`` to scalar JSON, multiline block, list, or dict formatting."""
    if isinstance(value, Mapping):
        return _format_mapping_for_log(value, depth)
    if isinstance(value, (list, tuple)):
        return _format_sequence_for_log(value, depth)
    if isinstance(value, str) and "\n" in value:
        return _format_multiline_str(value, depth)
    return json.dumps(value, ensure_ascii=False)


def indent_json_payload(payload: Any, *, indent_spaces: int = 2) -> str:
    """Pretty-print ``payload`` with redacted secrets, multiline blocks, and diffs."""
    redacted = redact_for_log(payload)
    pretty = _format_value_for_log(redacted, 0)
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
