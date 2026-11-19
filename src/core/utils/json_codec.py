"""Deterministic JSON serialization and deserialization helpers."""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.models.exceptions import ValidationError
from core.utils.logging import get_logger

logger = get_logger("core.utils.json_codec")


def _json_default(value: Any) -> Any:
    """Serialize nested Pydantic models and ``Path`` objects for ``json.dumps``.

    Args:
        value: Arbitrary nested value encountered by ``json.dumps``.

    Returns:
        JSON-serializable representation of ``value``.

    Raises:
        TypeError: If ``value`` is a class or an unsupported object type.
    """
    if isinstance(value, type):
        raise TypeError(f"to_json expects an instance, got class {value.__name__}")
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def to_json(value: Any) -> str:
    """Serialize a Python value (``BaseModel``, ``Path``, ``dict``, ``list``, primitive) to JSON.

    Args:
        value: Value to serialize with deterministic sorted object keys.

    Returns:
        Compact deterministic JSON string.

    Raises:
        TypeError: If ``value`` or any nested element is a class or non-serializable type.
    """
    if isinstance(value, type):
        raise TypeError(f"to_json expects an instance, not class {value.__name__}")
    if isinstance(value, BaseModel):
        return json.dumps(value.model_dump(mode="json"), sort_keys=True, default=_json_default)
    return json.dumps(value, sort_keys=True, default=_json_default)


def from_json(data: str) -> Any:
    """Deserialize a JSON string into Python primitives, dicts, or lists.

    Args:
        data: Raw JSON string to parse.

    Returns:
        Decoded Python object.

    Raises:
        TypeError: If ``data`` is not a ``str``.
        ValidationError: If ``data`` is not valid JSON.
    """
    if not isinstance(data, str):
        raise TypeError(f"from_json expects a str, got {type(data).__name__}")
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        logger.debug("Failed decoding JSON payload at pos %d: %s", exc.pos, exc.msg)
        raise ValidationError(
            f"Invalid JSON string: {exc}",
            context={"pos": exc.pos, "lineno": exc.lineno, "colno": exc.colno},
        ) from exc
