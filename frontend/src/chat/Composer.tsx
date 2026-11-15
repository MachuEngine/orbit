import type { RefObject } from "react";
import type { useComposer } from "../composer/useComposer";
import { Icon } from "../ui/Icon";
import { VoiceToggle } from "../voice/VoiceToggle";
import type { VoiceInput } from "../voice/useVoiceInput";

/** 메시지 입력창. Enter로 보내고 Shift+Enter로 줄을 바꾼다. 한글 조합 중 Enter는 `useComposer`가 막는다. */
export function Composer({
  composer,
  inputRef,
  blocked = false,
  blockedHint = "승인 대기 중에는 전송할 수 없습니다. 위 카드에서 결정하면 보낼 수 있고, 입력한 내용은 그대로 남습니다.",
  failureText = "메시지를 전송하지 못했습니다. 연결을 확인한 뒤 다시 전송해 주세요.",
  voice,
  autoRead,
}: {
  composer: ReturnType<typeof useComposer>;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  /** 승인 대기·대화 불러오는 중이라 새 메시지를 보낼 수 없다. 입력은 계속 할 수 있다. */
  blocked?: boolean;
  /** 막힌 동안 입력창 아래에 보이는 안내. 기본은 승인 대기 안내다. */
  blockedHint?: string;
  /** 전송 실패 문구. 없으면 연결 확인을 안내하는 기본 문구를 쓴다. */
  failureText?: string;
  /** 음성 입력(ADR-0025). 없거나 브라우저가 지원하지 않으면 마이크 버튼을 그리지 않는다. */
  voice?: VoiceInput;
  /** "음성으로 질문하면 답을 자동으로 읽기" 설정. 없으면 체크 상자를 그리지 않는다. */
  autoRead?: { checked: boolean; onChange: (value: boolean) => void };
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
          className="input composer-input"
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
        {voice !== undefined && voice.supported && <VoiceToggle voice={voice} />}
        <button type="submit" className="btn btn-primary" disabled={sending || blocked}>
          {sending ? "전송 중" : "전송"}
        </button>
      </div>
      <p id="composer-hint" className="composer-hint">
        {blocked ? blockedHint : "Enter로 전송하고, Shift+Enter로 줄을 바꿉니다."}
      </p>
      {voice !== undefined && voice.supported && (
        <p className="voice-status" role="status">
          {voice.phase === "recording" && (
            <>
              <Icon name="mic" />
              <span>
                녹음 중입니다. 다시 누르면 멈추고 글자로 바꿉니다. Esc를 누르면 취소합니다.
              </span>
            </>
          )}
          {voice.phase === "transcribing" && <span>음성을 글자로 바꾸는 중입니다.</span>}
        </p>
      )}
      {voice?.error != null && (
        <p className="voice-error" role="alert">
          <Icon name="error" />
          <span>{voice.error}</span>
        </p>
      )}
      {autoRead !== undefined && (
        <label className="voice-auto-read">
          <input
            type="checkbox"
            checked={autoRead.checked}
            onChange={(event) => {
              autoRead.onChange(event.target.checked);
            }}
          />
          <span>음성으로 질문하면 답을 자동으로 읽기</span>
        </label>
      )}
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
