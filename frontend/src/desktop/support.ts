/** 데스크톱 브리지 테스트 도구. `window.orbitDesktop`을 가짜로 바꾼다. */
import { vi, type Mock } from "vitest";
import type { ConnectEvent, DesktopBridge, LoginEvent, PreparedConnect } from "./bridge";

export interface FakeDesktop {
  api: DesktopBridge;
  notify: Mock<DesktopBridge["notify"]>;
  setBadge: Mock<DesktopBridge["setBadge"]>;
  /** 전역 단축키가 눌린 것처럼 등록된 모든 콜백을 부른다. */
  pressShortcut: () => void;
  startLogin: Mock<() => void>;
  openConnectFlow: Mock<(url: string) => void>;
  prepareConnect: Mock<NonNullable<DesktopBridge["prepareConnect"]>>;
  /** 셸이 연결 이벤트를 보낸 것처럼 등록된 콜백을 모두 부른다. */
  emitConnect: (event: ConnectEvent) => void;
  clearSession: Mock<() => void>;
  /** 셸이 로그인 이벤트를 보낸 것처럼 등록된 콜백을 모두 부른다. */
  emitLogin: (event: LoginEvent) => void;
  /** 지금 등록돼 있는 단축키 콜백 수. */
  listeners: () => number;
  uninstall: () => void;
}

export function installDesktop(): FakeDesktop {
  const callbacks = new Set<() => void>();
  const notify = vi.fn<DesktopBridge["notify"]>();
  const setBadge = vi.fn<DesktopBridge["setBadge"]>();
  const loginCallbacks = new Set<(event: LoginEvent) => void>();
  const startLogin = vi.fn<() => void>();
  const openConnectFlow = vi.fn<(url: string) => void>();
  const connectCallbacks = new Set<(event: ConnectEvent) => void>();
  const prepared: PreparedConnect = {
    code_challenge: "A".repeat(43),
    port: 53124,
    app_state: "S".repeat(43),
  };
  const prepareConnect = vi.fn<NonNullable<DesktopBridge["prepareConnect"]>>(() =>
    Promise.resolve(prepared),
  );
  const clearSession = vi.fn<() => void>();
  const api: DesktopBridge = {
    notify,
    setBadge,
    startLogin,
    openConnectFlow,
    prepareConnect,
    onConnectEvent: (callback) => {
      connectCallbacks.add(callback);
      return () => {
        connectCallbacks.delete(callback);
      };
    },
    clearSession,
    onLoginEvent: (callback) => {
      loginCallbacks.add(callback);
      return () => {
        loginCallbacks.delete(callback);
      };
    },
    onToggleVoice: (callback) => {
      callbacks.add(callback);
      return () => {
        callbacks.delete(callback);
      };
    },
    info: { platform: "darwin", version: "0.0.0" },
  };
  window.orbitDesktop = api;
  return {
    api,
    notify,
    setBadge,
    startLogin,
    openConnectFlow,
    prepareConnect,
    emitConnect: (event) => {
      for (const callback of [...connectCallbacks]) callback(event);
    },
    clearSession,
    emitLogin: (event) => {
      for (const callback of [...loginCallbacks]) callback(event);
    },
    pressShortcut: () => {
      for (const callback of [...callbacks]) callback();
    },
    listeners: () => callbacks.size,
    uninstall: () => {
      delete window.orbitDesktop;
    },
  };
}
