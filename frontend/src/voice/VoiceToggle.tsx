import { Icon } from "../ui/Icon";
import { MAX_RECORD_SECONDS, type VoiceInput } from "./useVoiceInput";

function clock(seconds: number): string {
  return `${String(Math.floor(seconds / 60))}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * 입력창 옆 마이크 토글(ADR-0025 §5). 누르면 녹음을 시작하고 다시 누르면 멈추고 전사한다.
 * 경과 시간은 눈으로만 보이게 하고(매초 읽히지 않게 aria-hidden), 상태 안내는 입력창 아래의 상태 영역이 맡는다.
 */
export function VoiceToggle({ voice }: { voice: VoiceInput }) {
  const recording = voice.phase === "recording";
  const transcribing = voice.phase === "transcribing";
  return (
    <button
      type="button"
      className={`btn btn-secondary voice-toggle${recording ? " voice-toggle-on" : ""}`}
      aria-pressed={recording}
      // disabled를 쓰면 포커스가 사라지므로 aria-disabled로 두고 클릭만 무시한다.
      aria-disabled={transcribing}
      aria-busy={transcribing}
      onClick={() => {
        if (!transcribing) voice.toggle();
      }}
    >
      <Icon name="mic" />
      <span className="voice-toggle-label">{transcribing ? "변환 중" : "음성 입력"}</span>
      {recording && (
        <span className="voice-timer num" aria-hidden="true">
          {clock(voice.elapsed)} / {clock(MAX_RECORD_SECONDS)}
        </span>
      )}
    </button>
  );
}
