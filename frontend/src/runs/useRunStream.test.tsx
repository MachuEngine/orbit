import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useRunStream } from "./useRunStream";

class FakeSource extends EventTarget {
  readyState = 1;
  onerror: (() => void) | null = null;
  close() {
    this.readyState = 2;
  }
}

function Probe({ factory }: { factory: () => EventSource }) {
  // factory는 effect 의존성이므로 렌더마다 바뀌지 않게 밖에서 만들어 넘긴다.
  const { state, connection } = useRunStream("run-1", factory);
  return (
    <div>
      <output aria-label="상태">{state.status}</output>
      <output aria-label="연결">{connection}</output>
      <ul>
        {state.messages.map((m) => (
          <li key={m.id}>{m.text}</li>
        ))}
      </ul>
    </div>
  );
}

describe("useRunStream", () => {
  it("이벤트를 화면 상태로 모으고 끊김을 연결 상태로 알린다", () => {
    const source = new FakeSource();
    render(<Probe factory={() => source as unknown as EventSource} />);
    act(() => {
      source.dispatchEvent(
        new MessageEvent("assistant_text", {
          data: '{"step":0,"text":"안녕하세요"}',
          lastEventId: "1",
        }),
      );
      source.dispatchEvent(
        new MessageEvent("status", { data: '{"status":"running"}', lastEventId: "2" }),
      );
    });
    expect(screen.getByText("안녕하세요")).toBeInTheDocument();
    expect(screen.getByLabelText("상태")).toHaveTextContent("running");

    act(() => {
      source.readyState = 0;
      source.onerror?.();
    });
    expect(screen.getByLabelText("연결")).toHaveTextContent("reconnecting");
  });
});
