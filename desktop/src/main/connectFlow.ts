/**
 * 계정 연결 시작 주소 판정(ADR-0024 §5). renderer가 넘긴 주소가 서버 출처의 `desktop-begin` 경로이고
 * 티켓 하나만 가진 형태일 때만 시스템 브라우저로 연다. 범용 `openExternal`은 노출하지 않는다.
 */
export const BEGIN_PATH = "/api/connections/google/desktop-begin";
const TICKET_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

/**
 * 허용하는 주소면 판정에 쓴 `URL`의 정규화된 `href`를, 아니면 null을 돌려준다.
 * 출처(스킴·호스트·포트) 정확히 일치, 사용자 정보·해시 없음, 경로 일치, 쿼리는 `ticket` 하나뿐이다.
 */
export function connectFlowHref(raw: unknown, serverOrigin: string): string | null {
  if (typeof raw !== "string" || raw.length > 2048) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.origin !== serverOrigin) return null;
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.username !== "" || url.password !== "" || url.hash !== "") return null;
  if (url.pathname !== BEGIN_PATH) return null;
  const keys = [...url.searchParams.keys()];
  if (keys.length !== 1 || keys[0] !== "ticket") return null;
  const ticket = url.searchParams.get("ticket");
  if (ticket === null || !TICKET_PATTERN.test(ticket)) return null;
  return url.href;
}
