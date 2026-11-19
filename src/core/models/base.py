"""Shared Pydantic v2 base models enforcing strict extra-field rejection."""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Immutable Pydantic v2 base model rejecting undeclared extra fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class MutableModel(BaseModel):
    """Mutable Pydantic v2 base model rejecting undeclared extra fields."""

    model_config = ConfigDict(frozen=False, extra="forbid")
