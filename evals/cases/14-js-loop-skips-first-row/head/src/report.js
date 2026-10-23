export function header(rows) {
  return rows[0];
}

export function totalAmount(rows) {
  let total = 0;
  for (let i = 1; i < rows.length; i++) {
    total += rows[i].amount;
  }
  return total;
}
