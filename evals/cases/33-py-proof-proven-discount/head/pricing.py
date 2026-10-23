def apply_discount(price_cents, percent):
    """Price after a percentage discount, in whole cents."""
    return price_cents - price_cents * percent // 10
