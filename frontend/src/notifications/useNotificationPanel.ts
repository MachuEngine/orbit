import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelNotification,
  getNotificationSettings,
  getUpcomingNotifications,
  putNotificationSettings,
} from "../api/endpoints";
import { isApiError } from "../api/errors";
import type { NotificationItem, NotificationSettings } from "../api/schemas";

export type UpcomingState =
  { phase: "loading" } | { phase: "error" } | { phase: "ready"; items: NotificationItem[] };

export type SettingsState =
  { phase: "loading" } | { phase: "error" } | { phase: "ready"; settings: NotificationSettings };

/** 취소 결과: 취소했다 / 이미 도착했거나 취소돼 있었다 / 실패했다. */
export type CancelResult = "cancelled" | "gone" | "failed";

/** 패널이 열려 있는 동안만 예정된 알림과 설정을 읽고, 취소와 설정 저장을 보낸다. */
export function useNotificationPanel(open: boolean) {
  const [upcoming, setUpcoming] = useState<UpcomingState>({ phase: "loading" });
  const [settings, setSettings] = useState<SettingsState>({ phase: "loading" });
  const alive = useRef(false);

  const loadUpcoming = useCallback(async () => {
    try {
      const items = await getUpcomingNotifications();
      if (alive.current) setUpcoming({ phase: "ready", items });
    } catch {
      if (alive.current)
        setUpcoming((prev) => (prev.phase === "ready" ? prev : { phase: "error" }));
    }
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      const found = await getNotificationSettings();
      if (alive.current) setSettings({ phase: "ready", settings: found });
    } catch {
      if (alive.current)
        setSettings((prev) => (prev.phase === "ready" ? prev : { phase: "error" }));
    }
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    alive.current = true;
    getUpcomingNotifications().then(
      (items) => {
        if (alive.current) setUpcoming({ phase: "ready", items });
      },
      () => {
        if (alive.current) setUpcoming({ phase: "error" });
      },
    );
    getNotificationSettings().then(
      (found) => {
        if (alive.current) setSettings({ phase: "ready", settings: found });
      },
      () => {
        if (alive.current) setSettings({ phase: "error" });
      },
    );
    return () => {
      alive.current = false;
    };
  }, [open]);

  const cancel = useCallback(
    async (id: string): Promise<CancelResult> => {
      try {
        await cancelNotification(id);
        await loadUpcoming();
        return "cancelled";
      } catch (error) {
        if (isApiError(error) && (error.kind === "conflict" || error.kind === "not_found")) {
          await loadUpcoming();
          return "gone";
        }
        return "failed";
      }
    },
    [loadUpcoming],
  );

  /** 설정을 저장한다. 성공하면 저장된 값으로 바꾸고 예정 목록(시각이 바뀐다)을 다시 읽는다. */
  const save = useCallback(
    async (next: NotificationSettings): Promise<boolean> => {
      try {
        const saved = await putNotificationSettings(next);
        if (alive.current) setSettings({ phase: "ready", settings: saved });
        await loadUpcoming();
        return true;
      } catch {
        return false;
      }
    },
    [loadUpcoming],
  );

  return {
    upcoming,
    settings,
    reloadUpcoming: loadUpcoming,
    reloadSettings: loadSettings,
    cancel,
    save,
  };
}
