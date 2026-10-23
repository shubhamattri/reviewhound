export function label(inv: { status: string }): string {
  if (inv.status = "paid") {
    return "Paid";
  }
  return "Due";
}
