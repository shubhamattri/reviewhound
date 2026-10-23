export class Formatter {
  constructor(width) {
    this.width = width;
  }

  line(text) {
    return text.slice(0, this.width);
  }
}

export function pad(text, n) {
  return text.padEnd(n, " ");
}
