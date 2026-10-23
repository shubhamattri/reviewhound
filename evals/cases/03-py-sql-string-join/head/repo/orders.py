def find_order(db, order_id):
    return db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def find_by_customer(db, name):
    return db.execute(f"SELECT * FROM orders WHERE customer = '{name}'").fetchall()
