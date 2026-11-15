import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import { FakeRecorder, installFakeMic, type FakeMic } from "./voiceTestSupport";
import {
  isVoiceInputSupported,
  MAX_RECORD_SECONDS,
  RECORD_BITS_PER_SECOND,
  useVoiceInput,
  VOICE_TEXT,
  type UseVoiceInputOptions,
} from "./useVoiceInput";
import { VoiceToggle } from "./VoiceToggle";

function Harness(props: UseVoiceInputOptions) {
  const voice = useVoiceInput(props);
  return (
    <div>
      {voice.supported && <VoiceToggle voice={voice} />}
      <output aria-label="상태">{voice.phase}</output>
      <output aria-label="경과">{voice.elapsed}</output>
      <output aria-label="오류">{voice.error ?? ""}</output>
      <button type="button" onClick={voice.cancel}>
        취소
      </button>
    </div>
  );
}

let mic: (FakeMic & { restore: () => void }) | null = null;

beforeEach(() => {
  mic = installFakeMic();
});

afterEach(() => {
  mic?.restore();
  mic = null;
  vi.useRealTimers();
});

const phase = () => screen.getByLabelText("상태").textContent;
const toggle = () => screen.getByRole("button", { name: /음성 입력|변환 중/ });

describe("useVoiceInput", () => {
  it("한 번 누르면 녹음을 시작하고 다시 누르면 멈추고 전사해 결과를 넘긴다", async () => {
    const transcribe = vi.fn().mockResolvedValue("  내일 일정 알려 줘 ");
    const onTranscript = vi.fn();
    const onStart = vi.fn();
    render(<Harness onTranscript={onTranscript} onStart={onStart} transcribe={transcribe} />);
    expect(toggle()).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("recording");
    });
    expect(onStart).toHaveBeenCalledOnce();
    expect(toggle()).toHaveAttribute("aria-pressed", "true");
    expect(FakeRecorder.instances[0]?.options).toEqual({
      mimeType: "audio/webm;codecs=opus",
      audioBitsPerSecond: RECORD_BITS_PER_SECOND,
    });

    await userEvent.click(toggle());
    await waitFor(() => {
      expect(onTranscript).toHaveBeenCalledExactlyOnceWith("내일 일정 알려 줘");
    });
    expect(transcribe).toHaveBeenCalledOnce();
    const sent = transcribe.mock.calls[0]?.[0] as Blob;
    expect(sent.size).toBeGreaterThan(0);
    expect(mic?.track.stop).toHaveBeenCalled(); // 마이크를 놓았다
    expect(phase()).toBe("idle");
  });

  it("브라우저가 webm/opus를 지원하지 않으면 지원하는 형식으로 대체하고, 모두 없으면 기본 형식이다", async () => {
    FakeRecorder.isTypeSupported.mockImplementation((type: string) => type === "audio/mp4");
    render(<Harness onTranscript={vi.fn()} transcribe={vi.fn().mockResolvedValue("x")} />);
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("recording");
    });
    expect(FakeRecorder.instances[0]?.options).toEqual({
      mimeType: "audio/mp4",
      audioBitsPerSecond: RECORD_BITS_PER_SECOND,
    });
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("idle");
    });

    FakeRecorder.isTypeSupported.mockImplementation(() => false);
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("recording");
    });
    expect(FakeRecorder.instances[1]?.options).toEqual({
      audioBitsPerSecond: RECORD_BITS_PER_SECOND,
    });
  });

  it("경과 시간을 보이고 60초가 되면 자동으로 멈춰 전사한다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    const onTranscript = vi.fn();
    const transcribe = vi.fn().mockResolvedValue("자동 종료");
    render(<Harness onTranscript={onTranscript} transcribe={transcribe} />);
    await act(async () => {
      toggle().click();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(phase()).toBe("recording");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.getByLabelText("경과")).toHaveTextContent("5");
    expect(toggle()).toHaveTextContent("0:05 / 1:00");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_RECORD_SECONDS * 1000 - 5000);
    });
    expect(transcribe).toHaveBeenCalledOnce();
    expect(onTranscript).toHaveBeenCalledWith("자동 종료");
    expect(phase()).toBe("idle");
  });

  it("Esc를 누르면 전사하지 않고 취소한다", async () => {
    const transcribe = vi.fn().mockResolvedValue("보내지 않음");
    const onTranscript = vi.fn();
    render(<Harness onTranscript={onTranscript} transcribe={transcribe} />);
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("recording");
    });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => {
      expect(phase()).toBe("idle");
    });
    expect(transcribe).not.toHaveBeenCalled();
    expect(onTranscript).not.toHaveBeenCalled();
    expect(mic?.track.stop).toHaveBeenCalled();
  });

  it("다른 곳이 이미 처리한 Esc나 범위 밖 Esc는 가로채지 않는다", async () => {
    let inScope = false;
    const transcribe = vi.fn().mockResolvedValue("x");
    render(
      <Harness onTranscript={vi.fn()} transcribe={transcribe} shouldHandleEscape={() => inScope} />,
    );
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("recording");
    });
    // 이미 preventDefault된 Esc(다른 대화 상자가 닫히는 중)
    const handled = new KeyboardEvent("keydown", {
      key: "Escape",
      cancelable: true,
      bubbles: true,
    });
    handled.preventDefault();
    document.dispatchEvent(handled);
    // 범위 밖(포커스가 다른 대화 상자 안)
    await userEvent.keyboard("{Escape}");
    expect(phase()).toBe("recording");
    inScope = true;
    await userEvent.keyboard("{Escape}");
    await waitFor(() => {
      expect(phase()).toBe("idle");
    });
    expect(transcribe).not.toHaveBeenCalled();
  });

  it("녹음 중이 아닐 때의 Esc는 아무것도 하지 않는다", async () => {
    render(<Harness onTranscript={vi.fn()} transcribe={vi.fn()} />);
    await userEvent.keyboard("{Escape}");
    expect(phase()).toBe("idle");
  });

  it("마이크 권한이 거부되면 안내 문구를 보이고 녹음하지 않는다", async () => {
    mic?.restore();
    mic = installFakeMic({ error: new DOMException("denied", "NotAllowedError") });
    const transcribe = vi.fn();
    render(<Harness onTranscript={vi.fn()} transcribe={transcribe} />);
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(screen.getByLabelText("오류")).toHaveTextContent(VOICE_TEXT.permission);
    });
    expect(phase()).toBe("idle");
    expect(FakeRecorder.instances).toHaveLength(0);
    // 다시 누르면 새로 시도한다(이전 오류는 지운다).
    await userEvent.click(toggle());
    expect(mic.getUserMedia).toHaveBeenCalledTimes(2);
  });

  it("마이크가 없으면 다른 안내를 보인다", async () => {
    mic?.restore();
    mic = installFakeMic({ error: new DOMException("none", "NotFoundError") });
    render(<Harness onTranscript={vi.fn()} transcribe={vi.fn()} />);
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(screen.getByLabelText("오류")).toHaveTextContent(VOICE_TEXT.noDevice);
    });
  });

  it("getUserMedia나 MediaRecorder가 없는 환경에서는 지원하지 않는다고 알리고 버튼을 그리지 않는다", () => {
    mic?.restore();
    mic = null;
    expect(isVoiceInputSupported()).toBe(false);
    render(<Harness onTranscript={vi.fn()} transcribe={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /음성 입력/ })).not.toBeInTheDocument();

    mic = installFakeMic();
    vi.stubGlobal("MediaRecorder", undefined);
    expect(isVoiceInputSupported()).toBe(false);
  });

  it("전사 오류는 종류별 문구로 알리고 입력창에는 아무것도 넣지 않는다", async () => {
    const cases: [string, string][] = [
      ["usage_limit_exceeded", VOICE_TEXT.limit],
      ["too_long", VOICE_TEXT.tooLong],
      ["unsupported_format", VOICE_TEXT.format],
      ["provider_error", VOICE_TEXT.provider],
      ["stt_unavailable", VOICE_TEXT.unavailable],
    ];
    for (const [detail, text] of cases) {
      const onTranscript = vi.fn();
      const transcribe = vi.fn().mockRejectedValue(new ApiError("server", { status: 429, detail }));
      const { unmount } = render(<Harness onTranscript={onTranscript} transcribe={transcribe} />);
      await userEvent.click(toggle());
      await waitFor(() => {
        expect(phase()).toBe("recording");
      });
      await userEvent.click(toggle());
      await waitFor(() => {
        expect(screen.getByLabelText("오류")).toHaveTextContent(text);
      });
      expect(onTranscript).not.toHaveBeenCalled();
      expect(phase()).toBe("idle");
      unmount();
    }
  });

  it("네트워크 오류와 빈 전사 결과도 안내한다", async () => {
    const transcribe = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("network", {}))
      .mockResolvedValueOnce("   ");
    render(<Harness onTranscript={vi.fn()} transcribe={transcribe} />);
    for (const expected of [VOICE_TEXT.network, VOICE_TEXT.empty]) {
      await userEvent.click(toggle());
      await waitFor(() => {
        expect(phase()).toBe("recording");
      });
      await userEvent.click(toggle());
      await waitFor(() => {
        expect(screen.getByLabelText("오류")).toHaveTextContent(expected);
      });
    }
  });

  it("전사 중에는 버튼이 비활성이고 다시 시작하지 않는다", async () => {
    let resolve: (text: string) => void = () => undefined;
    const transcribe = vi.fn(
      () =>
        new Promise<string>((r) => {
          resolve = r;
        }),
    );
    render(<Harness onTranscript={vi.fn()} transcribe={transcribe} />);
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("recording");
    });
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("transcribing");
    });
    // disabled가 아니라 aria-disabled라 포커스를 유지하고, 눌러도 아무 일도 없다.
    expect(toggle()).toHaveAttribute("aria-disabled", "true");
    expect(toggle()).toHaveAttribute("aria-busy", "true");
    expect(toggle()).not.toBeDisabled();
    toggle().focus();
    await userEvent.click(toggle());
    expect(toggle()).toHaveFocus();
    expect(phase()).toBe("transcribing");
    expect(toggle()).toHaveTextContent("변환 중");
    resolve("완료");
    await waitFor(() => {
      expect(phase()).toBe("idle");
    });
    expect(transcribe).toHaveBeenCalledOnce();
  });

  it("녹음 중에 화면이 사라지면 마이크 트랙을 멈추고 전사하지 않는다", async () => {
    const transcribe = vi.fn();
    const { unmount } = render(<Harness onTranscript={vi.fn()} transcribe={transcribe} />);
    await userEvent.click(toggle());
    await waitFor(() => {
      expect(phase()).toBe("recording");
    });
    unmount();
    expect(mic?.track.stop).toHaveBeenCalled();
    expect(FakeRecorder.instances[0]?.state).toBe("inactive");
    expect(transcribe).not.toHaveBeenCalled();
  });

  it("마이크 권한을 기다리는 사이 화면이 사라져도 얻은 트랙을 멈춘다", async () => {
    let grant: (stream: MediaStream) => void = () => undefined;
    const track = { stop: vi.fn() };
    mic?.getUserMedia.mockReturnValue(
      new Promise<MediaStream>((resolve) => {
        grant = resolve;
      }),
    );
    const { unmount } = render(<Harness onTranscript={vi.fn()} transcribe={vi.fn()} />);
    await userEvent.click(toggle());
    unmount();
    grant({ getTracks: () => [track] } as unknown as MediaStream);
    await waitFor(() => {
      expect(track.stop).toHaveBeenCalled();
    });
    expect(FakeRecorder.instances).toHaveLength(0);
  });
});
