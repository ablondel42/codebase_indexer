type Vector[T] = list[T]
type Matrix[T] = (
    list[list[T]]
)
type _ = int


class Holder[T]:
    value: T
    type Nested = set[T]


def build[T](val: T) -> list[T]:
    type Local = list[T]
    out: Local = [val]
    return out
