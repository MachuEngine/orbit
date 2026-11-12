import type { RefObject } from "react";
import type { useComposer } from "../composer/useComposer";

/** 메시지 입력창. Enter로 보내고 Shift+Enter로 줄을 바꾼다. 한글 조합 중 Enter는 `useComposer`가 막는다. */
export function Composer({
  composer,
  inputRef,
  blocked = false,
  blockedHint = "승인 대기 중에는 전송할 수 없습니다. 위 카드에서 결정하면 보낼 수 있고, 입력한 내용은 그대로 남습니다.",
  failureText = "메시지를 전송하지 못했습니다. 연결을 확인한 뒤 다시 전송해 주세요.",
}: {
  composer: ReturnType<typeof useComposer>;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  /** 승인 대기·대화 불러오는 중이라 새 메시지를 보낼 수 없다. 입력은 계속 할 수 있다. */
  blocked?: boolean;
  /** 막힌 동안 입력창 아래에 보이는 안내. 기본은 승인 대기 안내다. */
  blockedHint?: string;
  /** 전송 실패 문구. 없으면 연결 확인을 안내하는 기본 문구를 쓴다. */
  failureText?: string;
}) {
  const sending = composer.status === "sending";
  const failed = composer.status === "failed";
  const describedBy = failed ? "composer-hint composer-error" : "composer-hint";
  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (!blocked) void composer.submit();
      }}
    >
      <label className="visually-hidden" htmlFor="composer-input">
        메시지
      </label>
      <div className="composer-row">
        <textarea
          id="composer-input"
          ref={inputRef}
          className="composer-input"
          rows={1}
          aria-describedby={describedBy}
          aria-invalid={failed}
          {...composer.inputProps}
          onKeyDown={(event) => {
            // 승인 대기 중에는 Enter로도 전송하지 않는다. 입력한 내용은 그대로 둔다.
            if (blocked && event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              return;
            }
            composer.inputProps.onKeyDown(event);
          }}
        />
        <button type="submit" className="btn btn-primary" disabled={sending || blocked}>
          {sending ? "전송 중" : "전송"}
        </button>
      </div>
      <p id="composer-hint" className="composer-hint">
        {blocked ? blockedHint : "Enter로 전송하고, Shift+Enter로 줄을 바꿉니다."}
      </p>
      {failed && (
        <p id="composer-error" className="composer-error" role="alert">
          {failureText}{" "}
          <button type="button" className="btn btn-secondary" onClick={() => void composer.retry()}>
            다시 전송
          </button>
        </p>
      )}
    </form>
  );
}
