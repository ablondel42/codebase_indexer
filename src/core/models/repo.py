"""Repository entity model."""

from pydantic import Field, field_validator

from core.models.base import FrozenModel
from core.models.enums import Language
from core.utils.timestamps import validate_iso_timestamp


class RepoRecord(FrozenModel):
    """Canonical record representing an indexed repository on disk."""

    id: str = Field(..., min_length=1, pattern=r"^repo:.+")
    root_path: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    default_language: Language = Field(default=Language.PYTHON)
    created_at: str
    last_indexed_at: str | None = None

    @field_validator("created_at", "last_indexed_at")
    @classmethod
    def _validate_timestamps(cls, value: str | None) -> str | None:
        """Validate repository timestamps are ISO 8601 strings.

        Args:
            value: Candidate timestamp string or ``None``.

        Returns:
            Validated timestamp string or ``None``.

        Raises:
            ValueError: If ``value`` is not a valid ISO 8601 string.
        """
        return validate_iso_timestamp(value)
