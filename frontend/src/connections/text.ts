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
  failed: `${PRODUCT_NAME}에서는 연결을 해제했지만 Google에서 권한을 거두었는지 확인하지 못했습니다. Google 계정 권한 페이지(myaccount.google.com/permissions)에서 ${PRODUCT_NAME}의 접근 권한을 직접 삭제해 주세요.`,
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

// ---------------------------------------------------------------- 메일 검색 색인 (ADR-0019)

export const INDEX_TEXT = {
  heading: "메일 검색 색인",
  intro: `고른 라벨과 기간의 메일만 ${PRODUCT_NAME} 서버에 저장해, 메일 내용으로 찾을 수 있게 합니다. 연결을 해제하면 저장한 메일도 함께 지워집니다.`,
  loading: "색인 정보를 불러오는 중입니다.",
  loadFailed: "색인 정보를 불러오지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
  retry: "다시 불러오기",
  pollStopped: "동기화 상태를 자동으로 불러오지 못해 갱신을 멈췄습니다.",
  pollUnauthorized: "로그인이 만료되었습니다. 다시 로그인한 뒤 확인해 주세요.",
  noScopes: "아직 고른 범위가 없습니다. 라벨과 기간을 골라 추가하면 색인을 시작합니다.",
  labelField: "라벨",
  periodField: "기간",
  chooseLabel: "라벨을 고르세요",
  add: "범위 추가",
  adding: "추가하는 중",
  added: "범위를 추가했습니다. 동기화를 시작합니다.",
  addFailed: "범위를 추가하지 못했습니다. 잠시 뒤 다시 눌러 주세요.",
  unknownLabel: "이 라벨을 찾지 못했습니다. 라벨 목록을 다시 불러온 뒤 골라 주세요.",
  labelsFailed: "라벨 목록을 불러오지 못했습니다.",
  scopeListLabel: "색인 범위",
  scopeDone: "완료",
  scopeWaiting: "대기",
  deleteConfirmLabel: "범위 삭제 확인",
  deleteConfirm: "이 범위를 지우면 색인에 저장된 이 범위의 메일도 함께 지워집니다. 지울까요?",
  deleteAction: "범위 삭제",
  cancel: "취소",
  deleted: "범위를 지웠습니다. 색인에서도 지웠습니다.",
  deleteFailed: "범위를 지우지 못했습니다. 잠시 뒤 다시 눌러 주세요.",
  countLabel: "색인한 메일",
  lastSyncLabel: "마지막 동기화",
  neverSynced: "아직 없음",
  syncNow: "지금 동기화",
  syncing: "동기화하는 중입니다. 끝나면 이 화면이 저절로 바뀝니다.",
  syncRequested: "동기화를 요청했습니다.",
  syncAlready: "이미 동기화를 기다리고 있습니다.",
  syncFailed: "동기화를 요청하지 못했습니다. 잠시 뒤 다시 눌러 주세요.",
  waitingForSync: "동기화를 기다리는 중입니다.",
  keepsOldIndex: "이미 저장한 메일로는 계속 찾을 수 있습니다.",
} as const;

export const WINDOW_OPTIONS: readonly { days: number; label: string }[] = [
  { days: 30, label: "최근 30일" },
  { days: 90, label: "최근 90일" },
  { days: 365, label: "최근 1년" },
];

export function windowLabel(days: number): string {
  return WINDOW_OPTIONS.find((o) => o.days === days)?.label ?? `최근 ${String(days)}일`;
}

const SYSTEM_LABEL_NAMES: Record<string, string> = {
  INBOX: "받은편지함",
  SENT: "보낸편지함",
  STARRED: "별표편지함",
  IMPORTANT: "중요편지함",
  UNREAD: "읽지 않은 메일",
  CATEGORY_PERSONAL: "개인",
  CATEGORY_SOCIAL: "소셜",
  CATEGORY_PROMOTIONS: "프로모션",
  CATEGORY_UPDATES: "업데이트",
  CATEGORY_FORUMS: "포럼",
};

