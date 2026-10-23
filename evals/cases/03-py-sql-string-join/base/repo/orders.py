def find_order(db, order_id):
    return db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
