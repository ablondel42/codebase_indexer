"""Mock Python module covering all symbol kinds, constructs, and noise guards for cbi.ast."""

import asyncio
import pathlib as pl
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Any, Final, Self, TypeAlias, overload

from pydantic import BaseModel, Field, computed_field

if TYPE_CHECKING:
    from sqlite3 import Connection

__all__ = [
    "BatchResult",
    "DEFAULT_RETRIES",
    "Payload",
    "QuotaExceededError",
    "SERVICE_NAME",
    "TaskRecord",
    "TaskStatus",
    "TaskSummary",
    "TaskWorker",
    "active_workers",
    "build_worker",
    "default_timeout",
    "max_batch_size",
    "run_batch",
]

# --- 1. Type Aliases (PEP 613 TypeAlias & PEP 695 `type` statement) ---
Payload: TypeAlias = dict[str, Any]
type BatchResult = list[Payload]

# --- 2. Module Constants (UPPER_CASE & Final) and Module Variables ---
DEFAULT_RETRIES = 3
SERVICE_NAME: str = "cbi-mock"
ROUTES: dict[str, Callable[..., Any]] = {}
max_batch_size: Final[int] = 100

active_workers = 0
default_timeout: float = 5.0
_ = "ignored_throwaway"


# --- 3. Decorator Factory & Nested Function ---
def route(path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        ROUTES[path] = fn
        return fn

    return register


# --- 4. Stub Exception, Enum, Dataclass (__post_init__), and Pydantic Model ---
class QuotaExceededError(RuntimeError):
    """Raised when a task or batch exceeds allowed limits."""


class TaskStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"


@dataclass(slots=True)
class TaskRecord:
    task_id: int
    title: str
    tags: list[str] = field(default_factory=list)
    slug: str = field(init=False)

    def __post_init__(self) -> None:
        self.slug = f"{self.task_id}-{self.title.strip().lower()}"


class TaskSummary(BaseModel):
    task_id: int
    title: str
    status: TaskStatus = TaskStatus.DONE
    tag_count: int = Field(default=0, ge=0)

    @computed_field
    @property
    def label(self) -> str:
        return f"{self.task_id}:{self.title}"


# --- 5. Base Class, Subclass, Nested Class, Properties, Sync & Async Methods ---
class BaseWorker:
    kind = "base"

    def reset(self) -> None: ...

    def describe(self) -> str:
        return f"{self.kind}:{SERVICE_NAME}"


class TaskWorker(BaseWorker):
    type WorkerTag = str
    kind: WorkerTag = "task"

    class Config:
        strict: bool = True

    def __init__(
        self,
        root: pl.Path,
        max_retries: int = DEFAULT_RETRIES,
        conn: "Connection | None" = None,
    ) -> None:
        self.root: pl.Path = root
        self.max_retries: int = max_retries
        self._processed: int = 0
        self._ready: bool = conn is None or self.max_retries > 0

    @classmethod
    def from_cwd(cls) -> Self:
        return cls(root=pl.Path.cwd())

    @staticmethod
    def normalize_title(raw: str, /) -> str:
        return raw.strip().lower()

    @property
    def is_ready(self) -> bool:
        return self._ready and self.max_retries > 0 and self.root.exists()

    @is_ready.setter
    def is_ready(self, value: bool) -> None:
        self._ready = value

    @cached_property
    def worker_id(self) -> str:
        return f"{self.kind}:{self.root.name}"

    def process(self, record: TaskRecord, *, strict: bool = True) -> Payload:
        # Local noise (must NOT be indexed as symbols):
        # local Assign, list comprehension var, and walrus NamedExpr.
        cleaned_title = self.normalize_title(record.title)
        clean_tags = [tag.strip() for tag in record.tags if tag.strip()]
        if (tag_count := len(clean_tags)) > max_batch_size and strict:
            raise QuotaExceededError(f"Too many tags: {tag_count}")

        self._processed += 1
        summary = TaskSummary(
            task_id=record.task_id,
            title=cleaned_title,
            tag_count=tag_count,
        )
        return {
            **summary.model_dump(),
            "tags": clean_tags,
            "service": SERVICE_NAME,
            "timeout": default_timeout,
        }

    async def process_async(
        self, record: TaskRecord, *extra_tags: str, **meta: Any
    ) -> Payload:
        await asyncio.sleep(0)
        base = self.process(record)
        if extra_tags:
            return {**base, "extra": list(extra_tags), **meta}
        return {**base, **meta}


# --- 6. Overloaded Sync Function, Decorated Async Entry Point & Scoped Imports ---
@overload
def build_worker(raw_dir: str = ".") -> TaskWorker: ...


@overload
def build_worker(raw_dir: pl.Path) -> TaskWorker: ...


def build_worker(raw_dir: str | pl.Path = ".") -> TaskWorker:
    import uuid

    _ = uuid.uuid4().hex
    resolved = pl.Path(raw_dir).resolve()
    return TaskWorker(root=resolved, max_retries=DEFAULT_RETRIES)


@route("/tasks/batch")
async def run_batch(records: list[TaskRecord]) -> BatchResult:
    worker = build_worker()

    def format_error(err: QuotaExceededError) -> Payload:
        from decimal import Decimal

        return {"error": str(err), "worker": worker.describe(), "code": str(Decimal("1"))}

    results: BatchResult = []
    for rec in records:
        try:
            results.append(await worker.process_async(rec))
        except QuotaExceededError as exc:
            results.append(format_error(exc))
    return results


if __name__ == "__main__":
    import pprint

    MAIN_ONLY_DEBUG = True
    pprint.pp((SERVICE_NAME, active_workers, MAIN_ONLY_DEBUG))
