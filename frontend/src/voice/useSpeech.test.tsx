import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { loadAutoRead, saveAutoRead } from "./autoRead";
import { FakeUtterance, installFakeSpeech, voice } from "./speechTestSupport";
import { isSpeechSupported, pickKoreanVoice, useSpeech } from "./useSpeech";

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

function Harness() {
  const speech = useSpeech();
  return (
    <div>
      <output aria-label="읽는 중">{speech.speakingKey ?? ""}</output>
      <button
        type="button"
        onClick={() => {
          speech.toggle("a", "첫 번째 답");
        }}
      >
        A
      </button>
      <button
        type="button"
        onClick={() => {
          speech.toggle("b", "두 번째 답");
        }}
      >
        B
      </button>
      <button type="button" onClick={speech.stop}>
        멈춤
      </button>
    </div>
  );
}

describe("pickKoreanVoice", () => {
  it("ko-KR 음성을 우선하고 다른 한국어 음성이 그다음이며 없으면 undefined다", () => {
    const voices = [voice("en-US"), voice("ko", "기타"), voice("ko_KR", "표준"), voice("ja-JP")];
    expect(pickKoreanVoice(voices)?.name).toBe("표준");
    expect(pickKoreanVoice([voice("en-US"), voice("ko", "기타")])?.name).toBe("기타");
    expect(pickKoreanVoice([voice("en-US")])).toBeUndefined();
    expect(pickKoreanVoice([])).toBeUndefined();
  });
});

describe("useSpeech", () => {
  it("speechSynthesis가 없으면 지원하지 않는다", () => {
    vi.stubGlobal("SpeechSynthesisUtterance", undefined);
    expect(isSpeechSupported()).toBe(false);
  });

  it("한국어 음성으로 읽고, 같은 답을 다시 누르면 멈춘다", async () => {
    const korean = voice("ko-KR", "한국어");
    const synth = installFakeSpeech([voice("en-US"), korean]);
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "A" }));
    const utterance = FakeUtterance.instances[0];
    expect(utterance?.text).toBe("첫 번째 답");
    expect(utterance?.lang).toBe("ko-KR");
    expect(utterance?.voice).toBe(korean);
    expect(synth.speak).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("읽는 중")).toHaveTextContent("a");

    await userEvent.click(screen.getByRole("button", { name: "A" }));
    expect(synth.cancel).toHaveBeenCalled();
    expect(synth.speak).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("읽는 중")).toHaveTextContent("");
  });

  it("한국어 음성이 없으면 voice를 지정하지 않고 lang만 지정한다", async () => {
    installFakeSpeech([voice("en-US")]);
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "A" }));
    expect(FakeUtterance.instances[0]?.voice).toBeNull();
    expect(FakeUtterance.instances[0]?.lang).toBe("ko-KR");
  });

  it("다른 답을 누르면 읽던 것을 멈추고 새로 읽으며, 이전 발화의 종료 이벤트는 무시한다", async () => {
    installFakeSpeech();
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "A" }));
    await userEvent.click(screen.getByRole("button", { name: "B" }));
    expect(screen.getByLabelText("읽는 중")).toHaveTextContent("b");
    act(() => {
      FakeUtterance.instances[0]?.onerror?.(); // 취소된 첫 발화가 뒤늦게 알린다
    });
    expect(screen.getByLabelText("읽는 중")).toHaveTextContent("b");
    act(() => {
      FakeUtterance.instances[1]?.onend?.();
    });
    expect(screen.getByLabelText("읽는 중")).toHaveTextContent("");
  });

  it("화면이 사라지면 읽기를 멈춘다", async () => {
    const synth = installFakeSpeech();
    const { unmount } = render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "A" }));
    synth.cancel.mockClear();
    unmount();
    expect(synth.cancel).toHaveBeenCalled();
  });
});

describe("자동 읽기 설정 저장", () => {
  it("기본은 꺼짐이고 저장한 값을 다시 읽는다", () => {
    expect(loadAutoRead()).toBe(false);
    saveAutoRead(true);
    expect(loadAutoRead()).toBe(true);
    saveAutoRead(false);
    expect(loadAutoRead()).toBe(false);
  });

  it("저장소를 쓸 수 없으면 예외 없이 꺼짐으로 둔다", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    expect(loadAutoRead()).toBe(false);
    expect(() => {
      saveAutoRead(true);
    }).not.toThrow();
    vi.restoreAllMocks();
  });
});
