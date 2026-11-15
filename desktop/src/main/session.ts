/** 앱 파티션(`persist:orbit`)의 세션 저장소 정리. Electron `Session`의 필요한 부분만 쓴다. */
export interface ClearableSession {
  clearStorageData: () => Promise<void>;
  clearCache: () => Promise<void>;
}

/**
 * 로그아웃 때 쿠키·캐시·로컬 저장소 등 파티션의 모든 저장소를 지운다(ADR-0024 §2).
 * 둘 중 하나가 실패해도 나머지는 시도하고, 하나라도 실패하면 false를 돌려준다.
 */
export async function clearSessionData(ses: ClearableSession): Promise<boolean> {
  let ok = true;
  for (const clear of [ses.clearStorageData, ses.clearCache]) {
    try {
      await clear.call(ses);
    } catch {
      ok = false;
    }
  }
  return ok;
}
