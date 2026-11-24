import typing
from typing import overload


@overload
def parse(x: str) -> int: ...


@typing.overload
def parse(x: bytes) -> int: ...


def parse(x: str | bytes) -> int:
    return len(x)


@overload
async def aparse(x: str) -> int: ...


async def aparse(x: str | bytes) -> int:
    return len(x)


class Codec:
    @overload
    def encode(self, x: str) -> bytes: ...

    def encode(self, x: str | bytes) -> bytes:
        return x.encode("utf-8") if isinstance(x, str) else x

    @classmethod
    @overload
    def decode(cls, x: str) -> int: ...

    @classmethod
    def decode(cls, x: str | bytes) -> int:
        return len(x)

    @overload
    @property
    def prop(self) -> int: ...

    @property
    def prop(self) -> int:
        return 2
