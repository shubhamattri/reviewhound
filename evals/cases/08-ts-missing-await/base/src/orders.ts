import { saveOrder } from "./db";

export async function placeOrder(order: { id: string }): Promise<string> {
  return "pending";
}
