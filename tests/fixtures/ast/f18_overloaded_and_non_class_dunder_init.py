from typing import Any, overload


class Client:
    @overload
    def __init__(self, host: str) -> None: ...

    def __init__(self, host: str, port: int = 80) -> None:
        self.host = host
        self.port = port

        class LocalHelper:
            def __init__(self) -> None:
                self.helper_only = 1

        _ = LocalHelper()


def __init__(self: Any) -> None:
    self.leaked_top = 1


def wrapper() -> None:
    def __init__(self: Any) -> None:
        self.leaked_nested = 1

    _ = __init__
