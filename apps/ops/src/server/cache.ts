export class TimedCache<T> {
  private value: T | null = null;
  private expiresAt = 0;

  constructor(private readonly ttlMs: number) {}

  get(now = Date.now()): T | null {
    return this.value !== null && now < this.expiresAt ? this.value : null;
  }

  set(value: T, now = Date.now()): T {
    this.value = value;
    this.expiresAt = now + this.ttlMs;
    return value;
  }

  clear() {
    this.value = null;
    this.expiresAt = 0;
  }
}
