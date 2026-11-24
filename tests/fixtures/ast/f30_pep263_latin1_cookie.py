# -*- coding: latin-1 -*-
import decimal

CAFE_PRICE = "café £1"


def résumé() -> str:
    return f"{CAFE_PRICE}:{decimal.Decimal(1)}"
