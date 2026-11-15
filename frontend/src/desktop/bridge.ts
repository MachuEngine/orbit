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
  /**
   * 아래는 로그인·연결 핸드오프(ADR-0024 §4·§5)용이다. 서버 화면보다 오래된 앱에는 없을 수
   * 있어 선택 항목으로 두고, 쓰는 쪽이 있는지 확인한다.
   */
  /** 시스템 브라우저에서 Google 로그인을 시작한다. 결과는 `onLoginEvent`로 온다. */
  startLogin?: () => void;
  /** 로그인 실패·완료 이벤트를 받는다. 완료되면 셸이 창을 다시 불러온다. */
  onLoginEvent?: (callback: (event: LoginEvent) => void) => () => void;
  /**
   * 계정 연결 준비(ADR-0024 §5). 셸이 PKCE·앱 `state`·loopback 수신기를 만들고 `code_challenge`·포트·
   * 앱 `state`만 돌려준다(검증기는 셸에만 있다). 티켓 요청에 이 값을 함께 보낸다.
   */
  prepareConnect?: (args: {
    intent: "connect" | "docs_scope";
    connection_id?: string;
  }) => Promise<PreparedConnect>;
  /** 연결 완료·실패 이벤트. 완료는 셸이 앱 세션으로 서버에 제출한 결과다. */
  onConnectEvent?: (callback: (event: ConnectEvent) => void) => () => void;
  /** 서버의 계정 연결 시작 주소를 시스템 브라우저로 연다. */
  openConnectFlow?: (url: string) => void;
  /** 로그아웃 후 앱의 쿠키·캐시·저장소를 지운다. */
  clearSession?: () => void;
}

export interface PreparedConnect {
  code_challenge: string;
  port: number;
  app_state: string;
}

export type ConnectEvent =
  { status: "done"; result: "connected" | "docs_added" } | { status: "failed"; reason: string };

export type LoginEvent = { status: "failed"; reason: string } | { status: "done" };

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

/** 이 앱이 시스템 브라우저 로그인 핸드오프를 지원하면 해당 브리지를 돌려준다. */
export function getLoginBridge():
  | (DesktopBridge & {
      startLogin: () => void;
      onLoginEvent: (callback: (event: LoginEvent) => void) => () => void;
    })
  | undefined {
  const desktop = getDesktop();
  if (
    desktop === undefined ||
    typeof desktop.startLogin !== "function" ||
    typeof desktop.onLoginEvent !== "function"
  ) {
    return undefined;
  }
  return desktop as DesktopBridge & {
    startLogin: () => void;
    onLoginEvent: (callback: (event: LoginEvent) => void) => () => void;
  };
}

export interface ConnectBridge {
  prepareConnect: NonNullable<DesktopBridge["prepareConnect"]>;
  onConnectEvent: NonNullable<DesktopBridge["onConnectEvent"]>;
  openConnectFlow: NonNullable<DesktopBridge["openConnectFlow"]>;
}

/** 이 앱이 시스템 브라우저 연결 핸드오프를 지원하면 그 함수들을 돌려준다. 하나라도 없으면 없다. */
export function getConnectBridge(): ConnectBridge | undefined {
  const desktop = getDesktop();
  if (
    desktop === undefined ||
    typeof desktop.prepareConnect !== "function" ||
    typeof desktop.onConnectEvent !== "function" ||
    typeof desktop.openConnectFlow !== "function"
  ) {
    return undefined;
  }
  return {
    prepareConnect: desktop.prepareConnect.bind(desktop),
    onConnectEvent: desktop.onConnectEvent.bind(desktop),
    openConnectFlow: desktop.openConnectFlow.bind(desktop),
  };
}

export function isDesktop(): boolean {
  return getDesktop() !== undefined;
}
