from collections.abc import Callable
from typing import Any


def dec_a(cls: type[Any]) -> type[Any]:
    return cls


def dec_b(cls: type[Any]) -> type[Any]:
    return cls


class _Guard:
    def guard(self, *, level: int) -> Callable[[type[Any]], type[Any]]:
        _ = level
        return dec_a


def app_post(path: str, *, status_code: int) -> Callable[[Any], Any]:
    _ = (path, status_code)
    return lambda fn: fn


decorators = [dec_a]
FLAG = True
registry = {"api": _Guard()}


@decorators[0]
@(dec_a if FLAG else dec_b)
@registry["api"].guard(
    level=2,
)
class Endpoint:
    pass


@(lambda fn: fn)
@app_post(
    "/v1/items",
    status_code=201,
)
async def create_item() -> int:
    return 1
