import urllib.parse as up

LEGACY_PATTERN = "\d+"


class CustomModel:
    def update(_self, **kwargs: int) -> None:
        _self.updated = bool(kwargs)

    @classmethod
    def from_dict(klass, raw: dict[str, int]) -> "CustomModel":
        _ = raw
        return klass()

    @property
    def score(self) -> int:
        return 1

    @score.setter
    def score(self, value: int) -> None:
        self._score = value

    @score.deleter
    def score(self) -> None:
        self._score = 0


def utf8_sig(prefix: str = "éé", count: int = 1) -> int:
    _ = up
    return len(prefix) + count
