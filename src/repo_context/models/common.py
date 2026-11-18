"""Common model types and serialization/validation helpers."""

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class Position:
    """A position in a file (zero-based).

    Attributes:
        line: Zero-based line number.
        character: Zero-based character offset within the line.
    """

    line: int
    character: int


@dataclass(frozen=True)
class Range:
    """A range in a file.

    Attributes:
        start: Starting position (inclusive).
        end: Ending position (exclusive).
    """

    start: Position
    end: Position


def _json_default(value: Any) -> Any:
    """Serialize nested Pydantic models, dataclasses, or Paths for json.dumps."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def to_json(value: Any) -> str:
    """Serialize a value (dict, list, dataclass, or Pydantic BaseModel) to JSON string."""
    if isinstance(value, type):
        if is_dataclass(value) or issubclass(value, BaseModel):
            raise TypeError("to_json expects an instance, not a class")
    if isinstance(value, BaseModel):
        return json.dumps(value.model_dump(mode="json"), sort_keys=True, default=_json_default)
    if is_dataclass(value) and not isinstance(value, type):
        return json.dumps(asdict(value), sort_keys=True, default=_json_default)
    return json.dumps(value, sort_keys=True, default=_json_default)


def from_json(data: str) -> Any:
    """Deserialize a JSON string to a Python object."""
    return json.loads(data)


def validate_iso_timestamp(value: str | None) -> str | None:
    """Validate that a timestamp string is in ISO 8601 format."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp must be ISO 8601 format (e.g., '2026-01-01T00:00:00Z')")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "Timestamp must be ISO 8601 format (e.g., '2026-01-01T00:00:00Z')"
        ) from exc
    return value


def normalize_json_field(value: Any) -> str | None:
    """Normalize a JSON-backed model field from dict/model/str/None to a valid JSON string."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON string: {exc}") from exc
        return value
    return to_json(value)
