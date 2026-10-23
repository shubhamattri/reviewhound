def clamp(value, low, high):
    """Limit value to the closed range [low, high]."""
    return max(low, min(value, high))
