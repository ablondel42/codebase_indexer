def full_sig(
    po_bare,
    /,
    bare,
    typed: str,
    opt: str = "x",
    _=0,
    *args: int,
    kw_req: bool,
    kw_opt: list[int] = [
        1,
        2,
    ],
    **kwargs: object,
) -> tuple[object, ...]:
    return (po_bare, bare, typed, opt, _, args, kw_req, kw_opt, kwargs)


def posonly_default(po_req: int, po_def: int = 1, /) -> int:
    return po_req + po_def


class ReceiverDemo:
    def inst_m(self, a: int) -> int:
        return a

    def pos_self(self, /, val: int = 2) -> int:
        return val

    @classmethod
    def cls_m(cls, token: str) -> str:
        return token

    @staticmethod
    def stat_m(self: int, cls: str) -> tuple[int, str]:
        return (self, cls)


class Meta(type):
    def __new__(mcs, name: str) -> type:
        return type(name, (), {})


def top_fn(self: int, cls: str) -> tuple[int, str]:
    return (self, cls)


def bare_only(bare) -> object:
    return bare


def clone_a(first: int = 10, *, flag: bool = False) -> None:
    _ = (first, flag)


def clone_b(second: int = 10, *, active: bool = False) -> None:
    _ = (second, active)


def diff_forms(
    a: int = 1,
    /,
    b: int = 1,
    *,
    c: int = 1,
    d: int = 2,
    e: str = "1",
) -> None:
    _ = (a, b, c, d, e)
