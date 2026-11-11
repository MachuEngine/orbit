import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { isImeEnter, useComposer } from "./useComposer";

function Harness({ send }: { send: (text: string) => Promise<void> }) {
  const composer = useComposer(send);
  return (
    <div>
      <textarea aria-label="입력" {...composer.inputProps} />
      <output aria-label="상태">{composer.status}</output>
      <button type="button" onClick={() => void composer.retry()}>
        재시도
      </button>
    </div>
  );
}

describe("isImeEnter", () => {
  it("조합 중이거나 keyCode 229인 Enter만 IME Enter다", () => {
    expect(isImeEnter({ key: "Enter", isComposing: true })).toBe(true);
    expect(isImeEnter({ key: "Enter", isComposing: false, keyCode: 229 })).toBe(true);
    expect(isImeEnter({ key: "Enter", isComposing: false, keyCode: 13 })).toBe(false);
    expect(isImeEnter({ key: "a", isComposing: true })).toBe(false);
  });
});

describe("useComposer", () => {
  it("Enter로 전송하고 입력을 비운다", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    render(<Harness send={send} />);
    const input = screen.getByLabelText("입력");
    await userEvent.type(input, "안녕{Enter}");
    await waitFor(() => {
      expect(input).toHaveValue("");
    });
    expect(send).toHaveBeenCalledExactlyOnceWith("안녕");
  });

  it("Shift+Enter는 전송하지 않는다", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    render(<Harness send={send} />);
    await userEvent.type(screen.getByLabelText("입력"), "a{Shift>}{Enter}{/Shift}b");
    expect(send).not.toHaveBeenCalled();
  });

  it("IME 조합 중 Enter(isComposing)로 전송하지 않는다", () => {
    const send = vi.fn().mockResolvedValue(undefined);
    render(<Harness send={send} />);
    const input = screen.getByLabelText("입력");
    fireEvent.change(input, { target: { value: "한" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(send).not.toHaveBeenCalled();
  });

  it("composition 이벤트 사이의 Enter로 전송하지 않고, 끝난 뒤에는 전송한다", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    render(<Harness send={send} />);
    const input = screen.getByLabelText("입력");
    fireEvent.change(input, { target: { value: "한글" } });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(send).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
    await waitFor(() => {
      expect(send).toHaveBeenCalledExactlyOnceWith("한글");
    });
  });

  it("Safari처럼 조합 종료 직후 keyCode 229인 Enter는 전송하지 않는다", () => {
    const send = vi.fn().mockResolvedValue(undefined);
    render(<Harness send={send} />);
    const input = screen.getByLabelText("입력");
    fireEvent.change(input, { target: { value: "한글" } });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(send).not.toHaveBeenCalled();
  });

  it("빈 입력은 전송하지 않는다", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    render(<Harness send={send} />);
    await userEvent.type(screen.getByLabelText("입력"), "   {Enter}");
    expect(send).not.toHaveBeenCalled();
  });

  it("전송 중에는 중복 전송하지 않는다", async () => {
    let release!: () => void;
    const send = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    render(<Harness send={send} />);
    const input = screen.getByLabelText("입력");
    await userEvent.type(input, "안녕{Enter}");
    await userEvent.type(input, "{Enter}");
    expect(screen.getByLabelText("상태")).toHaveTextContent("sending");
    expect(send).toHaveBeenCalledTimes(1);
    release();
    await waitFor(() => {
      expect(screen.getByLabelText("상태")).toHaveTextContent("idle");
    });
  });

  it("실패하면 입력을 남기고 재시도로 같은 내용을 다시 보낸다", async () => {
    const send = vi.fn().mockRejectedValueOnce(new Error("x")).mockResolvedValueOnce(undefined);
    render(<Harness send={send} />);
    const input = screen.getByLabelText("입력");
    await userEvent.type(input, "안녕{Enter}");
    await waitFor(() => {
      expect(screen.getByLabelText("상태")).toHaveTextContent("failed");
    });
    expect(input).toHaveValue("안녕");
    await userEvent.click(screen.getByRole("button", { name: "재시도" }));
    await waitFor(() => {
      expect(screen.getByLabelText("상태")).toHaveTextContent("idle");
    });
    expect(send).toHaveBeenNthCalledWith(2, "안녕");
    expect(input).toHaveValue("");
  });
});