/** 읽는 가치가 없거나 색인할 수 없는 시스템 라벨은 고르는 목록에서 뺀다. */
const HIDDEN_SYSTEM_LABELS = new Set(["DRAFT", "SPAM", "TRASH", "CHAT", "UNREAD"]);
/** 별표 변형(superstar)은 수시로 바뀌어 범위로 맞지 않는다. 서버(`mailindex/labels.py`)와 같은 목록이다. */
const SUPERSTAR_LABEL = /_(STAR|BANG|GUILLEMET|INFO|CHECK|QUESTION)$/;

export function isSelectableLabel(label: { id: string; kind: "system" | "user" }): boolean {
  return (
    label.kind === "user" || !(HIDDEN_SYSTEM_LABELS.has(label.id) || SUPERSTAR_LABEL.test(label.id))
  );
}

/** 시스템 라벨은 이해하기 쉬운 한국어 이름으로, 사용자 라벨(외부 문자열)은 원래 이름 그대로 보인다. */
export function labelDisplayName(label: {
  id: string;
  name: string;
  kind?: "system" | "user";
}): string {
  if (label.kind !== "user" && Object.hasOwn(SYSTEM_LABEL_NAMES, label.id)) {
    return SYSTEM_LABEL_NAMES[label.id] ?? label.name;
  }
  return label.name;
}

/** 범위 목록의 라벨 이름. 서버가 저장한 이름과 라벨 ID로 정한다(시스템 라벨 ID는 고정이다). */
export function scopeDisplayName(scope: { label_id: string; label_name: string }): string {
  return Object.hasOwn(SYSTEM_LABEL_NAMES, scope.label_id) && scope.label_name === scope.label_id
    ? (SYSTEM_LABEL_NAMES[scope.label_id] ?? scope.label_name)
    : scope.label_name;
}

const SYNC_ERROR_MESSAGES: Record<string, string> = {
  permission_revoked: "Google 계정 권한이 거부돼 동기화하지 못했습니다. 계정을 다시 연결해 주세요.",
  rate_limited: "Google 요청 한도에 걸려 동기화하지 못했습니다. 잠시 뒤 자동으로 다시 시도합니다.",
  transient_error:
    "Google에 일시적인 문제가 있어 동기화하지 못했습니다. 잠시 뒤 자동으로 다시 시도합니다.",
  service_disabled: "Google Cloud에서 Gmail API가 꺼져 있어 동기화하지 못했습니다.",
  quota_exceeded:
    "Google 사용 한도를 넘어 동기화하지 못했습니다. 한도가 풀리면 자동으로 다시 시도합니다.",
};

/** 동기화 오류 종류별 안내. 모르는 종류와 임베딩 오류는 일반 문구를 쓴다. */
export function syncErrorMessage(kind: string | null): string {
  if (kind !== null && Object.hasOwn(SYNC_ERROR_MESSAGES, kind)) {
    return SYNC_ERROR_MESSAGES[kind] ?? "";
  }
  if (kind?.startsWith("embedding_") === true) {
    return "메일 내용을 색인하는 서비스에 문제가 있어 동기화하지 못했습니다. 잠시 뒤 자동으로 다시 시도합니다.";
  }
  return "동기화하지 못했습니다. 잠시 뒤 자동으로 다시 시도합니다.";
}

/** 라벨 목록 조회가 커넥터 오류로 실패했을 때의 안내(`ApiError.detail`이 오류 종류다). */
export function labelsErrorMessage(detail: string | null): string {
  if (detail === "permission_revoked") return SYNC_ERROR_MESSAGES.permission_revoked ?? "";
  if (detail === "rate_limited")
    return "Google 요청 한도에 걸렸습니다. 잠시 뒤 다시 불러와 주세요.";
  return INDEX_TEXT.labelsFailed;
}

/** "N분 전". 절대 시각은 호출하는 쪽이 title로 보여 준다. */
export function agoText(iso: string, now: Date): string {
  const minutes = Math.floor((now.getTime() - new Date(iso).getTime()) / 60_000);
  if (minutes < 1) return "방금 전";
  if (minutes < 60) return `${String(minutes)}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${String(hours)}시간 전`;
  return `${String(Math.floor(hours / 24))}일 전`;
}
