/** Google 로그인 콜백이 `/?login_error=<종류>`로 돌려보내는 실패 종류(서버 `LoginErrorKind`). */
const MESSAGES = {
  denied: "Google 로그인을 취소했습니다. 로그인하려면 버튼을 다시 눌러 주세요.",
  expired: "로그인 시간이 지났습니다. 버튼을 눌러 처음부터 다시 로그인해 주세요.",
  state: "로그인 정보를 확인하지 못했습니다. 버튼을 눌러 다시 로그인해 주세요.",
  nonce: "로그인 정보를 확인하지 못했습니다. 버튼을 눌러 다시 로그인해 주세요.",
  id_token: "로그인 정보를 확인하지 못했습니다. 버튼을 눌러 다시 로그인해 주세요.",
  exchange: "로그인 정보를 확인하지 못했습니다. 버튼을 눌러 다시 로그인해 주세요.",
  email_unverified: "인증되지 않은 이메일의 Google 계정으로는 로그인할 수 없습니다.",
  not_allowed: "이 Google 계정은 아직 사용할 수 없습니다. 허용된 계정으로 로그인해 주세요.",
  unavailable: "Google 또는 서버에 연결하지 못했습니다. 잠시 뒤 다시 로그인해 주세요.",
} as const;

export type LoginErrorKind = keyof typeof MESSAGES;

const GENERIC = "로그인하지 못했습니다. 버튼을 눌러 다시 시도해 주세요.";

function isKind(value: string): value is LoginErrorKind {
  return Object.hasOwn(MESSAGES, value);
}

/** 알려진 종류는 그 문구를, 그 밖의 값은 일반 문구를 쓴다(주소의 값을 화면에 그대로 옮기지 않는다). */
export function loginErrorMessage(kind: string): string {
  return isKind(kind) ? MESSAGES[kind] : GENERIC;
}

/** 현재 주소의 `login_error` 값. 없으면 null. */
export function readLoginError(search: string): string | null {
  return new URLSearchParams(search).get("login_error");
}

/** 새로고침해도 같은 오류가 다시 보이지 않게 주소에서 `login_error`를 지운다. */
export function clearLoginErrorFromUrl(): void {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("login_error")) return;
  url.searchParams.delete("login_error");
  window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
}
