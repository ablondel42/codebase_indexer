from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T", bound=type)


def first(cls: T) -> T:
    return cls


def second(*, flag: bool) -> Callable[[T], T]:
    def wrap(cls: T) -> T:
        _ = flag
        return cls

    return wrap


@first
@second(flag=True)
class Service:
    @classmethod
    def make(cls) -> int:
        return 1

    @staticmethod
    async def check(x: int) -> int:
        return x
