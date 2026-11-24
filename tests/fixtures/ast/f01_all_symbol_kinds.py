import pathlib
from dataclasses import dataclass
from typing import TypeAlias

Alias: TypeAlias = int
LIMIT = 5
count = 0


@dataclass
class Box:
    tag: str

    @property
    def size(self) -> int:
        return 1

    async def fetch(self) -> int:
        return 2

    def ping(self) -> None: ...


def helper(step: int = 1) -> int:
    return int(pathlib.PurePosixPath("a").name == "a") + LIMIT + count + step


async def run() -> int:
    return 4
