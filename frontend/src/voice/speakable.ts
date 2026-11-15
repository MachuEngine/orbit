/**
 * 비서 답을 소리 내어 읽기 좋은 문장으로 바꾼다(ADR-0025 §4). 순수 함수다.
 *
 * 뺀다: 코드 블록, 마크다운 기호, 주소(URL·이메일), 작업 집합 핸들(M3·E1·D2).
 * 600자를 넘으면 앞부분만 읽고 `TRUNCATION_NOTICE`로 끝낸다.
 * 승인 요청 카드, 도구 결과, 메일·문서 원문은 이 함수에 넣지 않는다(호출하는 쪽이 답 문장만 넘긴다).
 */

export const MAX_SPEAK_CHARS = 600;
export const TRUNCATION_NOTICE = "나머지는 화면에서 확인하세요";

const FENCED_CODE = /```[\s\S]*?(?:```|$)/g;
const IMAGE = /!\[[^\]\n]*\]\([^)\s]*\)/g;
const LINK = /\[([^\]\n]*)\]\([^)\s]*\)/g;
const URL_LIKE = /(?:https?:\/\/|www\.)[^\s)>\]]+/gi;
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/g;
// 핸들만 담은 괄호 묶음: "(E1)", "[M3, M4]"
const HANDLE_GROUP = /[([（]\s*[MED]\d{1,3}(?:\s*[,、]\s*[MED]\d{1,3})*\s*[)\]）]/g;
// 쉼표로 이어진 핸들 나열("M3, M4")도 한 덩어리로 뺀다.
const HANDLE = /\b[MED]\d{1,3}\b(?:\s*[,、]\s*[MED]\d{1,3}\b)*/g;
const SENTENCE_END = /[.!?。…]$/;

function stripMarkdown(line: string): string {
  return line
    .replace(/^\s{0,3}#{1,6}\s+/, "")
    .replace(/^\s*(?:[-*+]|\d{1,3}\.)\s+/, "")
    .replace(/^\s*>\s?/, "")
    .replace(/\*\*([^*\n]+)\*\*/g, "$1")
    .replace(/(?<![\w*])\*([^*\n]+)\*(?![\w*])/g, "$1")
    .replace(/`([^`\n]*)`/g, "$1")
    .replace(/`/g, "");
}

export function toSpeakableText(source: string): string {
  const withoutCode = source.replace(FENCED_CODE, "\n");
  const lines = withoutCode
    .split("\n")
    .map((line) =>
      stripMarkdown(
        line
          .replace(IMAGE, "")
          .replace(LINK, "$1")
          .replace(URL_LIKE, "")
          .replace(EMAIL, "")
          .replace(HANDLE_GROUP, "")
          .replace(HANDLE, ""),
      )
        // 빼고 남은 빈 괄호, 공백 앞의 쉼표를 정리한다.
        .replace(/[([（]\s*[)\]）]/g, "")
        .replace(/\s+([,.!?])/g, "$1")
        .replace(/\s+/g, " ")
        .trim(),
    )
    .filter((line) => line !== "");
  const text = lines.join(" ");
  if (text.length <= MAX_SPEAK_CHARS) return text;
  const head = text.slice(0, MAX_SPEAK_CHARS).trimEnd();
  return `${SENTENCE_END.test(head) ? head : `${head}.`} ${TRUNCATION_NOTICE}`;
}
