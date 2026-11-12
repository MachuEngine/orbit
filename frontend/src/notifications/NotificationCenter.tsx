import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "../ui/Icon";
import { NOTIFICATION_PANEL_ID, NotificationPanel } from "./NotificationPanel";
import { BELL_TITLE, NOTIFY_TEXT, badgeText, bellLabel } from "./text";
import type { NotificationsApi } from "./useNotifications";
import "./notifications.css";

/**
 * 헤더의 알림 버튼과 패널(ADR-0021 §6). 버튼의 이름은 "알림 N개"(읽지 않은 수)이고 배지에는 같은 수를
 * 보인다. 패널은 열리면 포커스를 받고 Esc나 배경을 누르면 닫히며, 닫히면 이 버튼으로 포커스를 돌려준다.
 */
export function NotificationCenter({
  notifications,
  now = Date.now,
}: {
  notifications: NotificationsApi;
  now?: () => number;
}) {
  const [open, setOpen] = useState(false);
  const bellRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  const close = useCallback(() => {
    setOpen(false);
  }, []);
  const unread = notifications.state.phase === "ready" ? notifications.state.unread : 0;

  useEffect(() => {
    if (wasOpen.current && !open) bellRef.current?.focus();
    wasOpen.current = open;
  }, [open]);

  return (
    <>
      <button
        type="button"
        ref={bellRef}
        className="btn btn-secondary notify-bell"
        title={BELL_TITLE}
        aria-label={bellLabel(unread)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? NOTIFICATION_PANEL_ID : undefined}
        onClick={() => {
          setOpen(true);
        }}
      >
        <Icon name="bell" />
        {unread > 0 && (
          <span className="notify-badge num" aria-hidden="true">
            {badgeText(unread)}
          </span>
        )}
      </button>
      {/* 영역은 항상 그려 두고 내용만 바꿔야 화면낭독기가 새 알림을 읽는다. */}
      <span role="status" className="visually-hidden">
        {NOTIFY_TEXT.unreadAnnounce(unread)}
      </span>
      {open && <NotificationPanel notifications={notifications} now={now} onClose={close} />}
    </>
  );
}
