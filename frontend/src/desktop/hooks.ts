import { useEffect, useRef } from "react";
import { getDesktop } from "./bridge";

/** 읽지 않은 수를 독 배지에 맞춘다. 데스크톱이 아니면 아무것도 하지 않는다. 사라질 때 지운다. */
export function useDesktopBadge(count: number): void {
  useEffect(() => {
    getDesktop()?.setBadge(count);
  }, [count]);
  useEffect(
    () => () => {
      getDesktop()?.setBadge(0);
    },
    [],
  );
}

/**
 * 전역 단축키(Option+Space) 신호가 오면 음성 입력을 켜고 끈다(ADR-0024 §6, ADR-0025 토글).
 * 마이크를 지원하지 않는 상태면 아무것도 하지 않는다.
 */
export function useDesktopVoiceToggle(voice: { supported: boolean; toggle: () => void }): void {
  const latest = useRef(voice);
  useEffect(() => {
    latest.current = voice;
  });
  useEffect(() => {
    const desktop = getDesktop();
    if (desktop === undefined) return undefined;
    return desktop.onToggleVoice(() => {
      if (!latest.current.supported) return;
      latest.current.toggle();
    });
  }, []);
}
