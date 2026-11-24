from contextlib import nullcontext
from typing import Any


class Complex:
    def __init__(self, items: list[int]) -> None:
        try:
            with nullcontext():
                match len(items):
                    case 0:
                        self.empty = True
                    case _:
                        for _i in items:
                            self.nonempty = True
        except RuntimeError:
            self.failed = True

        def inner(self_arg: Any) -> None:
            self_arg.fake_nested = 1

        _ = inner


def factory() -> object:
    class Inner:
        cls_field = 1

        def __init__(self) -> None:
            self.inst_field = 2

        def ping(self) -> int:
            return self.inst_field

    return Inner()
