import { toSpeakableText } from "./speakable";

/**
 * 비서 답 아래의 "읽어 주기" 버튼. 읽는 중에 다시 누르면 멈춘다(ADR-0025 §4).
 * 읽을 문장이 남지 않는 답(코드 블록뿐인 답 등)에는 버튼을 그리지 않는다.
 */
export function ReadAloudButton({
  text,
  speaking,
  onToggle,
}: {
  /** 비서 답 원문(마크다운). 읽기 좋은 문장으로 바꿔 넘긴다. */
  text: string;
  speaking: boolean;
  onToggle: (speakable: string) => void;
}) {
  const speakable = toSpeakableText(text);
  if (speakable === "") return null;
  return (
    <button
      type="button"
      className="btn btn-secondary read-aloud"
      onClick={() => {
        onToggle(speakable);
      }}
    >
      {speaking ? "읽기 멈추기" : "읽어 주기"}
    </button>
  );
}
