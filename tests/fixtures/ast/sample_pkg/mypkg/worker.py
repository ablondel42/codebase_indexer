import pathlib

from .. import standalone


def run_worker() -> pathlib.Path:
    return pathlib.Path(standalone.LABEL)
