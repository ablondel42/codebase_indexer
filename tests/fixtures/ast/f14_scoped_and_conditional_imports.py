from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import html

    _ = html

try:
    import sqlite3

    _ = sqlite3
except ImportError:
    import asyncio

    _ = asyncio


class Box:
    from uuid import UUID


def worker() -> None:
    import decimal

    _ = decimal.Decimal(1)


if __name__ == "__main__":
    import argparse

    _ = argparse.ArgumentParser
