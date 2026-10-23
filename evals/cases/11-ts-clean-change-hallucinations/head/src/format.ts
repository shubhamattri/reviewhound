export function money(cents: number): string {
  return (cents / 100).toFixed(2);
}

export function percent(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}
