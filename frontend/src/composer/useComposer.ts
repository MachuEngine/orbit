import { useCallback, useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";

export type ComposerStatus = "idle" | "sending" | "failed";

/** Safari는 조합이 끝난 직후의 Enter keydown에 `isComposing=false`, `keyCode=229`를 준다. */
const IME_KEY_CODE = 229;

export function isImeEnter(event: {
  key: string;
  isComposing?: boolean;
  keyCode?: number;
}): boolean {
  return event.key === "Enter" && (event.isComposing === true || event.keyCode === IME_KEY_CODE);
}

/**
 * 채팅 입력 로직. 화면 스타일은 갖지 않는다.
 * - Enter로 전송하되 IME 조합 중에는 전송하지 않는다. Shift+Enter는 줄바꿈이다.
 * - 전송 중에는 다시 전송하지 않는다.
 * - 실패하면 입력을 비우지 않고, `retry`로 같은 내용을 다시 보낸다.
 */
export function useComposer(send: (text: string) => Promise<void>) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<ComposerStatus>("idle");
  const composing = useRef(false);
  const sending = useRef(false);
  const failedText = useRef<string | null>(null);

  const deliver = useCallback(
    async (message: string) => {
      if (sending.current) return;
      sending.current = true;
      setStatus("sending");
      try {
        await send(message);
        failedText.current = null;
        setText((current) => (current === message ? "" : current));
        setStatus("idle");
      } catch {
        failedText.current = message;
        setStatus("failed");
      } finally {
        sending.current = false;
      }
    },
    [send],
  );

  const submit = useCallback(async () => {
    const message = text.trim();
    if (message === "" || composing.current) return;
    await deliver(message);
  }, [text, deliver]);

  const retry = useCallback(async () => {
    if (failedText.current !== null) await deliver(failedText.current);
  }, [deliver]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key !== "Enter" || event.shiftKey) return;
      if (composing.current || isImeEnter(event.nativeEvent)) return;
      event.preventDefault();
      void submit();
    },
    [submit],
  );

  const inputProps = {
    value: text,
    onChange: (event: ChangeEvent<HTMLTextAreaElement>) => {
      setText(event.target.value);
    },
    onKeyDown,
    onCompositionStart: () => {
      composing.current = true;
    },
    onCompositionEnd: () => {
      composing.current = false;
    },
  };

  return { text, status, inputProps, submit, retry };
}
