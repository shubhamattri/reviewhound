export async function saveOrder(order: { id: string }): Promise<void> {
  if (!order.id) {
    throw new Error("missing id");
  }
}
