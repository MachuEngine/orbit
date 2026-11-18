import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { runSceneActions } from "./sceneActions";
import { findScene, type SceneId } from "./scenes";

const cleanups: (() => void)[] = [];

function start(id: SceneId) {
  const stop = runSceneActions(findScene(id));
  cleanups.push(stop);
  return stop;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  for (const stop of cleanups.splice(0)) stop();
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe("미리보기 장면 동작의 대기 수명", () => {
  it("아직 그려지지 않은 화면의 poll을 취소하면 타이머와 추가 DOM 접근이 끝난다", () => {
    const query = vi.spyOn(document, "querySelector");
    const stop = start("connections-connected");
    expect(vi.getTimerCount()).toBe(1);
    vi.advanceTimersByTime(50);
    expect(query.mock.calls.length).toBeGreaterThan(1);
    expect(vi.getTimerCount()).toBe(1);

    stop();
    const queriesBefore = query.mock.calls.length;
    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(10_000);
    expect(query).toHaveBeenCalledTimes(queriesBefore);
    stop(); // 정리는 여러 번 호출해도 안전하다.
    expect(vi.getTimerCount()).toBe(0);
  });

  it("취소 전에 큐에서 꺼낸 callback도 취소 뒤에는 DOM에 접근하지 않는다", () => {
    const timers = vi.spyOn(window, "setTimeout");
    const query = vi.spyOn(document, "querySelector");
    const stop = start("connections-connected");
    const pending = timers.mock.calls.at(-1)?.[0];
    if (typeof pending !== "function") throw new Error("장면 대기 callback이 없다");

    stop();
    const queriesBefore = query.mock.calls.length;
    Reflect.apply(pending, undefined, []);
    expect(query).toHaveBeenCalledTimes(queriesBefore);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("한 장면을 정리해도 다른 장면의 대기를 지우지 않는다", () => {
    const first = start("connections-connected");
    const second = start("connections-confirm");
    expect(vi.getTimerCount()).toBe(2);
    first();
    expect(vi.getTimerCount()).toBe(1);
    vi.advanceTimersByTime(50);
    expect(vi.getTimerCount()).toBe(1);
    second();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("화면이 나타나면 기존대로 버튼을 누르고 다음 poll에서 대기를 끝낸다", () => {
    const clicked = vi.fn(() => {
      const main = document.createElement("main");
      main.className = "conn";
      document.body.append(main);
    });
    const stop = start("connections-connected");
    const header = document.createElement("header");
    header.className = "chat-header";
    const button = document.createElement("button");
    button.textContent = "계정 연결";
    button.addEventListener("click", clicked);
    header.append(button);
    document.body.append(header);

    vi.advanceTimersByTime(50);
    expect(clicked).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(50);
    expect(vi.getTimerCount()).toBe(0);
    expect(clicked).toHaveBeenCalledOnce();
    stop();
  });

  it("대상이 끝내 나타나지 않으면 기존 5초 상한에서 대기를 끝낸다", () => {
    start("connections-connected");
    vi.advanceTimersByTime(5000);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("동작이 없는 장면도 호출할 수 있는 정리 함수를 돌려준다", () => {
    const stop = start("chat-empty");
    expect(vi.getTimerCount()).toBe(0);
    stop();
    stop();
    expect(vi.getTimerCount()).toBe(0);
  });
});
