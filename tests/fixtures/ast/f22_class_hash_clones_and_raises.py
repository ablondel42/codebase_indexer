from collections.abc import Callable
from dataclasses import dataclass


class BaseA:
    pass


class BaseB:
    pass


@dataclass
class UserDTO(BaseA):
    """Doc 1."""

    id: int

    def key(self) -> int:
        return self.id


class AccountDTO(BaseB):
    # comment only
    id: int

    def key(self) -> int:
        return self.id


class DiffTypeDTO:
    id: str

    def key(self) -> str:
        return self.id


class DiffNameDTO:
    uid: int

    def key(self) -> int:
        return self.uid


def make_err() -> Callable[[], Exception]:
    return lambda: RuntimeError("x")


def fail_value_error() -> None:
    raise ValueError("bad")


def guarded_not_implemented(flag: bool) -> int:
    if flag:
        raise NotImplementedError
    return 1


def bare_reraise() -> None:
    raise


def dynamic_raise() -> None:
    raise make_err()()
