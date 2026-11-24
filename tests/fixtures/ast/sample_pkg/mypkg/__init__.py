from typing import TYPE_CHECKING

from .worker import run_worker

if TYPE_CHECKING:
    from mypkg import Exported

_ = run_worker


class Exported:
    value: int = 1
