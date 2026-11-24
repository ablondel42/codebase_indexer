from collections.abc import Callable
from typing import Any


def dec_a(fn: Callable[..., Any]) -> Callable[..., Any]:
    return fn


def dec_b(*, flag: bool) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    _ = flag
    return lambda fn: fn


@dec_a
def alpha(x: int) -> int:
    """Doc A."""
    # comment
    return x + 1


@dec_b(flag=True)
def beta(
    x: float,
) -> float:
    return (
        x
        + 1
    )


async def gamma(x: int) -> int:
    return x + 1


def delta(y: int) -> int:
    return y + 1


def eps(x: int) -> int:
    return x - 1


def zeta() -> str:
    return "doc"


def eta(x: int) -> int:
    f"{x}"
    return x + 1


def theta(x: int) -> int:
    b"raw"
    return x + 1


def iota(x: int) -> int:
    """Doc."""
    "second"
    return x + 1
