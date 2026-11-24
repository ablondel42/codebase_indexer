from types import SimpleNamespace
from typing import Any


class Record:
    MAX_ITEMS = 10
    shared: str
    nested: Any
    tag_a, tag_b = "a", "b"

    def __setitem__(self, idx: int, val: int) -> None:
        _ = (idx, val)

    def __init__(self, other: Any) -> None:
        self.shared = "override"
        self.bare_typed: int
        self.nested = SimpleNamespace()
        self.first, (self.mid, *self.rest), _ = (1, (2, 3, 4), 5)
        [self.alpha, *self.beta] = [1, 2, 3]
        self.p = self.q = 0
        other.not_ours = 1
        self.nested.not_ours = 2
        self.p += 1
        self[0] = 3

    def __post_init__(self) -> None:
        self.ready = True

    def helper(self) -> None:
        self.late_attr = 99

    async def ahelper(self) -> None:
        self.async_attr: str = "ok"

    @property
    def lazy_prop(self) -> int:
        self._cached_val = 1
        return self._cached_val

    @staticmethod
    def stat_guard(self: Any) -> None:
        self.static_ignored = 1

    @classmethod
    def cls_guard(cls) -> None:
        _ = cls
        self = SimpleNamespace()
        self.cls_ignored = 1
