import { describe, expect, it, vi } from "vitest";
import { subscribeRunEvents } from "./subscribe";

class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = 1;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    super();
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  emit(type: string, id: string, data: string) {
    this.dispatchEvent(new MessageEvent(type, { data, lastEventId: id }));
  }
}

function setup() {
  const source = new FakeEventSource("");
  const factory = vi.fn((url: string) => {
    Object.assign(source, { url });
    return source as unknown as EventSource;
  });
  const handlers = {
    onEvent: vi.fn(),
    onInvalidEvent: vi.fn(),
    onReconnecting: vi.fn(),
    onClosed: vi.fn(),
  };
  const unsubscribe = subscribeRunEvents("run-1", handlers, factory);
  return { source, factory, handlers, unsubscribe };
}

describe("subscribeRunEvents", () => {
  it("run 이벤트 URL을 구독하고 같은 seq는 한 번만 전달한다", () => {
    const { source, factory, handlers } = setup();
    expect(factory).toHaveBeenCalledWith("/api/runs/run-1/events");
    source.emit("status", "1", '{"status":"running"}');
    source.emit("status", "1", '{"status":"running"}');
    expect(handlers.onEvent).toHaveBeenCalledTimes(1);
  });

  it("종료 상태 이벤트를 받으면 연결을 닫는다", () => {
    const { source, handlers } = setup();
    source.emit("status", "1", '{"status":"running"}');
    expect(source.closed).toBe(false);
    source.emit("status", "2", '{"status":"succeeded"}');
    expect(source.closed).toBe(true);
    expect(handlers.onEvent).toHaveBeenCalledTimes(2);
  });

  it("형태가 다른 이벤트는 onInvalidEvent로, 모르는 종류는 조용히 넘긴다", () => {
    const { source, handlers } = setup();
    source.emit("status", "1", '{"status":"exploded"}');
    expect(handlers.onInvalidEvent).toHaveBeenCalledTimes(1);
    expect(handlers.onEvent).not.toHaveBeenCalled();
  });

  it("끊기면 재접속 중으로, 브라우저가 포기하면 닫힘으로 알린다", () => {
    const { source, handlers } = setup();
    source.readyState = 0;
    source.onerror?.();
    expect(handlers.onReconnecting).toHaveBeenCalledTimes(1);
    source.readyState = 2;
    source.onerror?.();
    expect(handlers.onClosed).toHaveBeenCalledTimes(1);
  });

  it("구독 해제하면 연결을 닫고 이후 이벤트를 무시한다", () => {
    const { source, handlers, unsubscribe } = setup();
    unsubscribe();
    expect(source.closed).toBe(true);
    source.emit("status", "1", '{"status":"running"}');
    expect(handlers.onEvent).not.toHaveBeenCalled();
  });
});
