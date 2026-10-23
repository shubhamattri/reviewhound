def validate_qty(qty):
    return qty > 0


class Cart:
    def __init__(self):
        self.items = []

    def add(self, sku, qty):
        self.items.append((sku, qty))
