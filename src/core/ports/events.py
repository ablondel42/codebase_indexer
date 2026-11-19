"""Typed domain event callback contract for headless progress and watch notifications."""

from collections.abc import Callable
from typing import TypeAlias

from core.models.base import FrozenModel

EventCallback: TypeAlias = Callable[[FrozenModel], None]
