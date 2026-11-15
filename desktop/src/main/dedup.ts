/**
 * 같은 알림 ID는 한 번만 띄운다. 폴링이 같은 알림을 계속 돌려줘도, 창을 다시 로드해 renderer의
 * 기억이 사라져도 main이 중복을 막는다. 메모리가 계속 늘지 않게 오래된 ID부터 잊는다.
 */
export class NotificationDedup {
  private readonly seen = new Set<string>();

  constructor(private readonly capacity = 500) {
    if (!Number.isInteger(capacity) || capacity < 1) throw new RangeError("capacity must be >= 1");
  }

  /** 처음 보는 ID면 true(띄운다), 이미 띄운 ID면 false. */
  shouldShow(id: string): boolean {
    if (this.seen.has(id)) return false;
    this.seen.add(id);
    if (this.seen.size > this.capacity) {
      const oldest = this.seen.values().next();
      if (oldest.done !== true) this.seen.delete(oldest.value);
    }
    return true;
  }
}
