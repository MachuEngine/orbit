/** 연결 화면에 쓰는 문구. 서버가 주는 값은 알려진 것만 사람이 읽는 말로 바꾼다. */
import { PRODUCT_NAME } from "../brand";

/** 범위 주소는 `/auth/` 뒤의 이름으로 알아본다. 정해진 순서가 화면 표시 순서다. */
const SCOPE_LABELS: [string, string][] = [
  ["calendar.events", "일정 보기·만들기·수정"],
  ["calendar.calendarlist.readonly", "캘린더 목록 보기"],
  ["gmail.readonly", "메일 읽기·검색"],
  ["gmail.compose", "메일 초안 작성·보내기"],
];

/** 허용된 범위 중 사람이 읽는 말이 있는 것만 돌려준다. 모르는 값은 보이지 않는다. */
export function scopeLabels(scopes: readonly string[]): string[] {
  return SCOPE_LABELS.filter(([name]) =>
    scopes.some((scope) => scope.endsWith(`/auth/${name}`)),
  ).map(([, label]) => label);
}

const RESULT_MESSAGES: Record<string, string> = {
  denied: "Google 연결을 취소했습니다. 연결하려면 버튼을 다시 눌러 주세요.",
  expired: "연결 시간이 지났습니다. 버튼을 눌러 처음부터 다시 연결해 주세요.",
  state: "연결 정보를 확인하지 못했습니다. 버튼을 눌러 다시 연결해 주세요.",
  nonce: "연결 정보를 확인하지 못했습니다. 버튼을 눌러 다시 연결해 주세요.",
  id_token: "연결 정보를 확인하지 못했습니다. 버튼을 눌러 다시 연결해 주세요.",
  exchange: "연결 정보를 확인하지 못했습니다. 버튼을 눌러 다시 연결해 주세요.",
  email_unverified: "인증되지 않은 이메일의 Google 계정은 연결할 수 없습니다.",
  scope_missing:
    "필요한 권한이 모두 허용되지 않아 연결하지 못했습니다. 다시 연결하면서 모든 항목을 허용해 주세요.",
  no_refresh_token:
    "Google이 계속 사용할 수 있는 권한을 주지 않았습니다. 다시 연결하면서 모든 항목을 허용해 주세요.",
  already_connected: "이미 연결된 계정이 있습니다. 먼저 연결을 해제해 주세요.",
  unauthenticated: "로그인이 필요합니다. 로그인한 뒤 다시 연결해 주세요.",
  unavailable: "Google 또는 서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
};

const GENERIC_FAILURE = "연결하지 못했습니다. 버튼을 눌러 다시 시도해 주세요.";

/** 알려진 종류는 그 문구를, 그 밖의 값은 일반 문구를 쓴다(주소의 값을 화면에 그대로 옮기지 않는다). */
export function connectionErrorMessage(kind: string): string {
  return Object.hasOwn(RESULT_MESSAGES, kind)
    ? (RESULT_MESSAGES[kind] ?? GENERIC_FAILURE)
    : GENERIC_FAILURE;
}

export const CONNECTED_MESSAGE = "Google 계정을 연결했습니다.";

export const REVOKE_MESSAGES = {
  revoked: "연결을 해제했습니다. Google에서도 권한을 거두었습니다.",
  none: "연결을 해제했습니다.",
  failed: `${PRODUCT_NAME}에서는 연결을 해제했지만 Google에서 권한을 거두었는지 확인하지 못했습니다. Google 계정의 보안 설정에서 ${PRODUCT_NAME}의 접근 권한을 직접 삭제해 주세요.`,
} as const;

export type ConnectionResult = { kind: "connected" } | { kind: "error"; error: string };

/** 현재 주소의 연결 결과(`?connection=connected` 또는 `?connection_error=<종류>`). 없으면 null. */
export function readConnectionResult(search: string): ConnectionResult | null {
  const params = new URLSearchParams(search);
  const error = params.get("connection_error");
  if (error !== null) return { kind: "error", error };
  if (params.get("connection") === "connected") return { kind: "connected" };
  return null;
}

/** 새로고침해도 같은 결과가 다시 보이지 않게 주소에서 연결 결과를 지운다. */
export function clearConnectionResultFromUrl(): void {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("connection") && !url.searchParams.has("connection_error")) return;
  url.searchParams.delete("connection");
  url.searchParams.delete("connection_error");
  window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
}
