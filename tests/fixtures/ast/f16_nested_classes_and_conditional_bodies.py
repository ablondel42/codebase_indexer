FAST = True


class Outer:
    class Meta:
        db_table = "t"

        class Rules:
            strict: bool = True

            def __init__(self) -> None:
                self.active = True

            @property
            def ok(self) -> bool:
                return self.active

    if FAST:
        def __init__(self) -> None:
            self.fast = True

        def run(self) -> int:
            return 1
    else:
        def __init__(self) -> None:
            self.slow = True

        def run(self) -> int:
            return 2
