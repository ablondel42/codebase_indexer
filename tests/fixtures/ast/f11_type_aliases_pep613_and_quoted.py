from __future__ import annotations

import typing
from typing import TypeAlias

Alpha: TypeAlias = list[int]
Pair: typing.TypeAlias = tuple[int, int]
Beta: "TypeAlias" = dict[str, str]
Gamma: "typing.TypeAlias" = tuple[int, ...]


class Box:
    not_alias: TypeAlias
    bare_quoted: "TypeAlias"
    Delta: "TypeAlias" = set[str]


def make_list() -> list[int]:
    LocalAlias: TypeAlias = int
    val: LocalAlias = 1
    return [val]
