/**
 * 데스크톱 셸(`desktop/`, ADR-0024)이 노출하는 `window.orbitDesktop`을 다루는 곳.
 * 웹 브라우저에는 이 객체가 없으므로 모든 데스크톱 분기는 여기서 확인한다. 없으면 웹 동작 그대로다.
 */
export interface DesktopBridge {
  /** 시스템 알림을 띄운다. 같은 `id`는 셸이 한 번만 띄운다. */
  notify: (payload: { id: string; title: string; body?: string }) => void;
  /** 독 배지에 읽지 않은 수를 보인다. 0이면 지운다. */
  setBadge: (count: number) => void;
  /** 전역 단축키 신호를 받는다. 돌려주는 함수로 해제한다. */
  onToggleVoice: (callback: () => void) => () => void;
  info: { platform: string; version: string };
}

declare global {
  interface Window {
    orbitDesktop?: DesktopBridge;
  }
}

/** 데스크톱 셸 안이면 브리지를, 아니면 `undefined`를 돌려준다. 형태가 다르면 없는 것으로 본다. */
export function getDesktop(): DesktopBridge | undefined {
  if (typeof window === "undefined") return undefined;
  const desktop = (window as { orbitDesktop?: unknown }).orbitDesktop;
  if (typeof desktop !== "object" || desktop === null) return undefined;
  const candidate = desktop as Partial<DesktopBridge>;
  if (
    typeof candidate.notify !== "function" ||
    typeof candidate.setBadge !== "function" ||
    typeof candidate.onToggleVoice !== "function"
  ) {
    return undefined;
  }
  return candidate as DesktopBridge;
}

export function isDesktop(): boolean {
  return getDesktop() !== undefined;
}
