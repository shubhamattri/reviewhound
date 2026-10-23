from app.auth import require_role


def cancel_order(request, order_id):
    orders.cancel(order_id)
    return {"ok": True}
