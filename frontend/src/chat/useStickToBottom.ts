import { useCallback, useEffect, useRef, useState } from "react";

/** 맨 아래에서 이만큼 안쪽이면 "아래에 붙어 있다"고 본다. */
export const NEAR_BOTTOM_PX = 48;

export function isNearBottom(
  metrics: { scrollTop: number; scrollHeight: number; clientHeight: number },
  threshold = NEAR_BOTTOM_PX,
): boolean {
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight <= threshold;
}

/**
 * 새 내용이 생기면 맨 아래로 따라가되, 사용자가 위로 올라가 읽는 중이면 따라가지 않는다.
 * `changeKey`가 바뀌거나 `contentRef` 요소의 크기가 바뀔 때 맨 아래로 이동한다.
 * `paused`가 true인 동안(승인 카드를 읽는 중)에는 따라가지 않는다.
 */
export function useStickToBottom(changeKey: unknown, paused = false) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  const pausedRef = useRef(paused);
  const [away, setAway] = useState(false);

  const toBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el !== null) el.scrollTop = el.scrollHeight;
  }, []);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (el === null) return;
    stuck.current = isNearBottom(el);
    setAway(!stuck.current);
  }, []);

  useEffect(() => {
    pausedRef.current = paused;
    if (stuck.current && !paused) toBottom();
  }, [changeKey, paused, toBottom]);

  useEffect(() => {
    const content = contentRef.current;
    if (content === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (stuck.current && !pausedRef.current) toBottom();
    });
    observer.observe(content);
    return () => {
      observer.disconnect();
    };
  }, [toBottom]);

  const jumpToBottom = useCallback(() => {
    stuck.current = true;
    setAway(false);
    toBottom();
  }, [toBottom]);

  return { scrollRef, contentRef, onScroll, showJump: away, jumpToBottom };
}
