from contextlib import nullcontext

COND = True
MODE = "a"

if COND:
    def from_if() -> int:
        return 1
else:
    def from_else() -> int:
        return 2

try:
    A = 1
except* ValueError as err:
    _ = err
    B = 2
else:
    D = 4
finally:
    C = 3

with nullcontext(1) as handle:
    _ = handle
    WITH_VAL = 5

match MODE:
    case captured:
        _ = captured
        MATCH_VAL = 6

if "__main__" == __name__:
    skipped_main = 1

    def skipped_fn() -> None:
        pass
else:
    KEEP_ELSE = 7

if __name__ != "__main__":
    NOT_MAIN_GUARD = 8
