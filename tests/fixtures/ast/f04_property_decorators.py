import functools
from collections.abc import Callable
from functools import cached_property
from typing import Any

import pydantic
from pydantic import BaseModel, computed_field


def trace(fn: Callable[..., Any]) -> Callable[..., Any]:
    return fn


@property
def top_fn(x: object) -> int:
    _ = x
    return 0


class PlainItem:
    @property
    @trace
    def p_plain(self) -> int:
        return 1

    @cached_property
    def p_cached(self) -> int:
        return 2

    @functools.cached_property
    def p_qual_cached(self) -> int:
        return 3

    @cached_property
    async def p_async(self) -> int:
        return 4


class ModelItem(BaseModel):
    @computed_field
    def p_comp(self) -> int:
        return 5

    @computed_field()
    def p_comp_call(self) -> int:
        return 6

    @pydantic.computed_field(return_type=int)
    def p_qual_comp(self) -> int:
        return 7
