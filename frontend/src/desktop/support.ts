/** 데스크톱 브리지 테스트 도구. `window.orbitDesktop`을 가짜로 바꾼다. */
import { vi, type Mock } from "vitest";
import type { DesktopBridge } from "./bridge";

export interface FakeDesktop {
  api: DesktopBridge;
  notify: Mock<DesktopBridge["notify"]>;
  setBadge: Mock<DesktopBridge["setBadge"]>;
  /** 전역 단축키가 눌린 것처럼 등록된 모든 콜백을 부른다. */
  pressShortcut: () => void;
  /** 지금 등록돼 있는 단축키 콜백 수. */
  listeners: () => number;
  uninstall: () => void;
}

export function installDesktop(): FakeDesktop {
  const callbacks = new Set<() => void>();
  const notify = vi.fn<DesktopBridge["notify"]>();
  const setBadge = vi.fn<DesktopBridge["setBadge"]>();
  const api: DesktopBridge = {
    notify,
    setBadge,
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
    pressShortcut: () => {
      for (const callback of [...callbacks]) callback();
    },
    listeners: () => callbacks.size,
    uninstall: () => {
      delete window.orbitDesktop;
    },
  };
}
