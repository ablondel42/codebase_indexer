from types import SimpleNamespace

HEAD, (mid, *TAIL), _ = (1, (2, 3, 4), 5)
(
    FIRST,
    second,
) = (
    10,
    20,
)
ALPHA = beta = 42
A = 1; b = 2; import math


class Inline:
    x = 1; y = 2


items: list[int] = [0]
items[0] = 1
state = SimpleNamespace(flag=False)
state.flag = True
_ = math.pi
