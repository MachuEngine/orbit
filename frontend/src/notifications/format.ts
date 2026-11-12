const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

const TIME = new Intl.DateTimeFormat("ko-KR", { hour: "numeric", minute: "2-digit" });

function startOfDay(ms: number): number {
  const d = new Date(ms);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** 시각을 "오후 3:00"처럼 쓴다. 읽을 수 없는 값이면 빈 문자열이다. */
export function clockText(iso: string): string {
  const time = new Date(iso);
  return Number.isNaN(time.getTime()) ? "" : TIME.format(time);
}

/**
 * 예정된 알림이 울릴 시각을 쓴다. 한 시간 안이면 "N분 뒤", 그 뒤로는 "오늘 오후 3:00",
 * "내일 오전 9:00", 더 멀면 "11월 14일 오후 3:00"이다. 읽을 수 없는 값이면 빈 문자열이다.
 */
export function formatUpcoming(iso: string, now: number): string {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return "";
  const ahead = time.getTime() - now;
  if (ahead < MINUTE) return "곧";
  if (ahead < HOUR) return `${String(Math.floor(ahead / MINUTE))}분 뒤`;
  const days = Math.round((startOfDay(time.getTime()) - startOfDay(now)) / (24 * HOUR));
  const clock = TIME.format(time);
  if (days === 0) return `오늘 ${clock}`;
  if (days === 1) return `내일 ${clock}`;
  const date = `${String(time.getMonth() + 1)}월 ${String(time.getDate())}일`;
  return time.getFullYear() === new Date(now).getFullYear()
    ? `${date} ${clock}`
    : `${String(time.getFullYear())}년 ${date} ${clock}`;
}
