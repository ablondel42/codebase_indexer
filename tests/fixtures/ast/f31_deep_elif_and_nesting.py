def dispatch_code(code: int) -> int:
    if code == 0:
        return 0
    elif code == 1:
        return 1
    elif code == 2:
        return 2
    elif code == 3:
        return 3
    elif code == 4:
        return 4
    elif code == 5:
        return 5
    elif code == 6:
        return 6
    elif code == 7:
        return 7
    elif code == 8:
        return 8
    elif code == 9:
        return 9
    elif code == 10:
        return 10
    elif code == 11:
        return 11
    elif code == 12:
        return 12
    elif code == 13:
        return 13
    elif code == 14:
        return 14
    else:
        import fractions
        return int(fractions.Fraction(code, 1))


# Post-chain symbol outside function scope
AFTER_CHAIN = True
