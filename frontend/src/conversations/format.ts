const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export const UNTITLED_CONVERSATION = "제목 없는 대화";

export function conversationTitle(title: string | null): string {
  const trimmed = title?.trim() ?? "";
  return trimmed === "" ? UNTITLED_CONVERSATION : trimmed;
}

/**
 * 마지막 활동 시각을 짧게 쓴다. 하루 안이면 상대 표현("3분 전"), 그보다 오래되면 날짜("11월 5일")이고
 * 해가 다르면 연도를 붙인다. 읽을 수 없는 값이면 빈 문자열이다.
 */
export function formatLastActivity(iso: string, now: number): string {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return "";
  const elapsed = now - time.getTime();
  if (elapsed < MINUTE) return "방금 전";
  if (elapsed < HOUR) return `${String(Math.floor(elapsed / MINUTE))}분 전`;
  if (elapsed < DAY) return `${String(Math.floor(elapsed / HOUR))}시간 전`;
  const date = `${String(time.getMonth() + 1)}월 ${String(time.getDate())}일`;
  return time.getFullYear() === new Date(now).getFullYear()
    ? date
    : `${String(time.getFullYear())}년 ${date}`;
}
