/**
 * 창 이동 판정(ADR-0024 §3). 순수 함수라 Electron 없이 테스트한다.
 * - `allow`: 서버 출처의 http/https 주소.
 * - `external`: Google 로그인·동의 주소. 시스템 브라우저로 연다(앱 창에서는 Google 로그인이 금지다).
 * - `block`: 그 밖의 모든 주소.
 */
export type NavigationDecision =
  { action: "allow" } | { action: "external"; url: string } | { action: "block" };

/** 시스템 브라우저로 넘길 수 있는 Google 로그인·동의 호스트. */
export const GOOGLE_AUTH_HOSTS: readonly string[] = ["accounts.google.com"];

function parse(raw: string): URL | null {
  try {
    return new URL(raw);
  } catch {
    return null;
  }
}

/** URL이 사용자 정보(`user:pw@`)를 갖고 있는가. 호스트를 헷갈리게 하는 우회에 쓰인다. */
function hasCredentials(url: URL): boolean {
  return url.username !== "" || url.password !== "";
}

/** 서버 출처의 http/https 주소인가. `serverOrigin`은 `URL.origin` 형태다. */
export function isServerUrl(raw: string, serverOrigin: string): boolean {
  const url = parse(raw);
  if (url === null) return false;
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  if (hasCredentials(url)) return false;
  return url.origin === serverOrigin;
}

/**
 * Google 로그인·동의 주소면 판정에 쓴 `URL`의 `href`를, 아니면 null을 돌려준다.
 * https, 기본 포트, 사용자 정보 없음, 호스트 정확히 일치. 시스템 브라우저에는 원본 문자열이 아니라
 * 이 정규화된 값을 넘긴다.
 */
export function googleHref(raw: string): string | null {
  const url = parse(raw);
  if (url === null) return null;
  if (url.protocol !== "https:" || url.port !== "") return null;
  if (hasCredentials(url)) return null;
  return GOOGLE_AUTH_HOSTS.includes(url.hostname) ? url.href : null;
}

export function isGoogleAuthUrl(raw: string): boolean {
  return googleHref(raw) !== null;
}

export function decideNavigation(raw: string, serverOrigin: string): NavigationDecision {
  if (isServerUrl(raw, serverOrigin)) return { action: "allow" };
  const google = googleHref(raw);
  if (google !== null) return { action: "external", url: google };
  return { action: "block" };
}

/** 새 창 요청 판정. 앱 안에서는 새 창을 절대 열지 않고, Google 주소만 시스템 브라우저로 넘긴다. */
export function decideWindowOpen(raw: string): { external: string | null } {
  return { external: googleHref(raw) };
}
