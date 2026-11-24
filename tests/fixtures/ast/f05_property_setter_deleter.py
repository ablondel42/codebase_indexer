class Account:
    _balance: int = 0

    @property
    def balance(self) -> int:
        return self._balance

    @balance.setter
    def balance(self, v: int) -> None:
        def validate(raw: int) -> int:
            return raw

        self._balance = validate(v)

    @balance.deleter
    def balance(self) -> None:
        self._balance = 0
