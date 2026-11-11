import type { RefObject } from "react";
import type { useComposer } from "../composer/useComposer";

/** 메시지 입력창. Enter로 보내고 Shift+Enter로 줄을 바꾼다. 한글 조합 중 Enter는 `useComposer`가 막는다. */
export function Composer({
  composer,
  inputRef,
}: {
  composer: ReturnType<typeof useComposer>;
  inputRef: RefObject<HTMLTextAreaElement | null>;
}) {
  const sending = composer.status === "sending";
  const failed = composer.status === "failed";
  const describedBy = failed ? "composer-hint composer-error" : "composer-hint";
  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        void composer.submit();
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
        />
        <button type="submit" className="btn btn-primary" disabled={sending}>
          {sending ? "보내는 중" : "보내기"}
        </button>
      </div>
      <p id="composer-hint" className="composer-hint">
        Enter로 보내고, Shift+Enter로 줄을 바꿉니다.
      </p>
      {failed && (
        <p id="composer-error" className="composer-error" role="alert">
          메시지를 보내지 못했습니다. 연결을 확인한 뒤 다시 보내 주세요.{" "}
          <button type="button" className="btn btn-secondary" onClick={() => void composer.retry()}>
            다시 보내기
          </button>
        </p>
      )}
    </form>
  );
}
