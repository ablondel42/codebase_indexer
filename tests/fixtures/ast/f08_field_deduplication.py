class Item:
    x: int

    def __init__(self, flag: bool) -> None:
        self.x = 1
        if flag:
            self.y = 2
        else:
            self.y = 3
