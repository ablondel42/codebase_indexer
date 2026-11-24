from math import sin

FLAG = True
x = 1
x = 2


def sin(v: float) -> float:
    return v


if FLAG:
    def job() -> int:
        return 1
else:
    def job() -> int:
        import fractions

        def sub() -> int:
            return int(fractions.Fraction(2, 1))

        return sub()
