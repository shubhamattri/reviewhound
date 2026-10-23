import { saveOrder } from "./db";

export async function placeOrder(order: { id: string }): Promise<string> {
  try {
    saveOrder(order);
  } catch (err) {
    return "failed";
  }
  return "saved";
}
