export const DEFAULT_SERVER_URL = "http://localhost:5173";
/** 전역 단축키 기본값. macOS에서는 Option+Space다. */
export const DEFAULT_SHORTCUT = "Alt+Space";

/** 암호화하지 않은 http를 허용하는 호스트(`URL.hostname` 형태, IPv6는 대괄호 포함). */
const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["localhost", "127.0.0.1", "[::1]"]);

/**
 * 서버 주소를 검증해 출처(origin)로 돌려준다. http/https만, 경로·쿼리·해시·사용자 정보 없이
 * 출처만 허용한다. http는 loopback에서만, 그 밖의 호스트는 https만 허용한다. 잘못된 값은 조용히 고치지 않고 오류로 알린다.
 */
export function parseServerOrigin(raw: string | undefined): string {
  const text = raw === undefined || raw.trim() === "" ? DEFAULT_SERVER_URL : raw.trim();
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    throw new Error(`ORBIT_DESKTOP_URL 형식이 올바르지 않습니다: ${text}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("ORBIT_DESKTOP_URL은 http 또는 https 주소여야 합니다.");
  }
  if (url.protocol === "http:" && !LOOPBACK_HOSTS.has(url.hostname)) {
    throw new Error(
      "ORBIT_DESKTOP_URL의 http는 localhost·127.0.0.1·[::1]에서만 쓸 수 있습니다. 그 밖은 https를 쓰세요.",
    );
  }
  if (url.username !== "" || url.password !== "") {
    throw new Error("ORBIT_DESKTOP_URL에 사용자 정보를 넣을 수 없습니다.");
  }
  if ((url.pathname !== "/" && url.pathname !== "") || url.search !== "" || url.hash !== "") {
    throw new Error("ORBIT_DESKTOP_URL은 출처(예: http://localhost:5173)만 적어야 합니다.");
  }
  return url.origin;
}

/**
 * 전역 단축키 설정. 값이 없으면 기본값이고, 빈 문자열·`off`면 단축키를 쓰지 않는다(null).
 */
export function parseShortcut(raw: string | undefined): string | null {
  if (raw === undefined) return DEFAULT_SHORTCUT;
  const text = raw.trim();
  if (text === "" || text.toLowerCase() === "off") return null;
  return text;
}
