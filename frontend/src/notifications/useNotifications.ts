import { useCallback, useEffect, useRef, useState } from "react";
import { getNotifications, markAllNotificationsRead, markNotificationRead } from "../api/endpoints";
import { isApiError } from "../api/errors";
import type { NotificationItem } from "../api/schemas";
import { selectToNotify, showBrowserNotification } from "./browser";

export const NOTIFICATION_POLL_MS = 30_000;

export type ArrivedState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "ready"; items: NotificationItem[]; unread: number };

export interface NotificationsApi {
  state: ArrivedState;
  /** 도착한 알림을 지금 다시 읽는다. */
  refresh: () => Promise<void>;
  markRead: (id: string) => Promise<void>;
  markAllRead: () => Promise<void>;
}

/**
 * 도착한 알림을 `pollMs`(기본 30초)마다 읽는다(ADR-0021 §6). 탭이 숨겨지면 멈추고 다시 보이면 바로
 * 읽은 뒤 이어간다. 새로 도착해 읽지 않았고 도착한 지 10분이 안 된 알림은, 이 탭에서 아직 띄우지
 * 않았을 때만 브라우저 알림으로 띄운다(권한이 허용돼 있을 때). 로그인이 끝났으면(401) 폴링을 멈추고
 * `onUnauthorized`를 부른다. `now`는 테스트에서 바꿔 끼운다.
 */
export function useNotifications({
  enabled,
  pollMs = NOTIFICATION_POLL_MS,
  now = Date.now,
  onUnauthorized,
}: {
  enabled: boolean;
  pollMs?: number;
  now?: () => number;
  onUnauthorized?: () => void;
}): NotificationsApi {
  const [state, setState] = useState<ArrivedState>({ phase: "loading" });
  const alive = useRef(false);
  // 요청마다 순번을 매겨 오래된 응답이 새 응답을 덮어쓰지 못하게 한다.
  const inFlight = useRef(0);
  const issued = useRef(0);
  const applied = useRef(0);
  const shown = useRef(new Set<string>());
  // 최신 콜백·시계는 ref로 읽어 폴링 효과가 다시 만들어지지 않게 한다.
  const nowRef = useRef(now);
  const unauthorizedRef = useRef(onUnauthorized);
  useEffect(() => {
    nowRef.current = now;
    unauthorizedRef.current = onUnauthorized;
  });

  /** `force`가 아니면 진행 중인 요청이 있을 때 건너뛴다(폴링). 읽음 처리 뒤에는 `force`로 읽는다. */
  const load = useCallback(async (force = false): Promise<boolean> => {
    if (!force && inFlight.current > 0) return true;
    issued.current += 1;
    const mine = issued.current;
    inFlight.current += 1;
    try {
      const response = await getNotifications();
      if (!alive.current || mine < applied.current) return true; // 더 새 응답을 이미 반영했다
      applied.current = mine;
      setState({ phase: "ready", items: response.notifications, unread: response.unread_count });
      for (const item of selectToNotify(response.notifications, shown.current, nowRef.current())) {
        if (showBrowserNotification(item)) shown.current.add(item.id);
      }
      return true;
    } catch (error) {
      if (!alive.current || mine < applied.current) return true;
      if (isApiError(error) && error.kind === "unauthorized") {
        unauthorizedRef.current?.();
        return false; // 로그인이 끝났다. 폴링을 이어가지 않는다
      }
      // 이미 읽은 목록이 있으면 유지한다(한 번의 실패로 화면을 비우지 않는다).
      setState((prev) => (prev.phase === "ready" ? prev : { phase: "error" }));
      return true;
    } finally {
      inFlight.current -= 1;
    }
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    alive.current = true;
    let cancelled = false; // 이 효과가 정리된 뒤에 도착한 응답이 타이머를 만들지 못하게 한다
    let timer: number | undefined;
    const stop = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
    };
    const start = () => {
      if (timer !== undefined) return;
      timer = window.setInterval(() => {
        void load().then((keepGoing) => {
          if (!keepGoing) stop();
        });
      }, pollMs);
    };
    const onVisibility = () => {
      if (document.hidden) {
        stop();
        return;
      }
      void load().then((keepGoing) => {
        if (keepGoing && !cancelled && !document.hidden) start();
      });
    };
    if (!document.hidden) onVisibility();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      alive.current = false;
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, pollMs, load]);

  const refresh = useCallback(async () => {
    await load(true);
  }, [load]);

  const markRead = useCallback(
    async (id: string) => {
      await markNotificationRead(id);
      await load(true);
    },
    [load],
  );

  const markAllRead = useCallback(async () => {
    await markAllNotificationsRead();
    await load(true);
  }, [load]);

  return { state, refresh, markRead, markAllRead };
}
