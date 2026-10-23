export function tax(amount: number): number {
  return amount * 0.2;
}

export class Invoice {
  constructor(private lines: number[]) {}

  subtotal(): number {
    return this.lines.reduce((a, b) => a + b, 0);
  }

  count(): number {
    return this.lines.length;
  }
}
