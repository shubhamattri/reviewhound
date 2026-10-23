export class Order {
  constructor(qty, price) {
    this.qty = qty;
    this.price = price;
  }
}

export function tax(amount) {
  return amount * 0.2;
}

export function lineTotals(orders) {
  return orders.map((o) => {
    o.qty * o.price;
  });
}
