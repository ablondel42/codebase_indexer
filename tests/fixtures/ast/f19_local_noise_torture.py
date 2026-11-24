from collections.abc import AsyncIterable
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

_ = 1
_, KEEP = 1, 2
GLOBAL_VAR = 0


def torture(items: list[int], other: Any) -> int:
    global GLOBAL_VAR
    GLOBAL_VAR = 1
    nonlocal_var = 0

    def touch() -> None:
        nonlocal nonlocal_var
        nonlocal_var += 1

    local_a = 1
    local_b: int = 2
    local_a += local_b
    head, *tail = items
    _ = (head, tail, touch)
    other.attr = 3
    walrus = 0
    squares = [sq * 2 for sq in items if (walrus := sq) > 0]
    unique = {u for u in squares}
    mapped = {k: v for k, v in enumerate(unique)}
    gen = sum(g for g in mapped)
    for loop_i in items:
        _ = loop_i
    with nullcontext(1) as ctx_res:
        _ = ctx_res
    try:
        pass
    except Exception as exc_var:
        _ = exc_var
    match items:
        case [m_first, *m_rest]:
            _ = (m_first, m_rest)
    fn = lambda lam_arg: lam_arg + gen
    return int(fn(walrus))


async def async_torture(
    aiter: AsyncIterable[int], actx: AbstractAsyncContextManager[int]
) -> None:
    async for a_item in aiter:
        _ = a_item
    async with actx as a_ctx:
        _ = a_ctx
