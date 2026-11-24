import pathlib

from deep_ns.sub_pkg.leaf import compute_leaf

from .. import standalone


def run_worker() -> str:
    return f"{pathlib.Path(standalone.LABEL)}:{compute_leaf()}"
