"""Source position, zero-based range, and 1-based line span models."""

from pydantic import Field

from core.models.base import FrozenModel


class Position(FrozenModel):
    """Zero-based character position inside a text document."""

    line: int = Field(..., ge=0)
    character: int = Field(..., ge=0)


class Range(FrozenModel):
    """Zero-based half-open text range ``[start, end)`` inside a document."""

    start: Position
    end: Position


class LineSpan(FrozenModel):
    """One-based inclusive line span ``[start_line, end_line]`` inside a file."""

    start_line: int = Field(..., ge=1)
    end_line: int = Field(..., ge=1)
